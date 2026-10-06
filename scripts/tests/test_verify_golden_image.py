#!/usr/bin/env python3
"""Unit tests for verify-golden-image.py — the on-disk golden-image check.

The check exists because a half-applied Windows image publishes silently: the
install is killed, `tools/click.py` reads the kill as a finished install, and
every template built from the image hangs much later. The one thing that
distinguishes the two is on the disk — an EFI System Partition with
``\\EFI\\Microsoft\\Boot\\bootmgfw.efi`` — so the interesting tests are the ones
that build that structure, and the ones that leave it out.

The disks here are assembled by hand in a bytearray rather than produced by a
real build, which is the point: the parser is exercised against a GPT and a
FAT16 filesystem with no qemu, no root and no 5 GiB download. ``Microsoft`` is
nine characters, so the fixture has to carry a VFAT long-name record — which is
also what makes it a real test of the name handling, because a reader that only
understands 8.3 names cannot find that directory at all.

The module under test lives in a file with a hyphen in its name, so it is loaded
by path.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
MODULE_PATH = SCRIPTS / "verify-golden-image.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_golden_image", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_image = _load()

SECTOR = 512
ESP_START_LBA = 2048
ESP_SECTORS = 5041  # chosen so the FAT has ~5000 clusters: the FAT16 band
ROOT_ENTRIES = 512
FAT_SECTORS = 4
RESERVED_SECTORS = 1
NUM_FATS = 2


def _lfn(sequence: int, name: str) -> bytes:
    """One VFAT long-name record holding part of `name`."""
    raw = bytearray(32)
    raw[0] = sequence
    raw[11] = 0x0F
    units = [ord(character) for character in name] + [0x0000]
    units += [0xFFFF] * (13 - len(units))
    blob = b"".join(struct.pack("<H", unit) for unit in units)
    raw[1:11] = blob[0:10]
    raw[14:26] = blob[10:22]
    raw[28:32] = blob[22:26]
    return bytes(raw)


def _entry(name8: str, ext3: str, attributes: int, cluster: int) -> bytes:
    raw = bytearray(32)
    raw[0:8] = name8.ljust(8)[:8].encode("ascii")
    raw[8:11] = ext3.ljust(3)[:3].encode("ascii")
    raw[11] = attributes
    struct.pack_into("<H", raw, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", raw, 26, cluster & 0xFFFF)
    return bytes(raw)


def _end_of_directory() -> bytes:
    return bytes(32)


def _fat16_boot_sector(total_sectors: int) -> bytes:
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", boot, 11, SECTOR)
    boot[13] = 1  # sectors per cluster
    struct.pack_into("<H", boot, 14, RESERVED_SECTORS)
    boot[16] = NUM_FATS
    struct.pack_into("<H", boot, 17, ROOT_ENTRIES)
    struct.pack_into("<H", boot, 19, total_sectors)
    boot[21] = 0xF8  # fixed-disk media descriptor
    struct.pack_into("<H", boot, 22, FAT_SECTORS)
    boot[38] = 0x29
    boot[43:54] = b"ONTRAK ESP "
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xaa"
    return bytes(boot)


def _build_disk(with_boot_file: bool = True, as_esp: bool = True) -> bytes:
    """A raw disk: GPT with one partition, formatted FAT16, holding a Windows ESP."""
    size = (ESP_START_LBA + ESP_SECTORS + 64) * SECTOR
    disk = bytearray(size)

    # -- protective MBR (LBA 0): one partition of type 0xEE covering the disk ----
    disk[510:512] = b"\x55\xaa"
    disk[446 + 4] = 0xEE

    # -- GPT header (LBA 1) ------------------------------------------------------
    header = bytearray(SECTOR)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<Q", header, 24, 1)  # this header's LBA
    struct.pack_into("<Q", header, 32, size // SECTOR - 1)  # backup header
    struct.pack_into("<Q", header, 40, 34)  # first usable
    struct.pack_into("<Q", header, 48, size // SECTOR - 34)  # last usable
    struct.pack_into("<Q", header, 72, 2)  # partition entries follow
    struct.pack_into("<I", header, 80, 128)
    struct.pack_into("<I", header, 84, 128)
    disk[1 * SECTOR : 2 * SECTOR] = header

    # -- one partition entry -----------------------------------------------------
    type_guid = verify_image.ESP_TYPE_GUID if as_esp else uuid.uuid4()
    entry = bytearray(128)
    entry[0:16] = type_guid.bytes_le
    entry[16:32] = uuid.uuid4().bytes_le
    struct.pack_into("<Q", entry, 32, ESP_START_LBA)
    struct.pack_into("<Q", entry, 40, ESP_START_LBA + ESP_SECTORS - 1)
    name = "EFI System Partition".encode("utf-16-le")
    entry[56 : 56 + len(name)] = name
    disk[2 * SECTOR : 2 * SECTOR + 128] = entry

    # -- the FAT16 filesystem in the partition ----------------------------------
    base = ESP_START_LBA * SECTOR
    disk[base : base + SECTOR] = _fat16_boot_sector(ESP_SECTORS)
    root_offset = base + (RESERVED_SECTORS + NUM_FATS * FAT_SECTORS) * SECTOR
    root_size = ROOT_ENTRIES * 32
    data_offset = root_offset + root_size

    def cluster_offset(cluster: int) -> int:
        return data_offset + (cluster - 2) * SECTOR

    # root: EFI/
    disk[root_offset : root_offset + 32] = _entry("EFI", "", 0x10, 2)
    disk[root_offset + 32 : root_offset + 64] = _end_of_directory()

    # EFI/Microsoft/ — the long name needs a VFAT record; 8.3 cannot spell it
    efI = cluster_offset(2)
    disk[efI : efI + 32] = _lfn(0x41, "Microsoft")
    disk[efI + 32 : efI + 64] = _entry("MICROS~1", "", 0x10, 3)
    disk[efI + 64 : efI + 96] = _end_of_directory()

    # EFI/Microsoft/Boot/
    microsoft = cluster_offset(3)
    disk[microsoft : microsoft + 32] = _entry("Boot", "", 0x10, 4)
    disk[microsoft + 32 : microsoft + 64] = _end_of_directory()

    # EFI/Microsoft/Boot/bootmgfw.efi — exactly 8.3, so no long name is needed
    boot = cluster_offset(4)
    if with_boot_file:
        disk[boot : boot + 32] = _entry("BOOTMGFW", "EFI", 0x20, 5)
    disk[boot + 32 : boot + 64] = _end_of_directory()

    # FAT: clusters 2..5 are each a single-cluster end-of-chain
    fat_offset = base + RESERVED_SECTORS * SECTOR
    for copy in range(NUM_FATS):
        start = fat_offset + copy * FAT_SECTORS * SECTOR
        entries = {0: 0xFFF8, 1: 0xFFFF, 2: 0xFFFF, 3: 0xFFFF, 4: 0xFFFF, 5: 0xFFFF}
        for index, value in entries.items():
            struct.pack_into("<H", disk, start + index * 2, value)
    return bytes(disk)


class VerifyGoldenImageTests(unittest.TestCase):
    def _write(self, payload: bytes, name: str = "disk.qcow2") -> Path:
        directory = Path(tempfile.mkdtemp(prefix="ontrak-verify-test-"))
        target = directory / name
        target.write_bytes(payload)
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        return target

    def _verify(self, payload: bytes):
        # named .qcow2 to exercise the raw path through resolve_input, but the bytes
        # begin with a GPT rather than the qcow2 magic, so no qemu-img is needed.
        return verify_image.verify(self._write(payload))

    def test_a_complete_image_verifies(self):
        report = self._verify(_build_disk())
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.boot_file, r"EFI\Microsoft\Boot\bootmgfw.efi")
        self.assertEqual(len(report.partitions), 1)
        self.assertEqual(report.esp_checked, 1)

    def test_the_long_name_directory_is_what_makes_it_findable(self):
        """A reader that only understood 8.3 names could not find \\EFI\\Microsoft.

        The fixture stores ``Microsoft`` as MICROS~1 plus a VFAT long-name record,
        exactly as Windows does, so this passing is what proves the long name is
        being read rather than the short one being coincidentally usable.
        """
        disk = _build_disk()
        report = self._verify(disk)
        self.assertTrue(report.ok, report.problems)
        # and the short name alone is not what BOOT_FILE asks for
        self.assertNotIn("MICROS~1", verify_image.BOOT_FILE)

    def test_lookup_is_case_insensitive(self):
        original = verify_image.BOOT_FILE
        verify_image.BOOT_FILE = ("efi", "microsoft", "boot", "BOOTMGFW.EFI")
        try:
            report = self._verify(_build_disk())
        finally:
            verify_image.BOOT_FILE = original
        self.assertTrue(report.ok, report.problems)

    def test_a_disk_without_the_boot_file_is_refused(self):
        report = self._verify(_build_disk(with_boot_file=False))
        self.assertFalse(report.ok)
        self.assertTrue(any("bootmgfw.efi" in problem for problem in report.problems), report.problems)
        self.assertEqual(report.esp_checked, 1)

    def test_a_disk_with_no_esp_is_refused(self):
        report = self._verify(_build_disk(as_esp=False))
        self.assertFalse(report.ok)
        self.assertEqual(report.esp_checked, 0)
        self.assertTrue(any("EFI System Partition" in problem for problem in report.problems), report.problems)

    def test_a_disk_without_a_partition_table_is_refused(self):
        report = self._verify(bytes((ESP_START_LBA + ESP_SECTORS + 64) * SECTOR))
        self.assertFalse(report.ok)
        self.assertTrue(any("GPT" in problem for problem in report.problems), report.problems)

    def test_a_legacy_mbr_disk_names_what_it_is(self):
        disk = bytearray((ESP_START_LBA + ESP_SECTORS + 64) * SECTOR)
        disk[510:512] = b"\x55\xaa"
        disk[446 + 4] = 0x07  # an NTFS-style MBR partition, not a protective 0xEE
        report = self._verify(bytes(disk))
        self.assertFalse(report.ok)
        self.assertTrue(any("legacy MBR" in problem for problem in report.problems), report.problems)

    def test_an_export_directory_resolves_to_its_disk(self):
        directory = Path(tempfile.mkdtemp(prefix="ontrak-verify-export-"))
        (directory / "disk.qcow2").write_bytes(_build_disk())
        resolved = verify_image.resolve_input(directory)
        self.assertEqual(resolved.name, "disk.qcow2")
        self.assertTrue(verify_image.verify(resolved).ok)

    def test_a_directory_with_no_disk_is_an_error(self):
        directory = Path(tempfile.mkdtemp(prefix="ontrak-verify-empty-"))
        with self.assertRaises(verify_image.ImageError):
            verify_image.resolve_input(directory)

    def test_a_missing_path_is_an_error_not_a_verdict(self):
        with self.assertRaises(verify_image.ImageError):
            verify_image.verify(Path("/nonexistent/golden.qcow2"))


if __name__ == "__main__":
    unittest.main()
