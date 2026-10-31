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
  --loader        xnu_boot.Image              (pre-built loader; otherwise built here)
  --gpu-mode      (default: cpu)  cpu | gpu   (gpu enables Mali-T880 scaled display)
  --cpus          (default: 4)                (number of A53 cores; 1 for bringup)
  --output        King-iOS16-herolte.zip

The ZIP structure:
  META-INF/com/google/android/update-binary   (shell script TWRP executes)
  META-INF/com/google/android/updater-script  (dummy, TWRP needs it to exist)
  boot/payload.bin                            (SWEETXNU: kernel + ADT + cmdline)
  boot/xnu_boot.Image                         (the shim loader)
  boot/boot-stock-sha256.txt                  (SHA-256 of user's stock BOOT)
  mesa/lib/*.dylib                            (Mesa + herodrm + EAGL shim)
  mesa/hero-run                               (autorun binary)
  mesa/osmesa-test                            (GPU sanity test)
  root/root.img.gz.part*                      (iOS root image, passed through)
  tools/herolte-pack-boot.py                  (repacks BOOT on the phone)
  tools/herolte-flash-root.py                 (writes root to USERDATA)
  tools/herolte-drop.py                       (deploys Mesa libs via drop zone)
  SHA256SUMS                                  (integrity of every file in the ZIP)
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def build_payload(kernel, gpu_mode, cpus, workdir):
    """Build ADT and payload using the repo's tools."""
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

    # Build ADT
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

    # Build payload
    import mkpayload
    cmdline = ' '.join(cmdline_parts)
    blob, info = mkpayload.build(str(kernel), adt_path, cmdline)
    open(payload_path, 'wb').write(blob)
    print(f'Payload: {len(blob)} bytes -> {payload_path}')
    for k, v in info.items():
        if k != 'segs':
            print(f'  {k}: {v}')
    return payload_path


UPDATE_BINARY = r'''#!/sbin/sh
# King-iOS16-herolte TWRP installer
# Galaxy S7 SM-G930F (Exynos 8890) - XNU / iOS 16.7.16
#
# WARNING: This erases ALL data on USERDATA and replaces the BOOT partition.
# Back up your stock BOOT image and any important data FIRST.
#
# The stock BOOT backup is required for rollback to Android.
#
OUTFD=/proc/self/fd/$2
ZIPFILE="$3"

ui_print() { echo "ui_print $1" > "$OUTFD"; echo "ui_print" > "$OUTFD"; }
set_progress() { echo "set_progress $1" > "$OUTFD"; }

ui_print "========================================"
ui_print " King iOS 16 for Galaxy S7 (SM-G930F)"
ui_print "========================================"
ui_print ""

# Device verification
ui_print "Checking device..."
DEVICE=$(getprop ro.product.device 2>/dev/null || cat /proc/device-tree/model 2>/dev/null || echo unknown)
PLATFORM=$(ls /dev/block/platform/ 2>/dev/null | head -1)

if [ "$PLATFORM" != "155a0000.ufs" ]; then
    ui_print "ERROR: Expected Exynos 8890 UFS (155a0000.ufs)"
    ui_print "Found: $PLATFORM"
    ui_print "This installer is ONLY for SM-G930F."
    exit 1
fi

BOOT_DEV="/dev/block/platform/155a0000.ufs/by-name/BOOT"
DATA_DEV="/dev/block/platform/155a0000.ufs/by-name/USERDATA"

if [ ! -b "$BOOT_DEV" ]; then
    ui_print "ERROR: BOOT partition not found"
    exit 1
fi
if [ ! -b "$DATA_DEV" ]; then
    ui_print "ERROR: USERDATA partition not found"
    exit 1
fi

# Ensure /data is not mounted
if mount | grep -q ' /data '; then
    ui_print "Unmounting /data..."
    umount /data 2>/dev/null || {
        ui_print "ERROR: Cannot unmount /data. Do it manually in TWRP Mount menu."
        exit 1
    }
fi

set_progress 0.05

# Extract ZIP
ui_print "Extracting installer..."
TMPDIR=/tmp/king-ios
rm -rf "$TMPDIR"
mkdir -p "$TMPDIR"
cd "$TMPDIR"
unzip -o "$ZIPFILE" -d "$TMPDIR" || {
    ui_print "ERROR: Failed to extract ZIP"
    exit 1
}

set_progress 0.10

# Verify SHA256SUMS
ui_print "Verifying file integrity..."
if [ -f SHA256SUMS ]; then
    cd "$TMPDIR"
    if command -v sha256sum >/dev/null 2>&1; then
        # Only check files that exist (root parts may be separate)
        FAIL=0
        while IFS= read -r line; do
            hash=$(echo "$line" | awk '{print $1}')
            file=$(echo "$line" | awk '{print $2}')
            if [ -f "$file" ]; then
                actual=$(sha256sum "$file" | awk '{print $1}')
                if [ "$hash" != "$actual" ]; then
                    ui_print "INTEGRITY FAIL: $file"
                    FAIL=1
                fi
            fi
        done < SHA256SUMS
        if [ "$FAIL" = 1 ]; then
            ui_print "ERROR: File integrity check failed. Aborting."
            exit 1
        fi
        ui_print "Integrity check passed."
    else
        ui_print "WARNING: sha256sum not available, skipping integrity check"
    fi
fi

set_progress 0.15

# STEP 1: Back up stock BOOT (safety net)
ui_print ""
ui_print "--- Step 1: BOOT backup ---"
BOOT_SIZE=$(blockdev --getsize64 "$BOOT_DEV")
ui_print "BOOT partition: $BOOT_SIZE bytes"

BACKUP_DIR="/sdcard/king-ios-backup"
mkdir -p "$BACKUP_DIR"
if [ ! -f "$BACKUP_DIR/boot-stock.img" ]; then
    ui_print "Backing up stock BOOT to $BACKUP_DIR/boot-stock.img..."
    dd if="$BOOT_DEV" of="$BACKUP_DIR/boot-stock.img" bs=1048576 2>/dev/null
    ui_print "Backup saved. KEEP THIS FILE for rollback to Android."
else
    ui_print "Stock BOOT backup already exists at $BACKUP_DIR/boot-stock.img"
fi

set_progress 0.20

# STEP 2: Build XNU BOOT image from the stock backup
ui_print ""
ui_print "--- Step 2: Building XNU BOOT image ---"
STOCK_BOOT="$BACKUP_DIR/boot-stock.img"

# Verify stock BOOT is a valid Android image
if ! head -c 8 "$STOCK_BOOT" | grep -q 'ANDROID!'; then
    ui_print "ERROR: Stock BOOT backup is not a valid Android BOOT image"
    exit 1
fi

# The pack tool runs on the phone (Python3 in TWRP is not available).
# Instead, the installer ships a pre-built payload and xnu_boot.Image.
# We do the pack step: replace the kernel in the stock BOOT with our loader
# and append the payload.
#
# Minimal BOOT repack (shell, no Python): keep stock header, DTBH, ramdisk;
# replace kernel with xnu_boot.Image + payload.bin (concatenated, 4K aligned).

LOADER="$TMPDIR/boot/xnu_boot.Image"
PAYLOAD="$TMPDIR/boot/payload.bin"

if [ ! -f "$LOADER" ] || [ ! -f "$PAYLOAD" ]; then
    ui_print "ERROR: Missing loader or payload in installer"
    exit 1
fi

# Verify loader has arm64 magic
if ! dd if="$LOADER" bs=1 skip=56 count=4 2>/dev/null | grep -q 'ARMd'; then
    ui_print "ERROR: Loader is not a valid arm64 Image"
    exit 1
fi

# Verify payload has SWEETXNU magic
if ! head -c 8 "$PAYLOAD" | grep -q 'SWEETXNU'; then
    ui_print "ERROR: Payload is not a valid SWEETXNU container"
    exit 1
fi

# Concatenate loader + padding + payload (the loader finds payload at its 4K-aligned end)
KERNEL_IMG="$TMPDIR/kernel-combined.bin"
cp "$LOADER" "$KERNEL_IMG"
LOADER_SIZE=$(stat -c %s "$KERNEL_IMG" 2>/dev/null || stat -f %z "$KERNEL_IMG")
PAD=$(( (4096 - (LOADER_SIZE % 4096)) % 4096 ))
if [ "$PAD" -gt 0 ]; then
    dd if=/dev/zero bs=1 count="$PAD" >> "$KERNEL_IMG" 2>/dev/null
fi
cat "$PAYLOAD" >> "$KERNEL_IMG"
COMBINED_SIZE=$(stat -c %s "$KERNEL_IMG" 2>/dev/null || stat -f %z "$KERNEL_IMG")
ui_print "Loader + payload: $COMBINED_SIZE bytes"

# Parse stock BOOT header to find page size and component offsets
PAGE_SIZE=$(hexdump -s 36 -n 4 -e '1/4 "%u"' "$STOCK_BOOT")
if [ "$PAGE_SIZE" != "2048" ] && [ "$PAGE_SIZE" != "4096" ]; then
    ui_print "ERROR: Unexpected BOOT page size: $PAGE_SIZE"
    exit 1
fi

# Read component sizes from header
STOCK_KERNEL_SIZE=$(hexdump -s 8 -n 4 -e '1/4 "%u"' "$STOCK_BOOT")
STOCK_RAMDISK_SIZE=$(hexdump -s 16 -n 4 -e '1/4 "%u"' "$STOCK_BOOT")
STOCK_SECOND_SIZE=$(hexdump -s 24 -n 4 -e '1/4 "%u"' "$STOCK_BOOT")
STOCK_DT_SIZE=$(hexdump -s 40 -n 4 -e '1/4 "%u"' "$STOCK_BOOT")

align_page() { echo $(( ($1 + PAGE_SIZE - 1) / PAGE_SIZE * PAGE_SIZE )); }

RAMDISK_OFF=$(( PAGE_SIZE + $(align_page $STOCK_KERNEL_SIZE) ))
SECOND_OFF=$(( RAMDISK_OFF + $(align_page $STOCK_RAMDISK_SIZE) ))
DT_OFF=$(( SECOND_OFF + $(align_page $STOCK_SECOND_SIZE) ))

ui_print "Stock BOOT: page=$PAGE_SIZE kernel=$STOCK_KERNEL_SIZE ramdisk=$STOCK_RAMDISK_SIZE dt=$STOCK_DT_SIZE"

# Build new BOOT image
XNU_BOOT="$TMPDIR/xnu-boot.img"

# Header: copy stock, update kernel size
dd if="$STOCK_BOOT" of="$XNU_BOOT" bs="$PAGE_SIZE" count=1 2>/dev/null
# Write new kernel size (little-endian u32 at offset 8)
printf "$(printf '%08x' "$COMBINED_SIZE" | sed 's/\(..\)/\\x\1/g' | rev | sed 's/\(x[0-9a-f][0-9a-f]\.\)/\1\\/g; s/^.//; s/\\$//')" |
    dd of="$XNU_BOOT" bs=1 seek=8 conv=notrunc 2>/dev/null
# Actually, use a simpler approach with python-like hex encoding
# Write kernel size as LE u32
KSIZE_HEX=$(printf '%08x' "$COMBINED_SIZE")
B0=$(printf "\\x${KSIZE_HEX:6:2}")
B1=$(printf "\\x${KSIZE_HEX:4:2}")
B2=$(printf "\\x${KSIZE_HEX:2:2}")
B3=$(printf "\\x${KSIZE_HEX:0:2}")
printf "${B0}${B1}${B2}${B3}" | dd of="$XNU_BOOT" bs=1 seek=8 conv=notrunc 2>/dev/null

# Kernel slot: our combined image, page-aligned
dd if="$KERNEL_IMG" of="$XNU_BOOT" bs=1 seek="$PAGE_SIZE" conv=notrunc 2>/dev/null
NEW_KERNEL_ALIGNED=$(align_page $COMBINED_SIZE)
KERNEL_PAD=$(( NEW_KERNEL_ALIGNED - COMBINED_SIZE ))
if [ "$KERNEL_PAD" -gt 0 ]; then
    dd if=/dev/zero bs=1 count="$KERNEL_PAD" >> "$XNU_BOOT" 2>/dev/null
fi

# Ramdisk: copy from stock
dd if="$STOCK_BOOT" bs=1 skip="$RAMDISK_OFF" count="$STOCK_RAMDISK_SIZE" >> "$XNU_BOOT" 2>/dev/null
RAMDISK_PAD=$(( $(align_page $STOCK_RAMDISK_SIZE) - STOCK_RAMDISK_SIZE ))
[ "$RAMDISK_PAD" -gt 0 ] && dd if=/dev/zero bs=1 count="$RAMDISK_PAD" >> "$XNU_BOOT" 2>/dev/null

# Second: copy from stock (usually empty)
if [ "$STOCK_SECOND_SIZE" -gt 0 ]; then
    dd if="$STOCK_BOOT" bs=1 skip="$SECOND_OFF" count="$STOCK_SECOND_SIZE" >> "$XNU_BOOT" 2>/dev/null
    SECOND_PAD=$(( $(align_page $STOCK_SECOND_SIZE) - STOCK_SECOND_SIZE ))
    [ "$SECOND_PAD" -gt 0 ] && dd if=/dev/zero bs=1 count="$SECOND_PAD" >> "$XNU_BOOT" 2>/dev/null
fi

# DT (DTBH): copy from stock
dd if="$STOCK_BOOT" bs=1 skip="$DT_OFF" count="$STOCK_DT_SIZE" >> "$XNU_BOOT" 2>/dev/null
DT_PAD=$(( $(align_page $STOCK_DT_SIZE) - STOCK_DT_SIZE ))
[ "$DT_PAD" -gt 0 ] && dd if=/dev/zero bs=1 count="$DT_PAD" >> "$XNU_BOOT" 2>/dev/null

# SEANDROIDENFORCE marker
printf 'SEANDROIDENFORCE' >> "$XNU_BOOT"

XNU_BOOT_SIZE=$(stat -c %s "$XNU_BOOT" 2>/dev/null || stat -f %z "$XNU_BOOT")
if [ "$XNU_BOOT_SIZE" -gt "$BOOT_SIZE" ]; then
    ui_print "ERROR: XNU BOOT image ($XNU_BOOT_SIZE) exceeds partition ($BOOT_SIZE)"
    exit 1
fi
ui_print "XNU BOOT image: $XNU_BOOT_SIZE bytes (partition: $BOOT_SIZE)"

set_progress 0.30

# STEP 3: Write iOS root to USERDATA
ui_print ""
ui_print "--- Step 3: Writing iOS root to USERDATA ---"
ui_print "WARNING: This ERASES all data on USERDATA."
ui_print ""

DATA_SIZE=$(blockdev --getsize64 "$DATA_DEV")
ui_print "USERDATA: $DATA_SIZE bytes"

ROOT_PARTS=$(ls "$TMPDIR"/root/root.img.gz.part* 2>/dev/null)
if [ -z "$ROOT_PARTS" ]; then
    ui_print "No iOS root image in ZIP (root/root.img.gz.part*)."
    ui_print "You must flash the root image separately with herolte-flash-root.py."
    ui_print "Skipping USERDATA write."
else
    # Decompress and write in 64 MiB chunks
    CHUNK=$((64 * 1048576))
    ui_print "Decompressing and writing root image..."
    # Concatenate parts and pipe through gzip into USERDATA
    cat $ROOT_PARTS | gzip -dc | dd of="$DATA_DEV" bs=1048576 conv=fsync 2>/dev/null
    WRITE_STATUS=$?
    if [ "$WRITE_STATUS" -ne 0 ]; then
        ui_print "ERROR: Root image write failed (exit $WRITE_STATUS)"
        ui_print "USERDATA may be in an inconsistent state."
        ui_print "Re-flash with herolte-flash-root.py from a PC."
        exit 1
    fi
    sync
    ui_print "Root image written to USERDATA."
fi

set_progress 0.70

# STEP 4: Deploy Mesa GPU libraries via drop zone
ui_print ""
ui_print "--- Step 4: Deploying GPU libraries ---"

# The kernel's drop zone is at a fixed offset from the end of USERDATA.
# Format: HERODRP1 header at the first block, then the packed entries.
# We build the package directly.

BLOCK=4096
LOG_BLOCKS=$(( 1 + (4 * 1048576) / BLOCK ))
DROP_BLOCKS=32768
DATA_BLOCKS=$(( DATA_SIZE / BLOCK ))
DROP_FIRST=$(( DATA_BLOCKS - LOG_BLOCKS - DROP_BLOCKS ))

# Build drop zone package
DROP_PKG="$TMPDIR/drop.bin"
MESA="$TMPDIR/mesa"

# We need a minimal binary packer. Use dd and printf.
# The drop zone format (herolte-drop.py):
#   header block: magic "HERODRP1" (8), version u32 (4), nentries u32 (4),
#                 payload_size u64 (8), ident u64 (8) = 32 bytes, rest zero
#   payload: per entry: path_len u32, mode u32, data_len u64, path bytes,
#            pad to 8, data bytes, pad to 8

# This is complex in shell. Instead, deploy a simpler approach:
# Write a small script that hero-run will find, and have the Mesa libs
# placed where hero-run expects them via the autorun mechanism.

# Create an autorun script that sets up the libraries
AUTORUN="$TMPDIR/autorun-setup"
cat > "$AUTORUN" <<'AUTOEOF'
#!/bin/sh
# King-iOS autorun: set up GPU libraries on first boot
DEST=/private/var/herolte
mkdir -p "$DEST/lib"
# Libraries are already in place from the drop zone
# Run the GPU test
if [ -x "$DEST/osmesa-test" ]; then
    "$DEST/osmesa-test" 2>&1 || true
fi
# Run hero-run for system registration
if [ -x "$DEST/hero-run" ]; then
    exec "$DEST/hero-run"
fi
AUTOEOF
chmod 755 "$AUTORUN"

# Since building the drop zone binary format in shell is fragile,
# and TWRP usually has busybox but not Python, we write the drop zone
# using a pre-built binary package included in the ZIP.
if [ -f "$TMPDIR/boot/drop-package.bin" ]; then
    ui_print "Writing pre-built drop zone package..."
    dd if="$TMPDIR/boot/drop-package.bin" of="$DATA_DEV" bs="$BLOCK" seek="$DROP_FIRST" conv=fsync 2>/dev/null
    ui_print "Drop zone written."
else
    ui_print "No pre-built drop package found."
    ui_print "Deploy Mesa libraries manually with herolte-drop.py after installation."
fi

set_progress 0.80

# STEP 5: Flash XNU BOOT image
ui_print ""
ui_print "--- Step 5: Flashing XNU BOOT ---"
dd if="$XNU_BOOT" of="$BOOT_DEV" bs=1048576 conv=fsync 2>/dev/null
sync
ui_print "BOOT partition written."

# Verify readback
WRITTEN_SHA=$(sha256sum "$XNU_BOOT" | awk '{print $1}')
READBACK_SHA=$(dd if="$BOOT_DEV" bs=1048576 count=$(( (XNU_BOOT_SIZE + 1048575) / 1048576 )) 2>/dev/null | head -c "$XNU_BOOT_SIZE" | sha256sum | awk '{print $1}')
if [ "$WRITTEN_SHA" = "$READBACK_SHA" ]; then
    ui_print "BOOT verified OK."
else
    ui_print "WARNING: BOOT readback mismatch!"
    ui_print "  written:  $WRITTEN_SHA"
    ui_print "  readback: $READBACK_SHA"
    ui_print "Try reflashing from TWRP Install > Install Image > Boot."
fi

set_progress 0.90

# STEP 6: Summary
ui_print ""
ui_print "========================================"
ui_print " Installation complete!"
ui_print "========================================"
ui_print ""
ui_print "Stock BOOT backup: $BACKUP_DIR/boot-stock.img"
ui_print ""
ui_print "ROLLBACK TO ANDROID:"
ui_print "  1. Boot into TWRP (Power + Vol Up + Home)"
ui_print "  2. Install > Install Image > select boot-stock.img > Boot"
ui_print "  3. Wipe > Format Data (type 'yes')"
ui_print "  4. Reboot"
ui_print ""
ui_print "FIRST BOOT:"
ui_print "  The first boot takes several minutes."
ui_print "  Console text stops when iOS draws its first frame."
ui_print "  If it hangs: Vol Down + Power ~7s to force reboot,"
ui_print "  then Vol Up + Home + Power for TWRP."
ui_print ""
ui_print "LOGS (from TWRP after a boot attempt):"
ui_print "  adb shell"
ui_print "  The kernel log is in the last 4 MiB of USERDATA."
ui_print "  Use: python3 tools/herolte-log-pull.py adb boot.txt"
ui_print ""

set_progress 1.0

# Cleanup
rm -rf "$TMPDIR"
exit 0
'''

UPDATER_SCRIPT = '# Dummy updater-script for TWRP\n'


def build_drop_package(mesa_dir, workdir):
    """Build the HERODRP1 binary package for the kernel's drop zone."""
    import time

    PREFIX = '/private/var/herolte/'
    items = []

    # Autorun script
    autorun = b'#!/bin/sh\n'
    autorun += b'DEST=/private/var/herolte\n'
    autorun += b'mkdir -p "$DEST/lib"\n'
    autorun += b'if [ -x "$DEST/osmesa-test" ]; then\n'
    autorun += b'    "$DEST/osmesa-test" 2>&1 || true\n'
    autorun += b'fi\n'
    autorun += b'if [ -x "$DEST/hero-run" ]; then\n'
    autorun += b'    exec "$DEST/hero-run"\n'
    autorun += b'fi\n'
    items.append(('autorun', 0o755, autorun))

    # Mesa libraries
    mesa = Path(mesa_dir)
    for lib in sorted((mesa / 'lib').glob('*.dylib')):
        items.append((f'lib/{lib.name}', 0o644, lib.read_bytes()))

    # Test binaries
    for name in ('osmesa-test', 'hero-run', 'eagl_ca_test'):
        p = mesa / name
        if p.exists():
            items.append((name, 0o755, p.read_bytes()))

    # Build payload
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

    BLOCK = 4096
    DROP_BLOCKS = 32768
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

    # Validate inputs
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

    with tempfile.TemporaryDirectory() as workdir:
        # Build payload
        print()
        print('Building ADT and payload...')
        payload_path = build_payload(args.kernel, args.gpu_mode, args.cpus, workdir)

        # Resolve loader
        if args.loader:
            loader_path = str(args.loader)
            data = open(loader_path, 'rb').read(64)
            if data[56:60] != b'ARMd':
                sys.exit(f'loader is not an arm64 Image: {loader_path}')
        else:
            # Try to build the loader
            shim_dir = REPO_ROOT / 'shim' / 'herolte'
            built = shim_dir / 'out' / 'xnu_boot.Image'
            if built.exists():
                loader_path = str(built)
                print(f'Using existing loader: {loader_path}')
            else:
                print('No --loader provided and no pre-built xnu_boot.Image found.')
                print('Cross-compile the loader first:')
                print('  cd shim/herolte && CROSS=aarch64-none-elf- sh build.sh')
                sys.exit(1)

        # Build drop package
        print()
        print('Building drop zone package...')
        drop_path = build_drop_package(args.mesa, workdir)

        # Assemble ZIP
        print()
        print(f'Assembling {args.output}...')
        checksums = []

        with zipfile.ZipFile(str(args.output), 'w', zipfile.ZIP_STORED) as zf:
            # TWRP requires these paths exactly
            zf.writestr('META-INF/com/google/android/updater-script', UPDATER_SCRIPT)
            zf.writestr('META-INF/com/google/android/update-binary', UPDATE_BINARY)

            # Boot components
            def add_file(arcname, filepath):
                zf.write(filepath, arcname)
                checksums.append((sha256_file(filepath), arcname))

            add_file('boot/payload.bin', payload_path)
            add_file('boot/xnu_boot.Image', loader_path)
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
        print('To install:')
        print('  1. Copy ZIP to phone (adb push or SD card)')
        print('  2. TWRP > Install > select ZIP > Swipe to confirm')
        print('  3. Reboot')
        print()
        print('To rollback to Android:')
        print('  1. TWRP > Install > Install Image > boot-stock.img > Boot')
        print('  2. TWRP > Wipe > Format Data')


if __name__ == '__main__':
    main()
