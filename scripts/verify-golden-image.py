#!/usr/bin/env python3
"""Prove that a golden-image export is a complete Windows install.

`infra/build-golden-image.sh` can publish a half-applied image, and nothing says
so until every Windows template built from it hangs at the firmware boot prompt.
The tell is on the disk: a real install has a GPT, an EFI System Partition, and
``\\EFI\\Microsoft\\Boot\\bootmgfw.efi`` on that partition. When the Windows image
apply is killed part-way — the OOM kill that ``io.cache=none`` exists to prevent —
the ESP is formatted but that file was never written, and the disk is not fit to
publish.

Doing this check by hand means attaching the qcow2 to a loop device, mounting the
ESP and looking, which needs root and a free nbd. This reads the partition table
and the FAT filesystem out of the file directly and needs neither, so it can run
against an image somebody else built *before* it replaces ``ontrak-win-base``.

Usage:
    scripts/verify-golden-image.py <disk.qcow2|disk.raw|export-directory>
    scripts/verify-golden-image.py --json <path>

An export directory is an `incus-windows` output directory: it must hold the
``disk.qcow2`` the image was published from.

Exit codes: 0 the image is complete, 2 it is not or could not be read.

Reading a qcow2 needs ``qemu-img``, which the build path already requires; a raw
image is read in place and needs nothing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

SECTOR = 512
GPT_SIGNATURE = b"EFI PART"
QCOW2_MAGIC = b"QFI\xfb"
MBR_SIGNATURE = b"\x55\xaa"

# The EFI System Partition type GUID (C12A7328-F81F-11D2-BA4B-00A0C93EC93B), which
# every UEFI bootable Windows disk has exactly one of.
ESP_TYPE_GUID = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")

# What Windows Setup writes to the ESP and nothing else does. Both names are
# 8.3-representable, but Windows stores the long name, so an 8.3-only reader would
# have to guess — this one reads the long name and falls back to the short one.
BOOT_FILE = ("EFI", "Microsoft", "Boot", "bootmgfw.efi")

ATTR_LONG_NAME = 0x0F
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10

FAT_END_OF_CHAIN = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}
CLUSTER_CHAIN_LIMIT = 1 << 20  # a corrupt FAT loop must not hang the check


class ImageError(Exception):
    """The image could not be read — as opposed to being read and found wanting."""


@dataclass
class Partition:
    index: int
    type_guid: uuid.UUID
    first_lba: int
    last_lba: int
    name: str = ""

    @property
    def offset(self) -> int:
        return self.first_lba * SECTOR

    @property
    def size(self) -> int:
        return max(0, self.last_lba - self.first_lba + 1) * SECTOR

    def describe(self) -> str:
        return f"{self.name or self.type_guid} at sector {self.first_lba} ({self.size >> 20} MiB)"


@dataclass
class Report:
    path: str
    partitions: list[Partition] = field(default_factory=list)
    esp_checked: int = 0
    boot_file: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------
# byte-level reads
# --------------------------------------------------------------------------
def _read(stream: BinaryIO, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise ImageError(f"refusing to read {size} bytes at {offset}")
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ImageError(f"short read at {offset}: wanted {size} bytes, got {len(data)}")
    return data


def _guid(raw: bytes) -> uuid.UUID:
    # GPT stores a GUID as the mixed-endian "bytes_le" form that Windows uses.
    return uuid.UUID(bytes_le=raw)


def read_gpt(stream: BinaryIO) -> list[Partition]:
    """Parse the GPT and return its non-empty entries, in table order."""
    header = _read(stream, SECTOR, SECTOR)  # LBA 1
    if header[:8] != GPT_SIGNATURE:
        mbr = _read(stream, 0, SECTOR)
        if mbr[510:512] == MBR_SIGNATURE and any(mbr[446 + i * 16 + 4] for i in range(4)):
            raise ImageError("the disk uses a legacy MBR, not GPT (expected a UEFI install)")
        raise ImageError("no GPT header at LBA 1 (the disk has no partition table)")
    header_size = struct.unpack_from("<I", header, 12)[0]
    if not 92 <= header_size <= SECTOR:
        raise ImageError(f"implausible GPT header size {header_size}")
    entries_lba = struct.unpack_from("<Q", header, 72)[0]
    entry_count = struct.unpack_from("<I", header, 80)[0]
    entry_size = struct.unpack_from("<I", header, 84)[0]
    if entry_size < 128 or entry_size % 8 or not 1 <= entry_count <= 4096:
        raise ImageError("implausible GPT partition entry array")
    raw = _read(stream, entries_lba * SECTOR, entry_count * entry_size)
    partitions: list[Partition] = []
    for index in range(entry_count):
        entry = raw[index * entry_size : (index + 1) * entry_size]
        type_guid = _guid(entry[0:16])
        if type_guid.int == 0:  # unused entry
            continue
        first_lba = struct.unpack_from("<Q", entry, 32)[0]
        last_lba = struct.unpack_from("<Q", entry, 40)[0]
        name = entry[56:128].decode("utf-16-le", "ignore").split("\x00")[0]
        partitions.append(Partition(index + 1, type_guid, first_lba, last_lba, name))
    if not partitions:
        raise ImageError("the GPT has no partitions")
    return partitions


# --------------------------------------------------------------------------
# FAT (the ESP is FAT12/16/32)
# --------------------------------------------------------------------------
@dataclass
class FatVolume:
    base: int
    bytes_per_sector: int
    sectors_per_cluster: int
    fat_offset: int
    fat_bits: int
    data_offset: int
    root_offset: int
    root_size: int
    root_cluster: int

    @property
    def cluster_size(self) -> int:
        return self.sectors_per_cluster * self.bytes_per_sector

    @property
    def root(self) -> int:
        """The first cluster of the root directory (0 if it is the fixed FAT12/16 area)."""
        return self.root_cluster if self.fat_bits == 32 else 0

    def cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 2) * self.cluster_size


@dataclass
class Entry:
    long: str
    short: str
    attributes: int
    cluster: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)

    def matches(self, part: str) -> bool:
        wanted = part.casefold()
        return wanted in (self.long.casefold(), self.short.casefold())


def open_fat(stream: BinaryIO, base: int) -> FatVolume:
    boot = _read(stream, base, SECTOR)
    if boot[510:512] != MBR_SIGNATURE:
        raise ImageError("the EFI System Partition does not begin with a FAT boot sector")
    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    sectors_per_cluster = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    root_entries = struct.unpack_from("<H", boot, 17)[0]
    total_sectors = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
    fat_size = struct.unpack_from("<H", boot, 22)[0]
    if bytes_per_sector not in (512, 1024, 2048, 4096) or not sectors_per_cluster or not reserved or not fats:
        raise ImageError("the EFI System Partition's FAT boot sector is not usable")
    if fat_size == 0:  # FAT32 keeps the size and the root cluster in different places
        fat_size = struct.unpack_from("<I", boot, 36)[0]
        root_cluster = struct.unpack_from("<I", boot, 44)[0]
        root_offset, root_size = 0, 0
    else:
        root_cluster = 0
        root_offset = base + (reserved + fats * fat_size) * bytes_per_sector
        root_size = root_entries * 32
    if not fat_size:
        raise ImageError("the EFI System Partition's FAT size is zero")
    data_offset = base + (reserved + fats * fat_size) * bytes_per_sector + root_size
    data_sectors = max(0, total_sectors - (reserved + fats * fat_size + root_size // bytes_per_sector))
    clusters = data_sectors // sectors_per_cluster
    if not clusters:
        raise ImageError("the EFI System Partition's FAT has no data region")
    fat_bits = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
    return FatVolume(
        base=base,
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        fat_offset=base + reserved * bytes_per_sector,
        fat_bits=fat_bits,
        data_offset=data_offset,
        root_offset=root_offset,
        root_size=root_size,
        root_cluster=root_cluster,
    )


def _fat_next(stream: BinaryIO, volume: FatVolume, cluster: int) -> int:
    if volume.fat_bits == 32:
        raw = _read(stream, volume.fat_offset + cluster * 4, 4)
        return struct.unpack("<I", raw)[0] & 0x0FFFFFFF
    if volume.fat_bits == 16:
        raw = _read(stream, volume.fat_offset + cluster * 2, 2)
        return struct.unpack("<H", raw)[0]
    # FAT12 packs two 12-bit entries into three bytes.
    raw = struct.unpack("<H", _read(stream, volume.fat_offset + (cluster * 3) // 2, 2))[0]
    return (raw >> 4) & 0xFFF if cluster & 1 else raw & 0xFFF


def _cluster_chain(stream: BinaryIO, volume: FatVolume, first: int) -> Iterator[int]:
    cluster = first
    for _ in range(CLUSTER_CHAIN_LIMIT):
        if cluster < 2:
            return
        yield cluster
        if volume.cluster_size == 0:
            return
        following = _fat_next(stream, volume, cluster)
        if following >= FAT_END_OF_CHAIN[volume.fat_bits]:
            return
        cluster = following
    raise ImageError("the FAT cluster chain does not end (the filesystem is corrupt)")


def _directory_bytes(stream: BinaryIO, volume: FatVolume, cluster: int) -> bytes:
    if cluster == 0:  # the fixed root directory area of FAT12/16
        return _read(stream, volume.root_offset, volume.root_size)
    return b"".join(
        _read(stream, volume.cluster_offset(c), volume.cluster_size) for c in _cluster_chain(stream, volume, cluster)
    )


def _short_name(raw: bytes) -> str:
    base = raw[0:8].decode("ascii", "ignore").rstrip(" \x00")
    extension = raw[8:11].decode("ascii", "ignore").rstrip(" \x00")
    if base[:1] == "\x05":  # a leading 0xE5 is escaped, because 0xE5 means "deleted"
        base = "\xe5" + base[1:]
    return f"{base}.{extension}" if extension else base


def _long_name(entries: list[bytes]) -> str:
    """Assemble a VFAT long name. `entries` are the LFN records, in file order."""
    parts: dict[int, bytes] = {}
    for raw in entries:
        sequence = raw[0] & 0x1F
        if sequence:
            parts[sequence] = raw[1:11] + raw[14:26] + raw[28:32]
    if not parts:
        return ""
    blob = b"".join(parts.get(index, b"") for index in range(1, max(parts) + 1))
    return blob.decode("utf-16-le", "ignore").split("\x00")[0]


def list_directory(stream: BinaryIO, volume: FatVolume, cluster: int) -> list[Entry]:
    data = _directory_bytes(stream, volume, cluster)
    entries: list[Entry] = []
    pending: list[bytes] = []
    for offset in range(0, len(data) - 31, 32):
        raw = data[offset : offset + 32]
        if raw[0] == 0x00:  # end of directory: nothing after this is in use
            break
        if raw[0] == 0xE5:  # deleted
            pending = []
            continue
        attributes = raw[11]
        if attributes == ATTR_LONG_NAME:
            pending.append(raw)
            continue
        if attributes & ATTR_VOLUME_ID:  # a volume label is not a file
            pending = []
            continue
        first_cluster = (struct.unpack_from("<H", raw, 20)[0] << 16) | struct.unpack_from("<H", raw, 26)[0]
        entries.append(Entry(_long_name(pending), _short_name(raw), attributes, first_cluster))
        pending = []
    return entries


def find_path(stream: BinaryIO, volume: FatVolume, parts: tuple[str, ...]) -> Entry | None:
    """Follow a path component by component through the FAT directory tree."""
    cluster = volume.root
    entry: Entry | None = None
    for part in parts:
        entry = next((candidate for candidate in list_directory(stream, volume, cluster) if candidate.matches(part)), None)
        if entry is None:
            return None
        cluster = entry.cluster
    return entry


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------
def verify(path: Path) -> Report:
    """Read `path` and report whether it is a complete Windows install.

    A disk that exists but is not what we need (no GPT, no ESP, no boot file) is a
    verdict — an incomplete image — and comes back in the report. A disk that
    cannot be opened at all raises :class:`ImageError`, because that is a caller
    mistake rather than something wrong with the image.
    """
    report = Report(path=str(path))
    with open_disk(path) as stream:
        try:
            report.partitions = read_gpt(stream)
        except ImageError as exc:
            report.problems.append(str(exc))
            return report
        esps = [p for p in report.partitions if p.type_guid == ESP_TYPE_GUID]
        if not esps:
            report.problems.append("the disk has no EFI System Partition")
            return report
        for esp in esps:
            report.esp_checked += 1
            try:
                volume = open_fat(stream, esp.offset)
                found = find_path(stream, volume, BOOT_FILE)
            except ImageError as exc:
                report.problems.append(f"partition {esp.index} is not a readable ESP: {exc}")
                continue
            if found is not None and not found.is_directory:
                # Everything a Windows guest needs to boot is present. Report the
                # earliest good ESP and stop; a disk only needs one.
                report.boot_file = "\\".join(BOOT_FILE)
                report.problems.clear()
                return report
    report.problems.append(
        "no EFI System Partition holds \\" + "\\".join(BOOT_FILE) + " — the Windows image was "
        "applied but the install never finished, so the disk is not publishable"
    )
    return report


@contextmanager
def open_disk(path: Path) -> Iterator[BinaryIO]:
    """Yield a readable stream of the disk's raw bytes.

    A qcow2 is converted first with ``qemu-img`` (already a build dependency): its
    cluster indirection is not worth reimplementing to read four small regions. A
    raw image is opened in place. The temporary raw copy lives only as long as the
    stream.
    """
    if not path.exists():
        raise ImageError(f"{path} does not exist")
    if not path.is_file():
        raise ImageError(f"{path} is not a file")
    with path.open("rb") as probe:
        magic = probe.read(4)
    if magic != QCOW2_MAGIC:
        with path.open("rb") as stream:
            yield stream
        return
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise ImageError("reading a qcow2 needs `qemu-img`, which is not on PATH")
    with tempfile.TemporaryDirectory(prefix="ontrak-verify-") as tmp:
        raw = Path(tmp) / "disk.raw"
        result = subprocess.run(
            [qemu_img, "convert", "-O", "raw", str(path), str(raw)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ImageError(f"qemu-img convert failed: {result.stderr.strip() or result.stdout.strip()}")
        with raw.open("rb") as stream:
            yield stream


def resolve_input(target: Path) -> Path:
    """Accept a disk image, or an export directory that holds one."""
    if target.is_dir():
        for name in ("disk.qcow2", "rootfs.img", "disk.raw"):
            candidate = target / name
            if candidate.is_file():
                return candidate
        raise ImageError(f"{target} holds none of disk.qcow2, rootfs.img or disk.raw")
    return target


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def _print_report(report: Report) -> None:
    for problem in report.problems:
        print(f"verify-golden-image: {problem}")
    if report.ok:
        print(
            f"verify-golden-image: {report.path} is a complete Windows install "
            f"({len(report.partitions)} partition(s), ESP holds {report.boot_file})"
        )
        return
    if report.partitions:
        print("  partitions seen:")
        for partition in report.partitions:
            print(f"    - {partition.index}: {partition.describe()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a golden-image disk is a complete Windows install.",
        epilog=(
            "Succeeds only when an EFI System Partition carries \\EFI\\Microsoft\\Boot\\bootmgfw.efi. "
            "Point it at the export directory `infra/build-golden-image.sh` produced, or at the "
            "disk itself."
        ),
    )
    parser.add_argument("image", help="disk.qcow2/disk.raw, or a directory holding one")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        disk = resolve_input(Path(args.image))
        report = verify(disk)
    except ImageError as exc:
        print(f"verify-golden-image: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "path": report.path,
                    "ok": report.ok,
                    "boot_file": report.boot_file,
                    "esp_checked": report.esp_checked,
                    "problems": report.problems,
                    "partitions": [
                        {
                            "index": p.index,
                            "type": str(p.type_guid),
                            "name": p.name,
                            "first_lba": p.first_lba,
                            "last_lba": p.last_lba,
                            "size_bytes": p.size,
                        }
                        for p in report.partitions
                    ],
                },
                indent=2,
            )
        )
    else:
        _print_report(report)
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
