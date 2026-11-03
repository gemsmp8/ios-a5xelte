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

# Drop zone constants (must match herolte-drop.py and bsd/dev/herolte_drop.c)
BLOCK = 4096
LOG_BLOCKS = 1 + (4 << 20) // BLOCK
DROP_BLOCKS = 32768


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

# If root parts exist, check if the ZIP is on USERDATA (internal storage).
# On the S7, /sdcard /data /data/media all live on USERDATA. If we write
# USERDATA while reading the ZIP from it, we destroy the ZIP mid-install.
if [ "$HAS_ROOT" = 1 ]; then
    ZIP_REAL=$(readlink -f "$ZIPFILE" 2>/dev/null || echo "$ZIPFILE")
    case "$ZIP_REAL" in
        /sdcard/*|/data/*|/storage/emulated/*|/storage/sdcard0/*|/storage/0/*)
            abort "ZIP is on internal storage (USERDATA). Writing the iOS root would destroy the ZIP mid-install. Copy the ZIP to an external SD card or OTG drive and install from there."
            ;;
    esac
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

    # Report bytes written (from dd's stderr)
    DD_BYTES=$(grep -o '[0-9]* bytes' "$DD_LOG" | head -1 | awk '{print $1}')
    if [ -n "$DD_BYTES" ]; then
        DD_MIB=$(( DD_BYTES / 1048576 ))
        ui_print "  Wrote $DD_MIB MiB to USERDATA."
        if [ "$DD_MIB" -lt 1024 ]; then
            abort "Root image suspiciously small ($DD_MIB MiB). Expected ~20480 MiB."
        fi
    fi

    sync

    # Post-write verification: check HFSX signature at offset 0x400.
    # HFS+ magic = 0x482B, HFSX (case-sensitive) version >= 5.
    HFSX_SIG=$(dd if="$DATA_DEV" bs=1 skip=1024 count=2 2>/dev/null | od -A n -t x1 | tr -d ' ')
    if [ "$HFSX_SIG" != "482b" ]; then
        abort "USERDATA does not contain a valid HFS+/HFSX filesystem (magic: $HFSX_SIG). Root image write may have failed silently."
    fi
    ui_print "  HFSX filesystem signature verified on USERDATA."
fi

set_progress 0.60

# ---- STEP 6: DEPLOY DROP ZONE (GPU LIBRARIES) ----
ui_print "[6/7] Deploying GPU libraries..."

DROP_FIRST=$(( DATA_SIZE / 4096 - (1 + 4194304 / 4096) - 32768 ))

if [ -f "$TMPDIR/boot/drop-package.bin" ]; then
    dd if="$TMPDIR/boot/drop-package.bin" of="$DATA_DEV" bs=4096 seek="$DROP_FIRST" conv=fsync 2>/dev/null
    require "Failed to write drop zone package"
    WROTE_USERDATA=1
    ui_print "  Drop zone written at block $DROP_FIRST."
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
    if args.ios_root and not args.no_root:
        print('Validating iOS root parts...')
        root_parts = verify_root_parts(args.ios_root)

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
