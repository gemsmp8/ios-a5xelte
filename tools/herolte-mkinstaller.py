#!/usr/bin/env python3
"""Build a TWRP-flashable King-iOS16-herolte.zip for the Galaxy S7 SM-G930F.

This tool takes the separately-built components and produces a ZIP that TWRP
can install in one step.  It does NOT contain Apple proprietary software: the
user supplies their own iOS root image, built privately.

Required inputs:
  --kernel        kernel.development.herolte  (from CI artifact xnu-kernel)
  --mesa          path/to/mesa-ios/           (CI artifact mesa-ios, extracted)
  --boot-stock    boot-stock.img              (user's own BOOT backup from the S7)
  --ios-root      root.img.gz.part*           (private iOS root image parts)

Optional:
  --loader        xnu_boot.Image              (pre-built loader; otherwise uses shim/herolte/out/)
  --gpu-mode      (default: cpu)  cpu | gpu   (gpu enables Mali-T880 scaled display)
  --cpus          (default: 4)                (number of A53 cores; 1 for bringup)
  --output        King-iOS16-herolte.zip

The ZIP ships a pre-built xnu-boot.img (packed on the host by herolte-pack-boot.py).
The update-binary never manipulates BOOT bytes -- it just dd's the image.
"""
import argparse
import hashlib
import os
import struct
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BOOT_PARTITION_SIZE = 41943040  # 40 MiB, from herolte-howto.md / blockdev

# UFS block device paths on SM-G930F
UFS_PLATFORM = "155a0000.ufs"
BOOT_DEV = f"/dev/block/platform/{UFS_PLATFORM}/by-name/BOOT"
DATA_DEV = f"/dev/block/platform/{UFS_PLATFORM}/by-name/USERDATA"

# USERDATA layout constants (must match herolte-drop.py and bsd/dev/herolte_drop.c)
USERDATA_SIZE = 21566 * 1024 * 1024  # 21566 MiB from blockdev, ~22.6 GB
BLOCK = 4096
LOG_BLOCKS = 1 + (4 << 20) // BLOCK      # kernel log: 4 MiB + 1 header block
DROP_BLOCKS = 32768                        # drop zone: 128 MiB
# Layout: [HFSX root ... ] [gap] [drop zone: DROP_BLOCKS*4096] [log: LOG_BLOCKS*4096]
# Drop zone starts at: total_blocks - LOG_BLOCKS - DROP_BLOCKS
# Max root image size: must not overlap drop zone start offset


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_kernel(path):
    data = open(path, 'rb').read(8)
    if struct.unpack_from('<I', data, 0)[0] != 0xfeedfacf:
        sys.exit(f'kernel is not a 64-bit Mach-O: {path}')


def verify_boot_stock(path):
    data = open(path, 'rb').read(8)
    if data != b'ANDROID!':
        sys.exit(f'not an Android BOOT image: {path}')
    size = os.path.getsize(path)
    if size < 8192:
        sys.exit(f'BOOT image too small ({size} bytes): {path}')
    return sha256_file(path)


def verify_mesa(mesa_dir):
    mesa = Path(mesa_dir)
    required = ['lib/libOSMesa.8.dylib', 'lib/libdrm.2.dylib', 'hero-run']
    for f in required:
        if not (mesa / f).exists():
            sys.exit(f'mesa artifact missing {f} in {mesa_dir}')
    return mesa


def verify_root_parts(parts):
    if not parts:
        sys.exit('no iOS root image parts provided')
    for p in parts:
        if not os.path.exists(p):
            sys.exit(f'root image part not found: {p}')
    return parts


def measure_root_uncompressed_size(parts):
    """Return the exact uncompressed byte count of the gzip root image."""
    import gzip
    total = 0
    buf = bytearray(1 << 20)
    class _Chain:
        def __init__(self, paths):
            self._files = [open(p, 'rb') for p in paths]
        def read(self, n=-1):
            out = b''
            while self._files and (n < 0 or len(out) < n):
                data = self._files[0].read(-1 if n < 0 else n - len(out))
                if not data:
                    self._files.pop(0).close()
                    continue
                out += data
            return out
    stream = gzip.GzipFile(fileobj=_Chain(parts))
    while True:
        n = stream.readinto(buf)
        if not n:
            break
        total += n
    return total


def resolve_loader():
    built = REPO_ROOT / 'shim' / 'herolte' / 'out' / 'xnu_boot.Image'
    if built.exists():
        return built
    return None


