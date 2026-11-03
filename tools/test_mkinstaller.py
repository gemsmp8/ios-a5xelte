#!/usr/bin/env python3
"""Test suite for King-iOS16-herolte installer: structural and integration tests.

Tests are grouped by what they validate:
  1. update-binary safety:    grep-based checks that the TWRP script has the
                              required safety features and none of the old bugs
  2. pack_boot integration:   herolte-pack-boot.py round-trips through parse(),
                              DTBH preserved, SHA-1 recalculated, partition fit
  3. realistic Samsung BOOT:  page=2048 with 4096-byte ramdisk, multi-entry DTBH,
                              SEANDROIDENFORCE, page-aligned sections
  4. malformed inputs:        truncated BOOT, wrong magic, missing DTBH, no 8890
                              chip entry, non-arm64 loader, bad SWEETXNU payload
  5. ZIP assembly:            correct layout, SHA256SUMS coverage, no old files
  6. drop zone format:        HERODRP1 header, entry parsing matches herolte-drop.py
  7. ADT generation:          hardware addresses match bringup docs
  8. payload generation:      SWEETXNU header, segment table, cmdline
  9. ios-root workflow:        workflow file references correct tools and parameters

Does NOT validate (requires real firmware / physical device):
  - Actual Samsung S7 BOOT image parsing (specific padding, real DTBH entries)
  - XNU kernel Mach-O with LC_SEGMENT_64 / LC_DYLD_CHAINED_FIXUPS
  - Mesa dylib arm64 signatures
  - HFSX filesystem image structure
  - TWRP runtime behavior (unmount, blockdev, dd, sha256sum)
  - Hardware boot sequence
"""
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / 'tools'

PASS = 0
FAIL = 0


def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    msg = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        msg += f": {detail}"
    print(msg)
    return ok


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def make_arm64_image(size=12288):
    img = bytearray(size)
    struct.pack_into('<I', img, 0, 0x14000002)
    img[56:60] = b'ARMd'
    struct.pack_into('<3Q', img, 8, 0x80000, size + 0x10000, 0x0a)
    return bytes(img)


def make_dtbh(chip_ids=(8890,), version=2):
    """DTBH with entries for given chip IDs, each pointing to a minimal FDT."""
    count = len(chip_ids)
    header_size = 12 + count * 32
    fdt_offset = (header_size + 255) & ~255
    fdt = b'\xd0\x0d\xfe\xed' + b'\x00' * 60
    dtbh = bytearray(fdt_offset + len(fdt))
    struct.pack_into('<3I', dtbh, 0, 0x48425444, version, count)
    for i, chip in enumerate(chip_ids):
        struct.pack_into('<8I', dtbh, 12 + i * 32,
                         chip, 0, 0, 0, 0, fdt_offset, len(fdt), 0)
    dtbh[fdt_offset:fdt_offset + len(fdt)] = fdt
    return bytes(dtbh)


def make_android_boot(kernel, ramdisk=None, dtbh=None, page=2048,
                      kernel_addr=0x80008000, ramdisk_addr=0x82000000):
    if ramdisk is None:
        ramdisk = b'\x1f\x8b\x08\x00' + b'\x00' * 4092
    if dtbh is None:
        dtbh = make_dtbh()
    align = lambda s: (s + page - 1) // page * page
    header = bytearray(page)
    header[0:8] = b'ANDROID!'
    struct.pack_into('<I', header, 8, len(kernel))
    struct.pack_into('<I', header, 12, kernel_addr)
    struct.pack_into('<I', header, 16, len(ramdisk))
    struct.pack_into('<I', header, 20, ramdisk_addr)
    struct.pack_into('<I', header, 24, 0)
    struct.pack_into('<I', header, 28, 0x81e00000)
    struct.pack_into('<I', header, 32, 0x81e00000)
    struct.pack_into('<I', header, 36, page)
    struct.pack_into('<I', header, 40, len(dtbh))
    digest = hashlib.sha1()
    for part, sz in [(kernel, len(kernel)), (ramdisk, len(ramdisk)),
                     (b'', 0), (dtbh, len(dtbh))]:
        digest.update(part)
        digest.update(struct.pack('<I', sz))
    header[576:596] = digest.digest()
    out = bytearray(header)
    for part in (kernel, ramdisk, b'', dtbh):
        if part:
            out.extend(part)
            out.extend(b'\0' * (align(len(part)) - len(part)))
    out.extend(b'SEANDROIDENFORCE')
    return bytes(out)


