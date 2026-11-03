#!/usr/bin/env python3
"""Test suite for herolte-mkinstaller.py: synthetic-input structural tests.

Validates the installer builder and update-binary without real firmware.
Uses synthetic BOOT images, fake kernels, and dummy Mesa artifacts.
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
    status = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
    else:
        PASS += 1
    msg = f"  [{status}] {name}"
    if detail and not ok:
        msg += f": {detail}"
    print(msg)
    return ok


def make_arm64_image(size=12288):
    """Minimal arm64 Linux Image header."""
    img = bytearray(size)
    struct.pack_into('<I', img, 0, 0x14000002)  # branch instruction
    img[56:60] = b'ARMd'
    struct.pack_into('<3Q', img, 8, 0x80000, size + 0x10000, 0x0a)
    return bytes(img)


def make_android_boot(kernel, ramdisk=b'\x1f\x8b' + b'\x00' * 1024,
                      dtbh=None, page=2048):
    """Build a minimal Android BOOT image with DTBH for Exynos 8890."""
    if dtbh is None:
        # DTBH with one Exynos 8890 entry
        dtbh_header = struct.pack('<3I', 0x48425444, 2, 1)  # "DTBH", version=2, count=1
        # chip=8890, platform=0, subtype=0, hw_rev=0, hw_rev_end=0, offset=0x100, size=64, space=0
        dtbh_entry = struct.pack('<8I', 8890, 0, 0, 0, 0, 0x100, 64, 0)
        dtbh_data = dtbh_header + dtbh_entry
        dtbh_data += b'\0' * (0x100 - len(dtbh_data))
        dtbh_data += b'\xd0\x0d\xfe\xed' + b'\0' * 60  # fake FDT at offset
        dtbh = dtbh_data

    align = lambda s: (s + page - 1) // page * page

    header = bytearray(page)
    header[0:8] = b'ANDROID!'
    struct.pack_into('<I', header, 8, len(kernel))
    struct.pack_into('<I', header, 12, 0x80008000)
    struct.pack_into('<I', header, 16, len(ramdisk))
    struct.pack_into('<I', header, 20, 0x82000000)
    struct.pack_into('<I', header, 24, 0)  # second_size
    struct.pack_into('<I', header, 28, 0x81e00000)
    struct.pack_into('<I', header, 32, 0x81e00000)  # tags
    struct.pack_into('<I', header, 36, page)
    struct.pack_into('<I', header, 40, len(dtbh))

    # SHA-1 digest
    digest = hashlib.sha1()
    for part, size in [(kernel, len(kernel)), (ramdisk, len(ramdisk)),
                       (b'', 0), (dtbh, len(dtbh))]:
        digest.update(part)
        digest.update(struct.pack('<I', size))
    header[576:596] = digest.digest()

    out = bytearray(header)
    out.extend(kernel)
    out.extend(b'\0' * (align(len(kernel)) - len(kernel)))
    out.extend(ramdisk)
    out.extend(b'\0' * (align(len(ramdisk)) - len(ramdisk)))
    # second is empty
    out.extend(dtbh)
    out.extend(b'\0' * (align(len(dtbh)) - len(dtbh)))
    out.extend(b'SEANDROIDENFORCE')
    return bytes(out)


def make_macho_kernel(size=65536):
    """Minimal 64-bit Mach-O header."""
    k = bytearray(size)
    struct.pack_into('<I', k, 0, 0xfeedfacf)
    return bytes(k)


def make_mesa_dir(d):
    """Create a fake Mesa artifact directory."""
    lib = d / 'lib'
    lib.mkdir(parents=True)
    (lib / 'libOSMesa.8.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 1024)
    (lib / 'libdrm.2.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 512)
    (lib / 'libglapi.0.dylib').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 256)
    (d / 'hero-run').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 256)
    (d / 'osmesa-test').write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 128)
    os.chmod(d / 'hero-run', 0o755)
    os.chmod(d / 'osmesa-test', 0o755)


def test_update_binary_structure():
    """Verify the update-binary script has required safety features."""
    print("\n--- update-binary structure tests ---")

    # Read the script from the installer module
    sys.path.insert(0, str(TOOLS))
    spec = __import__('importlib').util.spec_from_file_location(
        'mkinstaller', str(TOOLS / 'herolte-mkinstaller.py'))
    mod = __import__('importlib').util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    script = mod.UPDATE_BINARY

    report("has SM-G930F model check",
           'SM-G930F' in script)
    report("has UFS platform check",
           '155a0000.ufs' in script)
    report("no backup to /sdcard",
           '/sdcard' not in script and 'BACKUP_DIR="/sdcard' not in script,
           "script still backs up to /sdcard which IS on USERDATA")
    report("no hexdump byte manipulation",
           'hexdump' not in script,
           "script still uses fragile hexdump for BOOT repacking")
    report("no printf byte writing",
           'printf "\\x' not in script,
           "script still uses printf for byte writes")
    report("uses pre-built xnu-boot.img",
           'xnu-boot.img' in script)
    report("has battery check",
           'battery' in script.lower() or 'capacity' in script.lower())
    report("has USERDATA size check",
           'DATA_SIZE' in script and '20000000000' in script)
    report("has BOOT readback verification",
           'READBACK_SHA' in script or 'readback' in script.lower())
    report("has abort function",
           'abort()' in script or 'abort "' in script)
    report("aborts on BOOT write failure",
           'require "Failed to write BOOT' in script)
    report("verifies ANDROID! magic on xnu-boot.img",
           "ANDROID!" in script)
    report("no shell BOOT repacking (no stock kernel parsing)",
           'STOCK_KERNEL_SIZE' not in script,
           "script still parses stock kernel size for on-phone repacking")
    report("unmounts /data",
           'umount /data' in script)


def test_pack_boot_integration():
    """Test that pack_boot_image produces a valid Android BOOT."""
    print("\n--- pack_boot_image integration tests ---")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        kernel_img = make_arm64_image(8192)
        stock = make_android_boot(make_arm64_image(4096))
        payload = b'SWEETXNU' + b'\x01' + b'\x00' * 4087

        (td / 'stock.img').write_bytes(stock)
        (td / 'loader.Image').write_bytes(kernel_img)
        (td / 'payload.bin').write_bytes(payload)

        # Use herolte-pack-boot.py directly
        spec = __import__('importlib').util.spec_from_file_location(
            'pack_boot', str(TOOLS / 'herolte-pack-boot.py'))
        pack_boot = __import__('importlib').util.module_from_spec(spec)
        spec.loader.exec_module(pack_boot)

        class Args:
            stock = td / 'stock.img'
            image = td / 'loader.Image'
            output = td / 'xnu-boot.img'
            append = td / 'payload.bin'
            ramdisk = None
            partition_size = 41943040

        try:
            pack_boot.cmd_pack(Args())
            exists = Args.output.exists()
            report("pack_boot produces output", exists)

            if exists:
                data = Args.output.read_bytes()
                report("output starts with ANDROID!",
                       data[:8] == b'ANDROID!')
                report("output ends with SEANDROIDENFORCE",
                       data.endswith(b'SEANDROIDENFORCE'))
                report("output fits in 40 MiB partition",
                       len(data) <= 41943040)

                # Parse to verify structure
                parsed = pack_boot.parse(data)
                report("kernel contains loader + payload",
                       len(parsed['kernel']) > len(kernel_img))
                report("ramdisk preserved from stock",
                       len(parsed['ramdisk']) > 0)
                report("DTBH preserved",
                       parsed['dt'][:4] == b'DTBH')
        except Exception as e:
            report("pack_boot runs without error", False, str(e))


def test_zip_structure():
    """Test ZIP structure by assembling one directly (bypasses mkpayload which needs a real Mach-O)."""
    print("\n--- ZIP structure tests ---")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # Build a synthetic xnu-boot.img via pack_boot (already tested above)
        loader = make_arm64_image(8192)
        stock = make_android_boot(make_arm64_image(4096))
        payload = b'SWEETXNU' + b'\x01' + b'\x00' * 4087

        (td / 'stock.img').write_bytes(stock)
        (td / 'loader.Image').write_bytes(loader)
        (td / 'payload.bin').write_bytes(payload)

        spec = __import__('importlib').util.spec_from_file_location(
            'pack_boot2', str(TOOLS / 'herolte-pack-boot.py'))
        pack_boot = __import__('importlib').util.module_from_spec(spec)
        spec.loader.exec_module(pack_boot)

        class PArgs:
            stock_p = td / 'stock.img'
            image = td / 'loader.Image'
            output = td / 'xnu-boot.img'
            append = td / 'payload.bin'
            ramdisk = None
            partition_size = 41943040
        PArgs.stock = PArgs.stock_p
        pack_boot.cmd_pack(PArgs())

        # Build drop package
        mesa_dir = td / 'mesa'
        make_mesa_dir(mesa_dir)

        spec2 = __import__('importlib').util.spec_from_file_location(
            'mkinstaller2', str(TOOLS / 'herolte-mkinstaller.py'))
        mod = __import__('importlib').util.module_from_spec(spec2)
        spec2.loader.exec_module(mod)

        drop_path = mod.build_drop_package(str(mesa_dir), str(td))
        boot_sha = mod.sha256_file(str(td / 'stock.img'))

        # Assemble ZIP the same way main() does
        output = td / 'test-output.zip'
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

        built = output.exists()
        report("ZIP assembled", built)

        with zipfile.ZipFile(str(output)) as zf:
            names = zf.namelist()
            report("has update-binary",
                   'META-INF/com/google/android/update-binary' in names)
            report("has updater-script",
                   'META-INF/com/google/android/updater-script' in names)
            report("has xnu-boot.img",
                   'boot/xnu-boot.img' in names)
            report("has drop-package.bin",
                   'boot/drop-package.bin' in names)
            report("has boot-stock-sha256.txt",
                   'boot/boot-stock-sha256.txt' in names)
            report("has SHA256SUMS",
                   'SHA256SUMS' in names)
            report("has Mesa libs",
                   any('mesa/lib/' in n for n in names))

            # No old-style files that should be gone
            report("no payload.bin (embedded in xnu-boot.img now)",
                   'boot/payload.bin' not in names)
            report("no xnu_boot.Image (embedded in xnu-boot.img now)",
                   'boot/xnu_boot.Image' not in names)
            report("no bundled Python tools (not needed on phone)",
                   not any('tools/' in n for n in names))

            # Verify xnu-boot.img is valid
            boot_data = zf.read('boot/xnu-boot.img')
            report("xnu-boot.img starts with ANDROID!",
                   boot_data[:8] == b'ANDROID!')

            # Verify drop package
            drop_data = zf.read('boot/drop-package.bin')
            report("drop-package.bin starts with HERODRP1",
                   drop_data[:8] == b'HERODRP1')

            # Verify SHA256SUMS covers all content files
            sums_text = zf.read('SHA256SUMS').decode()
            sum_files = {line.split('  ', 1)[1] for line in sums_text.strip().split('\n') if '  ' in line}
            report("SHA256SUMS covers xnu-boot.img",
                   'boot/xnu-boot.img' in sum_files)
            report("SHA256SUMS covers drop-package.bin",
                   'boot/drop-package.bin' in sum_files)


def test_wrong_device_inputs():
    """Test rejection of invalid inputs."""
    print("\n--- Input validation tests ---")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # Not a Mach-O kernel
        bad_kernel = td / 'bad-kernel'
        bad_kernel.write_bytes(b'\x00' * 1024)
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'herolte-mkinstaller.py'),
             '--kernel', str(bad_kernel),
             '--mesa', '/nonexistent',
             '--boot-stock', '/nonexistent',
             '--output', str(td / 'out.zip')],
            capture_output=True, text=True)
        report("rejects non-Mach-O kernel", result.returncode != 0)

        # Not an Android BOOT
        good_kernel = td / 'kernel'
        good_kernel.write_bytes(make_macho_kernel())
        bad_boot = td / 'bad-boot.img'
        bad_boot.write_bytes(b'NOTANDROID' + b'\x00' * 1024)
        result = subprocess.run(
            [sys.executable, str(TOOLS / 'herolte-mkinstaller.py'),
             '--kernel', str(good_kernel),
             '--mesa', '/nonexistent',
             '--boot-stock', str(bad_boot),
             '--output', str(td / 'out.zip')],
            capture_output=True, text=True)
        report("rejects non-Android BOOT image", result.returncode != 0)


def test_drop_package_format():
    """Verify the drop package binary matches herolte-drop.py format."""
    print("\n--- Drop package format tests ---")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        make_mesa_dir(td / 'mesa')

        sys.path.insert(0, str(TOOLS))
        spec = __import__('importlib').util.spec_from_file_location(
            'mkinstaller', str(TOOLS / 'herolte-mkinstaller.py'))
        mod = __import__('importlib').util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        path = mod.build_drop_package(str(td / 'mesa'), str(td))
        data = open(path, 'rb').read()

        report("starts with HERODRP1", data[:8] == b'HERODRP1')
        version, nentries, payload_size, ident = struct.unpack_from('<IIQQ', data, 8)
        report("version is 1", version == 1)
        report("has entries", nentries > 0)
        report("block-aligned", len(data) % 4096 == 0)
        report("first entry block at 4096", len(data) > 4096)


if __name__ == '__main__':
    print("King-iOS16 Installer Test Suite")
    print("================================")

    test_update_binary_structure()
    test_pack_boot_integration()
    test_zip_structure()
    test_wrong_device_inputs()
    test_drop_package_format()

    print(f"\n{'=' * 40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