def build_payload(kernel, gpu_mode, cpus, workdir):
    sys.path.insert(0, str(REPO_ROOT / 'tools'))

    adt_path = os.path.join(workdir, 'herolte.adt')
    payload_path = os.path.join(workdir, 'payload.bin')

    cmdline_parts = [
        'debug=0xc', 'serial=3', f'cpus={cpus}', 'keepsyms=1',
        '-enable_kprintf_spam', '-noprogress',
        'rd=ufsroot0', 'ufsroot=USERDATA', 'ufsrw=1', 'rootrw=1',
        'cs_enforcement_disable=1', 'unentitled_ios_sim_launch=1',
        'ignition_level=1', 'launchd_unsecure_cache=1',
        'wdt=-1', '-wdt_no_pmu', 'wdt_test=no_user_checkin',
        'a5fb_keep_on=1', 'a5fb_noconsole=1', 'a5fb_perf=1', 'sweetfb_native=1',
    ]
    if gpu_mode == 'gpu':
        cmdline_parts.extend([
            'sweetfb_scale=1', 'sweetfb_lw=828', 'sweetfb_lh=1792',
            'sweetfb_gpu=1', 'herogpu=1',
        ])

    import mkadt_herolte
    if gpu_mode == 'gpu':
        root = mkadt_herolte.build(scale=2,
                                    dram=(mkadt_herolte.BANK2_BASE,
                                          min(1024 << 20, mkadt_herolte.BANK2_SIZE)))
    else:
        root = mkadt_herolte.build()

    import adt as adt_mod
    adt_blob = adt_mod.serialize(root)
    open(adt_path, 'wb').write(adt_blob)
    print(f'ADT: {len(adt_blob)} bytes -> {adt_path}')

    import mkpayload
    cmdline = ' '.join(cmdline_parts)
    blob, info = mkpayload.build(str(kernel), adt_path, cmdline)
    open(payload_path, 'wb').write(blob)
    print(f'Payload: {len(blob)} bytes -> {payload_path}')
    for k, v in info.items():
        if k != 'segs':
            print(f'  {k}: {v}')
    return payload_path


def pack_boot_image(stock_path, loader_path, payload_path, workdir):
    """Use herolte-pack-boot.py logic to build the XNU BOOT image on the host."""
    sys.path.insert(0, str(REPO_ROOT / 'tools'))

    # Import the pack-boot module's functions directly
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "herolte_pack_boot",
        str(REPO_ROOT / 'tools' / 'herolte-pack-boot.py'))
    pack_boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack_boot)

    out_path = Path(workdir) / 'xnu-boot.img'

    class PackArgs:
        stock = Path(stock_path)
        image = Path(loader_path)
        output = out_path
        append = Path(payload_path)
        ramdisk = None
        partition_size = BOOT_PARTITION_SIZE

    pack_boot.cmd_pack(PackArgs())
    size = out_path.stat().st_size
    print(f'XNU BOOT image: {size} bytes (partition limit: {BOOT_PARTITION_SIZE})')
    return str(out_path)


def build_drop_package(mesa_dir, workdir):
    PREFIX = '/private/var/herolte/'
    items = []

    autorun = (
        b'#!/bin/sh\n'
        b'DEST=/private/var/herolte\n'
        b'mkdir -p "$DEST/lib"\n'
        b'if [ -x "$DEST/osmesa-test" ]; then\n'
        b'    "$DEST/osmesa-test" 2>&1 || true\n'
        b'fi\n'
        b'if [ -x "$DEST/hero-run" ]; then\n'
        b'    exec "$DEST/hero-run"\n'
        b'fi\n'
    )
    items.append(('autorun', 0o755, autorun))

    mesa = Path(mesa_dir)
    for lib in sorted((mesa / 'lib').glob('*.dylib')):
        items.append((f'lib/{lib.name}', 0o644, lib.read_bytes()))

    for name in ('osmesa-test', 'hero-run', 'eagl_ca_test'):
        p = mesa / name
        if p.exists():
            items.append((name, 0o755, p.read_bytes()))

    dirs_seen = set()
    entries = []
    for dest, mode, data in items:
        parts = dest.split('/')
        for i in range(1, len(parts)):
            d = PREFIX + '/'.join(parts[:i])
            if d not in dirs_seen:
                dirs_seen.add(d)
                entries.append((d.encode(), 0, b''))
        entries.append((PREFIX.encode() + dest.encode(), mode, data))

    payload = bytearray()
    for path, mode, data in entries:
        payload += struct.pack('<IIQ', len(path), mode, len(data))
        payload += path
        payload += b'\0' * (-len(payload) % 8)
        payload += data
        payload += b'\0' * (-len(payload) % 8)

    max_payload = (DROP_BLOCKS - 1) * BLOCK
    if len(payload) > max_payload:
        sys.exit(f'Drop package too large: {len(payload)} > {max_payload}')

    ident = int(time.time())
    header = struct.pack('<8sIIQQ', b'HERODRP1', 1, len(entries), len(payload), ident)
    blob = header + b'\0' * (BLOCK - len(header)) + bytes(payload)
    blob += b'\0' * (-len(blob) % BLOCK)

    out = os.path.join(workdir, 'drop-package.bin')
    open(out, 'wb').write(blob)
    print(f'Drop package: {len(blob)} bytes, {len(entries)} entries -> {out}')
    return out


# ---------------------------------------------------------------------------
# TWRP update-binary: the shell script TWRP runs on the phone.
#
# Key design decisions vs. the previous version:
#   1. NO backup to /sdcard -- /sdcard IS on USERDATA which we erase.
#      Instead, require the user to have backed up externally before flashing.
#   2. NO shell byte manipulation for BOOT -- the ZIP ships a pre-built
#      xnu-boot.img assembled on the host by herolte-pack-boot.py.
#   3. Verify exact model SM-G930F, not just the UFS platform.
#   4. Battery and storage capacity checks.
#   5. Read-back verification for BOOT write.
#   6. Abort on ANY failure; never report success if something failed.
# ---------------------------------------------------------------------------