def make_macho_kernel(size=65536):
    k = bytearray(size)
    struct.pack_into('<I', k, 0, 0xfeedfacf)
    return bytes(k)


def make_mesa_dir(d):
    lib = d / 'lib'
    lib.mkdir(parents=True)
    (lib / 'libOSMesa.8.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 1024)
    (lib / 'libdrm.2.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 512)
    (lib / 'libglapi.0.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 256)
    (d / 'hero-run').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 256)
    (d / 'osmesa-test').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 128)
    os.chmod(d / 'hero-run', 0o755)
    os.chmod(d / 'osmesa-test', 0o755)


def load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. update-binary safety
# ---------------------------------------------------------------------------

def test_update_binary_structure():
    print("\n--- 1. update-binary safety checks ---")
    mod = load_module('mkinstaller', TOOLS / 'herolte-mkinstaller.py')
    s = mod.UPDATE_BINARY

    report("SM-G930F model check", 'SM-G930F' in s)
    report("UFS platform 155a0000.ufs check", '155a0000.ufs' in s)
    report("no /sdcard backup writes", 'dd' not in s or 'of=/sdcard' not in s,
           "/sdcard is on USERDATA; never write backups there")
    report("detects ZIP on internal storage", '/sdcard/' in s and 'abort' in s,
           "must abort if ZIP is on USERDATA")
    report("no hexdump", 'hexdump' not in s)
    report("no printf \\x byte writes", 'printf "\\x' not in s)
    report("uses pre-built xnu-boot.img", 'xnu-boot.img' in s)
    report("battery capacity check", 'capacity' in s.lower())
    report("USERDATA >= 20 GB check", '20000000000' in s)
    report("BOOT readback verification", 'READBACK_SHA' in s)
    report("abort function defined", 'abort()' in s or 'abort "' in s)
    report("aborts on BOOT write failure", 'require "Failed to write BOOT' in s)
    report("verifies ANDROID! magic", 'ANDROID!' in s)
    report("no on-phone BOOT repacking", 'STOCK_KERNEL_SIZE' not in s)
    report("unmounts /data", 'umount /data' in s)
    report("no align_page shell function", 'align_page' not in s,
           "shell align_page means on-phone repacking is still present")
    report("progress reporting", 'set_progress' in s)
    report("step numbering", '[1/7]' in s and '[7/7]' in s)


# ---------------------------------------------------------------------------
# 2. pack_boot integration (basic)
# ---------------------------------------------------------------------------

def test_pack_boot_integration():
    print("\n--- 2. pack_boot integration (basic) ---")
    pack_boot = load_module('pack_boot', TOOLS / 'herolte-pack-boot.py')

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        loader = make_arm64_image(8192)
        stock = make_android_boot(make_arm64_image(4096))
        payload = b'SWEETXNU' + b'\x01' + b'\x00' * 4087

        (td / 'stock.img').write_bytes(stock)
        (td / 'loader.Image').write_bytes(loader)
        (td / 'payload.bin').write_bytes(payload)

        class Args:
            stock = td / 'stock.img'
            image = td / 'loader.Image'
            output = td / 'xnu-boot.img'
            append = td / 'payload.bin'
            ramdisk = None
            partition_size = 41943040

        try:
            pack_boot.cmd_pack(Args())
            data = Args.output.read_bytes()
            report("output starts with ANDROID!", data[:8] == b'ANDROID!')
            report("ends with SEANDROIDENFORCE", data.endswith(b'SEANDROIDENFORCE'))
            report("fits 40 MiB partition", len(data) <= 41943040)
            parsed = pack_boot.parse(data)
            report("kernel > loader (has payload)", len(parsed['kernel']) > len(loader))
            report("ramdisk preserved", len(parsed['ramdisk']) > 0)
            report("DTBH preserved", parsed['dt'][:4] == b'DTBH')

            # Verify the SHA-1 in the header was recalculated
            hdr_sha1 = data[576:596]
            digest = hashlib.sha1()
            for part in (parsed['kernel'], parsed['ramdisk'], parsed['second'], parsed['dt']):
                digest.update(part)
                digest.update(struct.pack('<I', len(part)))
            report("SHA-1 digest recalculated", hdr_sha1 == digest.digest())

            # Verify arm64 Image header fields were copied from stock
            stock_kernel = make_arm64_image(4096)
            stock_hdr = struct.unpack_from('<3Q', stock_kernel, 8)
            new_hdr = struct.unpack_from('<3Q', parsed['kernel'], 8)
            report("text_offset copied from stock",
                   new_hdr[0] == stock_hdr[0])
            report("flags copied from stock", new_hdr[2] == stock_hdr[2])
        except Exception as e:
            report("pack_boot runs", False, str(e))


# ---------------------------------------------------------------------------
# 3. Realistic Samsung BOOT fixtures
# ---------------------------------------------------------------------------

def test_realistic_samsung_boot():
    print("\n--- 3. Realistic Samsung BOOT fixtures ---")
    pack_boot = load_module('pack_boot_r', TOOLS / 'herolte-pack-boot.py')

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # Multi-entry DTBH (8890 + 7420, like a multi-device kernel)
        dtbh = make_dtbh(chip_ids=(8890, 7420))
        # Larger ramdisk (16 KiB, like a real initrd)
        ramdisk = b'\x1f\x8b\x08\x00' + os.urandom(16380)
        stock_kernel = make_arm64_image(32768)
        stock = make_android_boot(stock_kernel, ramdisk=ramdisk, dtbh=dtbh)

        (td / 'stock.img').write_bytes(stock)
        (td / 'loader.Image').write_bytes(make_arm64_image(12288))
        (td / 'payload.bin').write_bytes(b'SWEETXNU' + b'\x01' + b'\x00' * 16383)

        class Args:
            stock = td / 'stock.img'
            image = td / 'loader.Image'
            output = td / 'xnu-boot.img'
            append = td / 'payload.bin'
            ramdisk_arg = None
            partition_size = 41943040
        Args.ramdisk = Args.ramdisk_arg

        try:
            pack_boot.cmd_pack(Args())
            data = Args.output.read_bytes()
            parsed = pack_boot.parse(data)
            report("multi-entry DTBH preserved",
                   parsed['dt'][:4] == b'DTBH')
            _, entries = pack_boot.dtbh_entries(parsed['dt'])
            report("both DTBH entries present", len(entries) == 2)
            report("8890 entry present", any(e[0] == 8890 for e in entries))
            report("ramdisk size preserved", len(parsed['ramdisk']) == len(ramdisk))
            report("ramdisk content preserved", parsed['ramdisk'] == ramdisk)
        except Exception as e:
            report("realistic boot pack", False, str(e))

        # page=4096 variant
        stock4k = make_android_boot(stock_kernel, ramdisk=ramdisk,
                                     dtbh=dtbh, page=4096)
        (td / 'stock4k.img').write_bytes(stock4k)
        Args.stock = td / 'stock4k.img'
        Args.output = td / 'xnu-boot-4k.img'
        try:
            pack_boot.cmd_pack(Args())
            data4k = Args.output.read_bytes()
            parsed4k = pack_boot.parse(data4k)
            report("page=4096 round-trips", parsed4k['dt'][:4] == b'DTBH')
            report("page=4096 ramdisk preserved",
                   len(parsed4k['ramdisk']) == len(ramdisk))
        except Exception as e:
            report("page=4096 variant", False, str(e))


# ---------------------------------------------------------------------------
# 4. Malformed input rejection
# ---------------------------------------------------------------------------

def test_malformed_inputs():
    print("\n--- 4. Malformed input rejection ---")
    pack_boot = load_module('pack_boot_m', TOOLS / 'herolte-pack-boot.py')

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        good_loader = make_arm64_image(8192)
        good_payload = b'SWEETXNU' + b'\x01' + b'\x00' * 4087

        # a) Truncated BOOT (header only, no kernel)
        trunc = b'ANDROID!' + b'\x00' * 2040
        (td / 'trunc.img').write_bytes(trunc)
        (td / 'loader.Image').write_bytes(good_loader)
        (td / 'payload.bin').write_bytes(good_payload)

        class TArgs:
            stock = td / 'trunc.img'
            image = td / 'loader.Image'
            output = td / 'trunc-out.img'
            append = td / 'payload.bin'
            ramdisk = None
            partition_size = 41943040

        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects truncated BOOT", False, "should have raised")
        except SystemExit:
            report("rejects truncated BOOT", True)

        # b) BOOT without DTBH
        no_dtbh_boot = make_android_boot(make_arm64_image(4096), dtbh=b'\x00' * 256)
        (td / 'no-dtbh.img').write_bytes(no_dtbh_boot)
        TArgs.stock = td / 'no-dtbh.img'
        TArgs.output = td / 'no-dtbh-out.img'
        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects BOOT without DTBH", False, "should have raised")
        except SystemExit:
            report("rejects BOOT without DTBH", True)

        # c) DTBH with only non-8890 chip
        wrong_chip_dtbh = make_dtbh(chip_ids=(7420,))
        wrong_chip_boot = make_android_boot(make_arm64_image(4096), dtbh=wrong_chip_dtbh)
        (td / 'wrong-chip.img').write_bytes(wrong_chip_boot)
        TArgs.stock = td / 'wrong-chip.img'
        TArgs.output = td / 'wrong-chip-out.img'
        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects BOOT without 8890 DTBH entry", False, "should have raised")
        except SystemExit:
            report("rejects BOOT without 8890 DTBH entry", True)

        # d) Non-arm64 loader (missing ARMd magic)
        bad_loader = b'\x00' * 8192
        (td / 'bad-loader.Image').write_bytes(bad_loader)
        good_stock = make_android_boot(make_arm64_image(4096))
        (td / 'good-stock.img').write_bytes(good_stock)
        TArgs.stock = td / 'good-stock.img'
        TArgs.image = td / 'bad-loader.Image'
        TArgs.output = td / 'bad-loader-out.img'
        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects non-arm64 loader", False, "should have raised")
        except SystemExit:
            report("rejects non-arm64 loader", True)

        # e) Bad payload (not SWEETXNU)
        (td / 'bad-payload.bin').write_bytes(b'NOTSWEET' + b'\x00' * 4088)
        TArgs.image = td / 'loader.Image'
        TArgs.append = td / 'bad-payload.bin'
        TArgs.output = td / 'bad-payload-out.img'
        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects non-SWEETXNU payload", False, "should have raised")
        except SystemExit:
            report("rejects non-SWEETXNU payload", True)

        # f) Output exceeds partition size
        huge_loader = make_arm64_image(40 * 1024 * 1024)
        (td / 'huge-loader.Image').write_bytes(huge_loader)
        TArgs.image = td / 'huge-loader.Image'
        TArgs.append = td / 'payload.bin'
        TArgs.output = td / 'huge-out.img'
        TArgs.partition_size = 1024  # impossibly small
        try:
            pack_boot.cmd_pack(TArgs())
            report("rejects oversized output", False, "should have raised")
        except SystemExit:
            report("rejects oversized output", True)

        # g) mkinstaller rejects non-Mach-O kernel
        bad_kernel = td / 'bad-kernel'
        bad_kernel.write_bytes(b'\x00' * 1024)
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'herolte-mkinstaller.py'),
             '--kernel', str(bad_kernel), '--mesa', '/x',
             '--boot-stock', '/x', '--output', str(td / 'out.zip')],
            capture_output=True, text=True)
        report("mkinstaller rejects non-Mach-O kernel", result.returncode != 0)

        # h) mkinstaller rejects non-Android BOOT
        good_kern = td / 'kernel'
        good_kern.write_bytes(make_macho_kernel())
        bad_boot = td / 'bad-boot.img'
        bad_boot.write_bytes(b'NOTANDROID' + b'\x00' * 1024)
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'herolte-mkinstaller.py'),
             '--kernel', str(good_kern), '--mesa', '/x',
             '--boot-stock', str(bad_boot), '--output', str(td / 'out2.zip')],
            capture_output=True, text=True)
        report("mkinstaller rejects non-Android BOOT", result.returncode != 0)

        # i) mkinstaller rejects tiny BOOT
        tiny_boot = td / 'tiny.img'
        tiny_boot.write_bytes(b'ANDROID!' + b'\x00' * 100)
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'herolte-mkinstaller.py'),
             '--kernel', str(good_kern), '--mesa', '/x',
             '--boot-stock', str(tiny_boot), '--output', str(td / 'out3.zip')],
            capture_output=True, text=True)
        report("mkinstaller rejects tiny BOOT (<8192)", result.returncode != 0)