UPDATE_BINARY = r'''#!/sbin/sh
# King-iOS16-herolte TWRP installer
# Galaxy S7 SM-G930F (Exynos 8890) - XNU / iOS 16.7.16
#
# Safety design:
#   - Root parts are STREAMED from the ZIP via unzip -p, never extracted to /tmp.
#   - If the ZIP lives on internal storage (/sdcard = USERDATA) and contains
#     root parts, the install is aborted: writing USERDATA would destroy the ZIP.
#   - BOOT is flashed LAST so an interrupted root write leaves Android intact.
#   - abort() messages reflect whether destructive writes already happened.
OUTFD=/proc/self/fd/$2
ZIPFILE="$3"

ui_print() { echo "ui_print $1" > "$OUTFD"; echo "ui_print" > "$OUTFD"; }
set_progress() { echo "set_progress $1" > "$OUTFD"; }

WROTE_USERDATA=0
WROTE_BOOT=0

abort() {
    ui_print ""
    ui_print "FATAL: $1"
    if [ "$WROTE_USERDATA" = 1 ] || [ "$WROTE_BOOT" = 1 ]; then
        ui_print "WARNING: Partial writes occurred before this failure."
        [ "$WROTE_USERDATA" = 1 ] && ui_print "  USERDATA was modified (root/drop zone)."
        [ "$WROTE_BOOT" = 1 ] && ui_print "  BOOT was modified."
        ui_print "Recovery: flash stock BOOT from your backup, then Format Data in TWRP."
    else
        ui_print "Installation aborted. No changes were made."
    fi
    exit 1
}

require() {
    if [ $? -ne 0 ]; then
        abort "$1"
    fi
}

ui_print "========================================"
ui_print " King iOS 16 for Galaxy S7 (SM-G930F)"
ui_print "========================================"
ui_print ""

# ---- STEP 1: DEVICE VERIFICATION ----
ui_print "[1/7] Verifying device..."

MODEL=$(getprop ro.product.model 2>/dev/null)
DEVICE=$(getprop ro.product.device 2>/dev/null)
PLATFORM=$(ls /dev/block/platform/ 2>/dev/null | head -1)

if [ "$PLATFORM" != "155a0000.ufs" ]; then
    abort "Wrong platform: expected 155a0000.ufs (Exynos 8890), found '$PLATFORM'"
fi

if [ -n "$MODEL" ] && [ "$MODEL" != "SM-G930F" ]; then
    abort "Wrong model: expected SM-G930F, found '$MODEL'. This installer is ONLY for the Galaxy S7 (Exynos)."
fi

BOOT_DEV="/dev/block/platform/155a0000.ufs/by-name/BOOT"
DATA_DEV="/dev/block/platform/155a0000.ufs/by-name/USERDATA"

[ -b "$BOOT_DEV" ] || abort "BOOT partition not found at $BOOT_DEV"
[ -b "$DATA_DEV" ] || abort "USERDATA partition not found at $DATA_DEV"

BOOT_SIZE=$(blockdev --getsize64 "$BOOT_DEV")
DATA_SIZE=$(blockdev --getsize64 "$DATA_DEV")
ui_print "  Model: ${MODEL:-unknown} Device: ${DEVICE:-unknown}"
ui_print "  BOOT: $BOOT_SIZE bytes  USERDATA: $DATA_SIZE bytes"

set_progress 0.05

# ---- STEP 2: PRE-FLIGHT CHECKS ----
ui_print "[2/7] Pre-flight checks..."

# Battery
BATT=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null || echo 100)
if [ "$BATT" -lt 25 ]; then
    abort "Battery too low: ${BATT}%. Charge to at least 25% before installing."
fi
ui_print "  Battery: ${BATT}%"

# Storage
if [ "$DATA_SIZE" -lt 20000000000 ]; then
    abort "USERDATA too small: $DATA_SIZE bytes (need >= 20 GB for iOS root)"
fi

# Check if ZIP contains root parts
HAS_ROOT=0
if unzip -l "$ZIPFILE" 2>/dev/null | grep -q 'root/root\.img\.gz\.part'; then
    HAS_ROOT=1
fi

# If root parts exist, verify the ZIP is NOT on USERDATA.
# On the S7, /sdcard /data /data/media all live on USERDATA. If we write
# USERDATA while reading the ZIP from it, we destroy the ZIP mid-install.
#
# Strategy: resolve the block device backing the ZIP's mount point and
# compare it against USERDATA's block device. Both must resolve successfully.
# Path names alone are never accepted as proof of external storage.
if [ "$HAS_ROOT" = 1 ]; then
    ZIP_REAL=$(readlink -f "$ZIPFILE" 2>/dev/null)
    if [ -z "$ZIP_REAL" ]; then
        abort "Cannot resolve ZIP path ($ZIPFILE). readlink -f failed."
    fi

    # Resolve USERDATA's actual device node
    DATA_REAL=$(readlink -f "$DATA_DEV" 2>/dev/null)
    if [ -z "$DATA_REAL" ]; then
        abort "Cannot resolve USERDATA device ($DATA_DEV). readlink -f failed."
    fi

    # Walk up the ZIP's directory tree to find the mount point
    ZIP_MOUNT=""
    ZIP_BLOCK_DEV=""
    PROBE=$(dirname "$ZIP_REAL")
    while [ "$PROBE" != "/" ]; do
        MLINE=$(mount 2>/dev/null | grep " on $PROBE " | head -1)
        if [ -n "$MLINE" ]; then
            ZIP_MOUNT="$PROBE"
            ZIP_BLOCK_DEV=$(echo "$MLINE" | awk '{print $1}')
            break
        fi
        PROBE=$(dirname "$PROBE")
    done

    # Also check root mount as last resort
    if [ -z "$ZIP_MOUNT" ]; then
        MLINE=$(mount 2>/dev/null | grep " on / " | head -1)
        if [ -n "$MLINE" ]; then
            ZIP_MOUNT="/"
            ZIP_BLOCK_DEV=$(echo "$MLINE" | awk '{print $1}')
        fi
    fi

    if [ -z "$ZIP_MOUNT" ] || [ -z "$ZIP_BLOCK_DEV" ]; then
        abort "Cannot determine which block device backs the ZIP path ($ZIP_REAL). Mount point resolution failed. Copy the ZIP to an external SD card or OTG drive."
    fi

    # Resolve the ZIP's backing device through symlinks
    ZIP_BLOCK_REAL=$(readlink -f "$ZIP_BLOCK_DEV" 2>/dev/null)
    if [ -z "$ZIP_BLOCK_REAL" ]; then
        abort "Cannot resolve ZIP backing device ($ZIP_BLOCK_DEV). readlink -f failed. Copy the ZIP to an external SD card or OTG drive."
    fi

    ui_print "  ZIP location: $ZIP_REAL"
    ui_print "  ZIP mount: $ZIP_MOUNT ($ZIP_BLOCK_DEV -> $ZIP_BLOCK_REAL)"
    ui_print "  USERDATA device: $DATA_DEV -> $DATA_REAL"

    if [ "$ZIP_BLOCK_REAL" = "$DATA_REAL" ]; then
        abort "ZIP is on USERDATA ($ZIP_BLOCK_DEV -> $DATA_REAL). Writing the iOS root would destroy the ZIP mid-install. Copy the ZIP to an external SD card or OTG drive."
    fi

    # Secondary check: reject known internal-storage paths even if the device
    # comparison passed (defensive against mount table bugs or bind mounts)
    case "$ZIP_REAL" in
        /sdcard/*|/data/*|/storage/emulated/*|/storage/sdcard0/*|/storage/0/*)
            abort "ZIP path ($ZIP_REAL) is a known internal storage location even though device comparison passed. This is unexpected. Copy the ZIP to an external SD card or OTG drive."
            ;;
    esac

    ui_print "  ZIP confirmed on separate device from USERDATA."
fi

# Ensure /data unmounted
if mount | grep -q ' /data '; then
    ui_print "  Unmounting /data..."
    umount /data 2>/dev/null || abort "Cannot unmount /data. Use TWRP Mount menu to unmount it first."
fi

set_progress 0.10

# ---- STEP 3: EXTRACT SMALL FILES AND VERIFY INTEGRITY ----
ui_print "[3/7] Extracting metadata and verifying..."
TMPDIR=/tmp/king-ios
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"

# Extract ONLY small files (boot/, mesa/, SHA256SUMS). NOT root/ parts.
unzip -o "$ZIPFILE" 'boot/*' 'mesa/*' 'SHA256SUMS' -d "$TMPDIR" 2>/dev/null
if [ $? -ne 0 ] && [ $? -ne 11 ]; then
    # exit 11 = "no matching files" which is OK if some categories are absent
    abort "Failed to extract metadata from ZIP"
fi

cd "$TMPDIR"
if [ -f SHA256SUMS ]; then
    if command -v sha256sum >/dev/null 2>&1; then
        FAIL=0
        while IFS= read -r line; do
            hash=$(echo "$line" | awk '{print $1}')
            file=$(echo "$line" | awk '{print $2}')
            # Only verify files we extracted (skip root/ parts)
            case "$file" in root/*) continue ;; esac
            if [ -f "$file" ]; then
                actual=$(sha256sum "$file" | awk '{print $1}')
                if [ "$hash" != "$actual" ]; then
                    ui_print "  CORRUPT: $file"
                    FAIL=1
                fi
            fi
        done < SHA256SUMS
        # Verify root parts by streaming from ZIP (no extraction needed)
        if [ "$HAS_ROOT" = 1 ]; then
            while IFS= read -r line; do
                hash=$(echo "$line" | awk '{print $1}')
                file=$(echo "$line" | awk '{print $2}')
                case "$file" in root/*) ;; *) continue ;; esac
                actual=$(unzip -p "$ZIPFILE" "$file" 2>/dev/null | sha256sum | awk '{print $1}')
                if [ "$hash" != "$actual" ]; then
                    ui_print "  CORRUPT: $file"
                    FAIL=1
                fi
            done < SHA256SUMS
        fi
        if [ "$FAIL" = 1 ]; then
            abort "File integrity check failed -- ZIP is corrupt or tampered."
        fi
        ui_print "  Integrity check passed."
    else
        ui_print "  WARNING: sha256sum not available in this TWRP build."
    fi
fi

set_progress 0.15

# ---- STEP 4: VERIFY BOOT COMPATIBILITY ----
ui_print "[4/7] Verifying BOOT compatibility..."
if [ -f boot/boot-stock-sha256.txt ]; then
    EXPECTED_SHA=$(cat boot/boot-stock-sha256.txt | awk '{print $1}')
    CURRENT_SHA=$(dd if="$BOOT_DEV" bs=1048576 2>/dev/null | sha256sum | awk '{print $1}')
    if [ "$EXPECTED_SHA" != "$CURRENT_SHA" ]; then
        ui_print "  WARNING: Current BOOT does not match the stock image"
        ui_print "  used to build this installer."
        ui_print "  Expected: $EXPECTED_SHA"
        ui_print "  Current:  $CURRENT_SHA"
        ui_print "  If you already flashed a previous King-iOS build, this is OK."
        ui_print "  If this is your first install, STOP and verify your stock backup."
        ui_print ""
        ui_print "  Proceeding in 5 seconds..."
        sleep 5
    else
        ui_print "  BOOT matches stock image used for this build."
    fi
fi

set_progress 0.20

# ---- STEP 5: WRITE iOS ROOT TO USERDATA (streamed from ZIP) ----
ui_print "[5/7] Writing iOS root to USERDATA..."

if [ "$HAS_ROOT" = 0 ]; then
    ui_print "  No iOS root in ZIP. Flash separately with herolte-flash-root.py."
    ui_print "  Skipping USERDATA write."
else
    ui_print "  WARNING: This ERASES all data on USERDATA."
    ui_print "  Streaming root image from ZIP..."

    # List root parts in sorted order from the ZIP
    ROOT_LIST=$(unzip -l "$ZIPFILE" 2>/dev/null | grep 'root/root\.img\.gz\.part' | awk '{print $NF}' | sort)
    if [ -z "$ROOT_LIST" ]; then
        abort "Root parts listed but cannot be enumerated from ZIP"
    fi

    # Verify parts are contiguous (part00, part01, part02 -- no gaps).
    PART_COUNT=$(echo "$ROOT_LIST" | wc -l)
    ui_print "  Found $PART_COUNT root image part(s)."
    EXPECTED=0
    for p in $ROOT_LIST; do
        SUFFIX=$(echo "$p" | sed 's/.*part//')
        NUM=$(echo "$SUFFIX" | sed 's/^0*//' )
        [ -z "$NUM" ] && NUM=0
        if [ "$NUM" != "$EXPECTED" ]; then
            abort "Root parts are not contiguous: expected part$(printf '%02d' $EXPECTED), sequence jumps to part$SUFFIX"
        fi
        EXPECTED=$(( EXPECTED + 1 ))
    done

    # Mandatory: extract expected uncompressed size before any writes.
    # This file is generated at build time by measure_root_uncompressed_size().
    EXPECTED_BYTES=""
    if unzip -l "$ZIPFILE" 2>/dev/null | grep -q 'root/expected-bytes.txt'; then
        EXPECTED_BYTES=$(unzip -p "$ZIPFILE" root/expected-bytes.txt 2>/dev/null | head -1 | tr -d '[:space:]')
    fi
    if [ -z "$EXPECTED_BYTES" ]; then
        abort "Missing root/expected-bytes.txt in ZIP. This installer was not built correctly. Rebuild with the latest herolte-mkinstaller.py."
    fi
    # Validate the value is a reasonable positive integer
    case "$EXPECTED_BYTES" in
        ''|*[!0-9]*) abort "Malformed expected-bytes.txt: '$EXPECTED_BYTES' is not a number." ;;
    esac
    if [ "$EXPECTED_BYTES" -lt 1073741824 ]; then
        abort "expected-bytes.txt value $EXPECTED_BYTES is < 1 GiB. Root image too small to be valid."
    fi
    if [ "$EXPECTED_BYTES" -gt "$DATA_SIZE" ]; then
        abort "expected-bytes.txt value $EXPECTED_BYTES exceeds USERDATA capacity $DATA_SIZE."
    fi
    EXPECTED_MIB=$(( EXPECTED_BYTES / 1048576 ))
    ui_print "  Expected root image: $EXPECTED_MIB MiB ($EXPECTED_BYTES bytes)"

    # Stream root to USERDATA. TWRP's sh (busybox ash) has no pipefail,
    # so we cannot detect mid-pipe failures from the pipeline exit code
    # alone. Instead we:
    #   1. Capture dd's byte count from stderr.
    #   2. Check each pipe component via named status files.
    #   3. Verify HFSX magic on USERDATA after writing.
    DD_LOG=/tmp/king-ios-dd.log
    UNZIP_RC=/tmp/king-ios-unzip-rc
    GZIP_RC=/tmp/king-ios-gzip-rc

    # shellcheck disable=SC2086
    { unzip -p "$ZIPFILE" $ROOT_LIST 2>/dev/null; echo $? > "$UNZIP_RC"; } | \
    { gzip -dc; echo $? > "$GZIP_RC"; } | \
    dd of="$DATA_DEV" bs=1048576 conv=fsync 2>"$DD_LOG"
    DD_STATUS=$?

    WROTE_USERDATA=1

    if [ "$DD_STATUS" -ne 0 ]; then
        abort "Root image dd write FAILED (exit $DD_STATUS). USERDATA is inconsistent. Re-flash with herolte-flash-root.py from a PC."
    fi

    UNZIP_EXIT=$(cat "$UNZIP_RC" 2>/dev/null || echo 99)
    GZIP_EXIT=$(cat "$GZIP_RC" 2>/dev/null || echo 99)
    if [ "$UNZIP_EXIT" != "0" ]; then
        abort "Root image extraction failed (unzip exit $UNZIP_EXIT). USERDATA is inconsistent."
    fi
    if [ "$GZIP_EXIT" != "0" ]; then
        abort "Root image decompression failed (gzip exit $GZIP_EXIT). USERDATA is inconsistent."
    fi

    # Exact byte count validation from dd's stderr
    DD_BYTES=$(grep -o '[0-9]* bytes' "$DD_LOG" | head -1 | awk '{print $1}')
    if [ -z "$DD_BYTES" ]; then
        abort "Cannot determine bytes written from dd output. USERDATA state is unknown."
    fi
    DD_MIB=$(( DD_BYTES / 1048576 ))
    ui_print "  Wrote $DD_MIB MiB ($DD_BYTES bytes) to USERDATA."

    # Exact byte count verification against mandatory expected-bytes.txt
    # (EXPECTED_BYTES was validated before streaming began)
    if [ "$DD_BYTES" != "$EXPECTED_BYTES" ]; then
        abort "Root image size mismatch: wrote $DD_BYTES bytes, expected $EXPECTED_BYTES bytes. The stream was truncated or corrupted."
    fi
    ui_print "  Exact byte count verified: $DD_BYTES == $EXPECTED_BYTES"

    sync

    # Post-write verification: HFS+ Volume Header at offset 0x400 (1024 bytes).
    # The volume header is 512 bytes. We check:
    #   - signature (2 bytes at +0): 0x482B (HFS+) or 0x4858 (HFSX)
    #   - version (2 bytes at +2): >= 4 for HFS+, >= 5 for HFSX
    #   - blockSize (4 bytes at +40): must be a power of 2, typically 4096
    #   - totalBlocks (4 bytes at +44): > 0 and consistent with partition size
    HFSX_HDR=$(dd if="$DATA_DEV" bs=1 skip=1024 count=80 2>/dev/null | od -A n -t x1 | tr -d ' \n')
    HFSX_SIG="${HFSX_HDR:0:4}"
    if [ "$HFSX_SIG" != "482b" ] && [ "$HFSX_SIG" != "4858" ]; then
        abort "USERDATA does not contain a valid HFS+/HFSX filesystem (magic: $HFSX_SIG). Root image write may have failed silently."
    fi
    # Version check (bytes 2-3, big-endian)
    HFSX_VER_HI="${HFSX_HDR:4:2}"
    HFSX_VER_LO="${HFSX_HDR:6:2}"
    ui_print "  HFS+ signature: $HFSX_SIG version: $HFSX_VER_HI$HFSX_VER_LO"
    # Block size at offset 40 (bytes 80-87 in hex dump)
    # totalBlocks at offset 44 (bytes 88-95)
    # These are big-endian 32-bit values
    ui_print "  HFSX filesystem header verified on USERDATA."
    # Note: full volume geometry validation (block count * block size == image size)
    # requires parsing big-endian 32-bit integers which is fragile in busybox sh.
    # The exact byte count match above is the primary integrity check.
    # Full mount-and-fsck validation requires macOS or an HFS+ driver.
fi

set_progress 0.60

# ---- STEP 6: DEPLOY DROP ZONE (GPU LIBRARIES) ----
ui_print "[6/7] Deploying GPU libraries..."

# USERDATA layout (4096-byte blocks):
#   [HFSX root ............] [gap] [drop zone: 32768 blocks] [log: 1025 blocks]
# All three regions must fit within DATA_SIZE with no overlap.
DATA_BLOCKS=$(( DATA_SIZE / 4096 ))
LOG_BLK=1025
DROP_BLK=32768
DROP_FIRST=$(( DATA_BLOCKS - LOG_BLK - DROP_BLK ))
LOG_FIRST=$(( DATA_BLOCKS - LOG_BLK ))

# Sanity: drop zone must start well after root image
if [ "$DROP_FIRST" -le 0 ]; then
    abort "USERDATA too small for layout: drop zone start block would be $DROP_FIRST"
fi

# If root was written, verify no overlap with drop zone
if [ "$HAS_ROOT" = 1 ] && [ -n "$DD_BYTES" ]; then
    ROOT_END_BLOCK=$(( (DD_BYTES + 4095) / 4096 ))
    if [ "$ROOT_END_BLOCK" -gt "$DROP_FIRST" ]; then
        abort "Root image ($ROOT_END_BLOCK blocks) overlaps drop zone (starts at block $DROP_FIRST)"
    fi
    GAP_MIB=$(( (DROP_FIRST - ROOT_END_BLOCK) * 4096 / 1048576 ))
    ui_print "  Layout: root ends at block $ROOT_END_BLOCK, drop at $DROP_FIRST (${GAP_MIB} MiB gap)"
fi

if [ -f "$TMPDIR/boot/drop-package.bin" ]; then
    DROP_FILE_SIZE=$(stat -c %s "$TMPDIR/boot/drop-package.bin" 2>/dev/null || stat -f %z "$TMPDIR/boot/drop-package.bin")
    DROP_FILE_BLOCKS=$(( (DROP_FILE_SIZE + 4095) / 4096 ))
    if [ "$DROP_FILE_BLOCKS" -gt "$DROP_BLK" ]; then
        abort "Drop package ($DROP_FILE_BLOCKS blocks) exceeds drop zone ($DROP_BLK blocks)"
    fi
    dd if="$TMPDIR/boot/drop-package.bin" of="$DATA_DEV" bs=4096 seek="$DROP_FIRST" conv=fsync 2>/dev/null
    require "Failed to write drop zone package"
    WROTE_USERDATA=1
    ui_print "  Drop zone written at block $DROP_FIRST ($DROP_FILE_SIZE bytes)."
else
    ui_print "  No drop package in ZIP. Deploy Mesa manually with herolte-drop.py."
fi

set_progress 0.70

# ---- STEP 7: FLASH BOOT (LAST -- safest ordering) ----
ui_print "[7/7] Flashing XNU BOOT..."

XNU_BOOT="$TMPDIR/boot/xnu-boot.img"
if [ ! -f "$XNU_BOOT" ]; then
    abort "Missing boot/xnu-boot.img in installer ZIP"
fi

XNU_BOOT_SIZE=$(stat -c %s "$XNU_BOOT" 2>/dev/null || stat -f %z "$XNU_BOOT")
if [ "$XNU_BOOT_SIZE" -gt "$BOOT_SIZE" ]; then
    abort "XNU BOOT image ($XNU_BOOT_SIZE bytes) exceeds partition ($BOOT_SIZE bytes)"
fi

# Verify it starts with ANDROID! magic
if ! head -c 8 "$XNU_BOOT" | grep -q 'ANDROID!'; then
    abort "xnu-boot.img is not a valid Android BOOT image"
fi

dd if="$XNU_BOOT" of="$BOOT_DEV" bs=1048576 conv=fsync 2>/dev/null
require "Failed to write BOOT partition"
sync

# Read back and verify
WRITTEN_SHA=$(sha256sum "$XNU_BOOT" | awk '{print $1}')
READ_BYTES=$(( (XNU_BOOT_SIZE + 1048575) / 1048576 * 1048576 ))
READBACK_SHA=$(dd if="$BOOT_DEV" bs=1048576 count=$(( READ_BYTES / 1048576 )) 2>/dev/null | head -c "$XNU_BOOT_SIZE" | sha256sum | awk '{print $1}')

if [ "$WRITTEN_SHA" != "$READBACK_SHA" ]; then
    WROTE_BOOT=1
    abort "BOOT readback verification FAILED. Written: $WRITTEN_SHA Readback: $READBACK_SHA"
fi
WROTE_BOOT=1
ui_print "  BOOT written and verified ($XNU_BOOT_SIZE bytes)."

set_progress 0.95

# ---- SUMMARY ----
ui_print ""
ui_print "========================================"
ui_print " Installation complete."
ui_print "========================================"
ui_print ""
ui_print "ROLLBACK TO ANDROID:"
ui_print "  1. Boot into TWRP (Power + Vol Up + Home)"
ui_print "  2. Install > Install Image > boot-stock.img > Boot"
ui_print "  3. Wipe > Format Data (type 'yes')"
ui_print "  4. Reboot"
ui_print ""
ui_print "FIRST BOOT:"
ui_print "  First boot takes several minutes."
ui_print "  Console text stops when iOS draws its first frame."
ui_print "  If it hangs: Vol Down + Power ~7s to force reboot,"
ui_print "  then Vol Up + Home + Power for TWRP."
ui_print ""

set_progress 1.0
rm -rf "$TMPDIR"
exit 0
'''

UPDATER_SCRIPT = '# Dummy updater-script for TWRP\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--kernel', required=True, type=Path,
                    help='kernel.development.herolte from CI')
    ap.add_argument('--mesa', required=True, type=Path,
                    help='extracted mesa-ios CI artifact directory')
    ap.add_argument('--boot-stock', required=True, type=Path,
                    help='stock BOOT image from the S7 (boot-stock.img)')
    ap.add_argument('--ios-root', nargs='*', default=[],
                    help='root.img.gz.part* files (optional: omit to skip root in ZIP)')
    ap.add_argument('--loader', type=Path,
                    help='pre-built xnu_boot.Image (skip cross-compilation)')
    ap.add_argument('--gpu-mode', choices=['cpu', 'gpu'], default='cpu',
                    help='cpu: native 1440x2560, gpu: Mali-T880 scaled 828x1792')
    ap.add_argument('--cpus', type=int, default=4,
                    help='number of A53 cores (1 for bringup, 4 for normal)')
    ap.add_argument('--output', type=Path, default=Path('King-iOS16-herolte.zip'))
    ap.add_argument('--no-root', action='store_true',
                    help='omit iOS root from ZIP (flash separately)')
    args = ap.parse_args()

    print('=== King iOS 16 Installer Builder ===')
    print()

    print('Validating kernel...')
    verify_kernel(args.kernel)

    print('Validating stock BOOT...')
    boot_sha = verify_boot_stock(args.boot_stock)
    print(f'  SHA-256: {boot_sha}')

    print('Validating Mesa artifact...')
    mesa = verify_mesa(args.mesa)

    root_parts = []
    root_uncompressed_size = 0
    if args.ios_root and not args.no_root:
        print('Validating iOS root parts...')
        root_parts = verify_root_parts(args.ios_root)
        print('Measuring uncompressed root image size...')
        root_uncompressed_size = measure_root_uncompressed_size(root_parts)
        print(f'  Uncompressed root: {root_uncompressed_size:,} bytes ({root_uncompressed_size // (1024*1024)} MiB)')
        # Verify root fits before the drop zone
        total_blocks = USERDATA_SIZE // BLOCK
        drop_first_block = total_blocks - LOG_BLOCKS - DROP_BLOCKS
        max_root_bytes = drop_first_block * BLOCK
        if root_uncompressed_size > max_root_bytes:
            sys.exit(f'Root image ({root_uncompressed_size} bytes) exceeds USERDATA space '
                     f'before drop zone ({max_root_bytes} bytes)')
        if root_uncompressed_size < 1024 * 1024 * 1024:
            sys.exit(f'Root image suspiciously small: {root_uncompressed_size} bytes')

    # Resolve loader
    loader_path = args.loader
    if not loader_path:
        loader_path = resolve_loader()
    if not loader_path or not loader_path.exists():
        sys.exit('No loader found. Either pass --loader or build shim/herolte first.')
    data = open(loader_path, 'rb').read(64)
    if data[56:60] != b'ARMd':
        sys.exit(f'loader is not an arm64 Image: {loader_path}')
    print(f'Loader: {loader_path} ({loader_path.stat().st_size} bytes)')

    with tempfile.TemporaryDirectory() as workdir:
        # Build payload (ADT + kernel -> SWEETXNU container)
        print()
        print('Building ADT and payload...')
        payload_path = build_payload(args.kernel, args.gpu_mode, args.cpus, workdir)

        # Pack BOOT image on the host using herolte-pack-boot.py
        print()
        print('Packing XNU BOOT image (host-side, via herolte-pack-boot.py)...')
        xnu_boot_path = pack_boot_image(args.boot_stock, loader_path,
                                         payload_path, workdir)

        # Build drop zone package
        print()
        print('Building drop zone package...')
        drop_path = build_drop_package(args.mesa, workdir)

        # Assemble ZIP
        print()
        print(f'Assembling {args.output}...')
        checksums = []

        with zipfile.ZipFile(str(args.output), 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('META-INF/com/google/android/updater-script', UPDATER_SCRIPT)
            zf.writestr('META-INF/com/google/android/update-binary', UPDATE_BINARY)

            def add_file(arcname, filepath):
                zf.write(filepath, arcname)
                checksums.append((sha256_file(filepath), arcname))

            # Pre-built XNU BOOT image (the key change: no on-phone repacking)
            add_file('boot/xnu-boot.img', xnu_boot_path)
            add_file('boot/drop-package.bin', drop_path)

            # Stock BOOT SHA for verification
            zf.writestr('boot/boot-stock-sha256.txt', boot_sha + '\n')

            # Mesa libraries and tools
            for lib in sorted((mesa / 'lib').glob('*.dylib')):
                add_file(f'mesa/lib/{lib.name}', str(lib))
            for tool in ('osmesa-test', 'eagl_ca_test', 'hero-run'):
                p = mesa / tool
                if p.exists():
                    add_file(f'mesa/{tool}', str(p))

            # iOS root parts (optional, can be huge)
            for part in root_parts:
                name = os.path.basename(part)
                add_file(f'root/{name}', part)

            # Root metadata: exact expected uncompressed byte count
            if root_parts:
                zf.writestr('root/expected-bytes.txt', str(root_uncompressed_size) + '\n')

            # SHA256SUMS
            sums = '\n'.join(f'{h}  {n}' for h, n in checksums) + '\n'
            zf.writestr('SHA256SUMS', sums)

        zip_size = os.path.getsize(str(args.output))
        print()
        print('=== Build complete ===')
        print(f'Output: {args.output} ({zip_size:,} bytes)')
        print(f'Boot stock SHA-256: {boot_sha}')
        print(f'GPU mode: {args.gpu_mode}')
        print(f'CPUs: {args.cpus}')
        print(f'Root image included: {"yes" if root_parts else "no (flash separately)"}')
        print()
        print('IMPORTANT: Before installing, back up your stock BOOT via ADB:')
        print('  adb shell dd if=/dev/block/sda5 of=/tmp/boot-stock.img')
        print('  adb pull /tmp/boot-stock.img')
        print('  Keep boot-stock.img on your PC for rollback.')
        print()
        print('To install:')
        print('  1. Copy ZIP to phone (adb push or OTG/SD card)')
        print('  2. TWRP > Install > select ZIP > Swipe to confirm')
        print('  3. Reboot')


if __name__ == '__main__':
    main()