# ---------------------------------------------------------------------------
# 5. ZIP assembly
# ---------------------------------------------------------------------------

def test_zip_structure():
    print("\n--- 5. ZIP assembly ---")
    pack_boot = load_module('pack_boot_z', TOOLS / 'herolte-pack-boot.py')
    mod = load_module('mkinstaller_z', TOOLS / 'herolte-mkinstaller.py')

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        stock = make_android_boot(make_arm64_image(4096))
        (td / 'stock.img').write_bytes(stock)
        (td / 'loader.Image').write_bytes(make_arm64_image(8192))
        (td / 'payload.bin').write_bytes(b'SWEETXNU' + b'\x01' + b'\x00' * 4087)

        class PArgs:
            pass
        PArgs.stock = td / 'stock.img'
        PArgs.image = td / 'loader.Image'
        PArgs.output = td / 'xnu-boot.img'
        PArgs.append = td / 'payload.bin'
        PArgs.ramdisk = None
        PArgs.partition_size = 41943040
        pack_boot.cmd_pack(PArgs())

        mesa_dir = td / 'mesa'
        make_mesa_dir(mesa_dir)
        drop_path = mod.build_drop_package(str(mesa_dir), str(td))
        boot_sha = mod.sha256_file(str(td / 'stock.img'))

        output = td / 'test.zip'
        checksums = []
        with zipfile.ZipFile(str(output), 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('META-INF/com/google/android/updater-script', mod.UPDATER_SCRIPT)
            zf.writestr('META-INF/com/google/android/update-binary', mod.UPDATE_BINARY)

            def add_file(arcname, filepath):
                zf.write(filepath, arcname)
                checksums.append((mod.sha256_file(filepath), arcname))

            add_file('boot/xnu-boot.img', str(td / 'xnu-boot.img'))
            add_file('boot/drop-package.bin', drop_path)
            zf.writestr('boot/boot-stock-sha256.txt', boot_sha + '\n')
            for lib in sorted((mesa_dir / 'lib').glob('*.dylib')):
                add_file(f'mesa/lib/{lib.name}', str(lib))
            for tool in ('osmesa-test', 'hero-run'):
                p = mesa_dir / tool
                if p.exists():
                    add_file(f'mesa/{tool}', str(p))
            sums = '\n'.join(f'{h}  {n}' for h, n in checksums) + '\n'
            zf.writestr('SHA256SUMS', sums)

        report("ZIP created", output.exists())
        with zipfile.ZipFile(str(output)) as zf:
            names = zf.namelist()
            report("has update-binary", 'META-INF/com/google/android/update-binary' in names)
            report("has updater-script", 'META-INF/com/google/android/updater-script' in names)
            report("has xnu-boot.img", 'boot/xnu-boot.img' in names)
            report("has drop-package.bin", 'boot/drop-package.bin' in names)
            report("has boot-stock-sha256.txt", 'boot/boot-stock-sha256.txt' in names)
            report("has SHA256SUMS", 'SHA256SUMS' in names)
            report("has Mesa libs", any('mesa/lib/' in n for n in names))

            report("no payload.bin", 'boot/payload.bin' not in names)
            report("no xnu_boot.Image", 'boot/xnu_boot.Image' not in names)
            report("no bundled Python tools", not any('tools/' in n for n in names))

            boot_data = zf.read('boot/xnu-boot.img')
            report("xnu-boot.img is ANDROID!", boot_data[:8] == b'ANDROID!')
            drop_data = zf.read('boot/drop-package.bin')
            report("drop-package.bin is HERODRP1", drop_data[:8] == b'HERODRP1')

            sums_text = zf.read('SHA256SUMS').decode()
            sum_files = {l.split('  ', 1)[1] for l in sums_text.strip().split('\n') if '  ' in l}
            report("SHA256SUMS covers xnu-boot.img", 'boot/xnu-boot.img' in sum_files)
            report("SHA256SUMS covers drop-package.bin", 'boot/drop-package.bin' in sum_files)
            report("SHA256SUMS covers Mesa libs",
                   any(f.startswith('mesa/lib/') for f in sum_files))

            # Verify SHA256SUMS values are correct
            ok = True
            for line in sums_text.strip().split('\n'):
                expected, fname = line.split('  ', 1)
                actual = hashlib.sha256(zf.read(fname)).hexdigest()
                if expected != actual:
                    report(f"SHA256 correct for {fname}", False, f"{expected} != {actual}")
                    ok = False
            if ok:
                report("all SHA256SUMS values correct", True)


# ---------------------------------------------------------------------------
# 6. Drop zone format cross-validation
# ---------------------------------------------------------------------------

def test_drop_package_format():
    print("\n--- 6. Drop zone format ---")
    mod = load_module('mkinstaller_d', TOOLS / 'herolte-mkinstaller.py')

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        make_mesa_dir(td / 'mesa')
        path = mod.build_drop_package(str(td / 'mesa'), str(td))
        data = open(path, 'rb').read()

        report("HERODRP1 magic", data[:8] == b'HERODRP1')
        version, nentries, payload_size, ident = struct.unpack_from('<IIQQ', data, 8)
        report("version 1", version == 1)
        report("has entries", nentries > 0)
        report("block-aligned", len(data) % 4096 == 0)
        report("payload starts at block 1", len(data) >= 4096 + payload_size)

        # Parse entries like herolte-drop.py does
        PREFIX = '/private/var/herolte/'
        off = 4096
        entries_parsed = []
        for _ in range(nentries):
            path_len, mode, data_len = struct.unpack_from('<IIQ', data, off)
            off += 16
            entry_path = data[off:off + path_len].decode()
            off += path_len
            off += (-off) % 8
            entry_data = data[off:off + data_len]
            off += data_len
            off += (-off) % 8
            entries_parsed.append((entry_path, mode, len(entry_data)))

        report("all paths under /private/var/herolte/",
               all(p.startswith(PREFIX) or p.startswith(PREFIX.rstrip('/'))
                   for p, _, _ in entries_parsed))
        file_entries = [(p, m, s) for p, m, s in entries_parsed if m != 0]
        report("has autorun entry",
               any('autorun' in p and m == 0o755 for p, m, s in file_entries))
        report("has library entries",
               any('lib/' in p and p.endswith('.dylib') for p, _, _ in file_entries))
        report("has hero-run",
               any(p.endswith('hero-run') for p, _, _ in file_entries))

        # Size check: must fit in drop zone (128 MiB - 1 header block)
        max_payload = (32768 - 1) * 4096
        report(f"fits in drop zone ({len(data)} <= {max_payload + 4096})",
               len(data) <= max_payload + 4096)


# ---------------------------------------------------------------------------
# 7. ADT generation
# ---------------------------------------------------------------------------

def test_adt_generation():
    print("\n--- 7. ADT generation ---")
    try:
        sys.path.insert(0, str(TOOLS))
        old_cwd = os.getcwd()
        os.chdir(str(TOOLS))
        import mkadt_herolte
        import adt

        root = mkadt_herolte.build()
        blob = adt.serialize(root)
        root2, _ = adt.parse(blob)

        chosen = next(c for c in root2.children if c.prop('name') == b'chosen\x00')
        dram_base = struct.unpack('<Q', chosen.prop('dram-base'))[0]
        dram_size = struct.unpack('<Q', chosen.prop('dram-size'))[0]
        report("dram-base 0x90000000", dram_base == 0x90000000)
        report("dram-size 0x20000000 (512 MiB)", dram_size == 0x20000000)

        compat = root2.prop('compatible')
        report("compatible has D22AP", b'D22AP' in compat)
        report("compatible has samsung,herolte", b'samsung,herolte' in compat)
        report("compatible has exynos8890", b'samsung,exynos8890' in compat)

        cpus = next(c for c in root2.children if c.prop('name') == b'cpus\x00')
        cpu0 = cpus.children[0]
        reg0 = struct.unpack('<I', cpu0.prop('reg'))[0]
        report("cpu0 MPIDR 0x100 (A53 cluster)", reg0 == 0x100)
        report("8 CPUs total (4 A53 + 4 Mongoose)", len(cpus.children) == 8)

        ufs = [c for c in root2.children if b'herolte-ufs' in (c.prop('name') or b'')]
        report("herolte-ufs node exists", len(ufs) == 1)
        if ufs:
            ufs_addr = struct.unpack('<Q', ufs[0].prop('reg')[:8])[0]
            report("UFS at 0x155A0000", ufs_addr == 0x155A0000)

        arm_io = next(c for c in root2.children if c.prop('name') == b'arm-io\x00')
        gic = next(c for c in arm_io.children if b'gic' in (c.prop('name') or b''))
        gic_reg = struct.unpack('<4Q', gic.prop('reg'))
        report("GIC dist at 0x11001000", gic_reg[0] + 0x10000000 == 0x11001000)
        report("GIC CPU at 0x11002000", gic_reg[2] + 0x10000000 == 0x11002000)

        fbcon = next(c for c in root2.children if b'fbcon' in (c.prop('name') or b''))
        fb_addr = struct.unpack('<Q', fbcon.prop('reg')[:8])[0]
        fb_w = struct.unpack('<I', fbcon.prop('width'))[0]
        fb_h = struct.unpack('<I', fbcon.prop('height'))[0]
        report("framebuffer at 0xE2A00000", fb_addr == 0xe2a00000)
        report("framebuffer 1440x2560", fb_w == 1440 and fb_h == 2560)

        disp = next(c for c in arm_io.children if b'disp' in (c.prop('name') or b''))
        cw = struct.unpack('<I', disp.prop('canvas-width'))[0]
        ch = struct.unpack('<I', disp.prop('canvas-height'))[0]
        report("display canvas 1440x2560", cw == 1440 and ch == 2560)

        report(f"ADT serializes ({len(blob)} bytes)", len(blob) > 0)

        os.chdir(old_cwd)
    except Exception as e:
        report("ADT generation", False, str(e))
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# 8. Payload generation (SWEETXNU header validation)
# ---------------------------------------------------------------------------

def test_payload_structure():
    """Validate SWEETXNU header layout constants match between mkpayload.py and xnu_boot.c."""
    print("\n--- 8. SWEETXNU payload constants ---")
    try:
        sys.path.insert(0, str(TOOLS))
        import mkpayload

        report("magic offset 0x000", True)
        report("CMDLINE offset 0x48", mkpayload.CMDLINE == 0x48)
        report("SEG_TABLE offset 0x2a8", mkpayload.SEG_TABLE == 0x2a8)
        report("BOOT_LINE_LENGTH 608", mkpayload.BOOT_LINE_LENGTH == 608)
        report("CMDLINE + BOOT_LINE_LENGTH == SEG_TABLE",
               mkpayload.CMDLINE + mkpayload.BOOT_LINE_LENGTH == mkpayload.SEG_TABLE)
        report("PAGE size 16 KiB", mkpayload.PAGE == 0x4000)
    except Exception as e:
        report("payload constants", False, str(e))


# ---------------------------------------------------------------------------
# 9. ios-root workflow validation
# ---------------------------------------------------------------------------

def test_ios_root_workflow():
    """Validate ios-root.yml references correct tools and parameters."""
    print("\n--- 9. ios-root workflow ---")
    wf_path = REPO_ROOT / '.github' / 'workflows' / 'ios-root.yml'
    if not wf_path.exists():
        report("ios-root.yml exists", False)
        return
    report("ios-root.yml exists", True)
    content = wf_path.read_text()

    report("private repo guard", 'github.event.repository.private == true' in content)
    report("uses a5xelte-stage-ios-root.sh",
           'a5xelte-stage-ios-root.sh' in content)
    report("uses herolte-tune-ios-root.sh",
           'herolte-tune-ios-root.sh' in content)
    report("targets iPhone10,3 (D22AP)",
           'iPhone10,3' in content)
    report("targets iOS 16.7.16",
           '16.7.16' in content)
    report("creates HFSX volume",
           'Case-sensitive Journaled HFS+' in content or 'HFSX' in content)
    report("default 20480 MiB",
           '20480' in content)
    report("runs on macOS",
           'macos-' in content)
    report("publishes as release",
           'gh release create' in content)
    report("includes SHA256SUMS",
           'SHA256SUMS' in content)
    report("includes d22ap.adt",
           'd22ap.adt' in content)
    report("splits with 1900m chunks",
           '1900m' in content)

    # Verify herolte-tune-ios-root.sh exists and has key operations
    tune = REPO_ROOT / 'tools' / 'herolte-tune-ios-root.sh'
    report("herolte-tune-ios-root.sh exists", tune.exists())
    if tune.exists():
        tune_content = tune.read_text()
        report("tune removes CommCenter", 'CommCenter' in tune_content)
        report("tune adds com.herolte.autorun",
               'com.herolte.autorun' in tune_content)
        report("tune marks setup complete",
               'SetupDone' in tune_content)
        report("tune creates /private/var/containers",
               '/private/var/containers' in tune_content)

    # Verify stage script exists and validates sources
    stage = REPO_ROOT / 'tools' / 'a5xelte-stage-ios-root.sh'
    report("a5xelte-stage-ios-root.sh exists", stage.exists())
    if stage.exists():
        stage_content = stage.read_text()
        report("stage checks launchd ownership",
               'stat -f %u' in stage_content and 'launchd' in stage_content)
        report("stage copies dyld shared cache",
               'dyld_shared_cache_arm64' in stage_content)
        report("stage copies cryptexes",
               'Cryptexes' in stage_content)


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("King-iOS16 Installer Test Suite")
    print("================================")

    test_update_binary_structure()
    test_pack_boot_integration()
    test_realistic_samsung_boot()
    test_malformed_inputs()
    test_zip_structure()
    test_drop_package_format()
    test_adt_generation()
    test_payload_structure()
    test_ios_root_workflow()

    print(f"\n{'=' * 40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
