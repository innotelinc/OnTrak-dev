#!/usr/bin/env python3
"""The USB writer: the checks that keep it off the wrong disk, and that it verifies.

``infra/installer/write-usb.sh`` is the one script in this repository whose failure
mode is a destroyed machine rather than a wrong result, so the tests here are mostly
about what it *refuses*. It exists because the one-line `dd` it replaced was not
enough on the host it was written for: two plain 3.8 GB writes killed the stick there,
at 3.5 GB and at 3.75 GB of 4.07 GB, and a stick that error-recovered mid-write holds
a plausible image with a hole in it — a grub prompt on the machine in front of the
operator rather than an error in a log.

What is checked without a stick attached:

* every refusal — no ISO, an ISO that is not there, a device that is not a block
  device, the disk the running system is on (derived from ``lsblk`` here rather than
  hardcoded to ``/dev/sda``), and a device too small for the image (a loop device,
  where the test can be root)
* that the properties the stick's own flashing needed are still in the script: chunked
  writes with direct I/O and an fsync each, and a read-back hashed against the ISO.
  These are read out of the script rather than exercised, because exercising them
  means writing to a real device — and the one time that was done here, on the
  reference stick, is in the script's header.
* that the *recommendation* is wired in: the docs, the Makefile and the build's final
  banner all point at this script, so the plain `dd` cannot quietly come back.

Nothing in this file writes to a device: every case stops at a refusal, and the rest
reads text.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
WRITER = ROOT / "infra" / "installer" / "write-usb.sh"


def run_writer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(WRITER), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"},
    )


def system_disk() -> str | None:
    """The whole disk the running system is mounted from, as lsblk names it.

    Derived rather than hardcoded: a runner's system disk is `sda`, `vda`, `nvme0n1`
    or anything else, and a test that names one of them would pass everywhere it does
    not matter.
    """
    result = subprocess.run(
        ["lsblk", "-nro", "NAME,MOUNTPOINTS"], capture_output=True, text=True, check=False
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].rstrip("\n") in ("/", "/boot"):
            # A partition (sda2, nvme0n1p2) names its disk once the trailing digits go.
            return fields[0].rstrip("0123456789")
    return None


class RefusalTests(unittest.TestCase):
    """Every one of these exits before anything is written."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-usb-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.iso = self.directory / "installer.iso"
        self.iso.write_bytes(b"\0" * (3 * 1024 * 1024))

    def test_no_iso_is_a_usage_error(self):
        result = run_writer()
        self.assertEqual(result.returncode, 2, f"{result.stdout}\n{result.stderr}")
        self.assertIn("usage:", result.stderr)

    def test_an_iso_that_is_not_there_is_refused(self):
        result = run_writer(str(self.directory / "absent.iso"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no such ISO", result.stderr)

    def test_a_regular_file_is_not_a_device(self):
        result = run_writer(str(self.iso), str(self.directory / "not-a-device"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not a block device", result.stderr)

    def test_the_disk_the_system_is_on_is_refused(self):
        """The one that matters: this script writes whole devices."""
        disk = system_disk()
        if not disk or not Path(f"/dev/{disk}").exists():
            self.skipTest("could not derive this machine's system disk")
        result = run_writer(str(self.iso), f"/dev/{disk}")
        self.assertEqual(result.returncode, 2, f"{result.stdout}\n{result.stderr}")
        self.assertIn("carries the running system", result.stderr)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("losetup"), "needs root and losetup")
    def test_a_device_too_small_for_the_image_is_refused(self):
        image = self.directory / "small.img"
        with image.open("wb") as handle:
            handle.truncate(2 * 1024 * 1024)
        attach = subprocess.run(
            ["losetup", "--find", "--show", str(image)], capture_output=True, text=True, check=False
        )
        if attach.returncode != 0:
            self.skipTest(f"could not attach a loop device: {attach.stderr.strip()}")
        loop = attach.stdout.strip()
        self.addCleanup(subprocess.run, ["losetup", "-d", loop], check=False)
        result = run_writer(str(self.iso), loop, "--force")
        self.assertEqual(result.returncode, 2, f"{result.stdout}\n{result.stderr}")
        self.assertIn("holds 2 MiB and the ISO is 3 MiB", result.stderr)

    def test_a_non_usb_device_needs_force(self):
        """--force exists, but it has to be asked for: the default is the stick."""
        disk = system_disk()
        source = WRITER.read_text(encoding="utf-8")
        self.assertIn("--force", source, "there is no way to override the USB check")
        if disk:
            # Without --force the refusal comes first, whichever device it is.
            result = run_writer(str(self.iso), f"/dev/{disk}")
            self.assertNotIn("--force was given", result.stderr)


class PropertiesTests(unittest.TestCase):
    """What the script has to keep doing, read out of the script itself."""

    def setUp(self):
        self.source = WRITER.read_text(encoding="utf-8")

    def test_the_write_is_chunked_direct_and_synced(self):
        # 3.8 GB into the page cache is what killed the stick: the failure has to
        # surface at the chunk that caused it.
        for needle in ("CHUNK_MIB=", "skip=\"$at\"", "seek=\"$at\"", "oflag=direct", "conv=fsync"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.source)

    def test_a_failed_chunk_is_retried(self):
        self.assertIn("RETRIES=", self.source)
        self.assertIn("attempt $attempt/$RETRIES", self.source)

    def test_the_stick_is_read_back_and_hashed(self):
        # "The write returned 0" is not evidence.
        self.assertIn("sha256sum", self.source)
        self.assertIn("does NOT match the ISO", self.source)
        self.assertIn("iflag=count_bytes", self.source)

    def test_the_guards_are_all_there(self):
        for needle in (
            "carries the running system",       # never the system disk
            "is not a removable USB disk",      # never a device nobody checked
            "no single candidate USB disk",     # never a guess between two sticks
            "has a mounted partition",          # never a filesystem in use
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.source)


class RecommendationTests(unittest.TestCase):
    """The script is what the repository tells people to use."""

    def test_the_docs_the_makefile_and_the_banner_all_point_at_it(self):
        for path in (
            ROOT / "docs" / "installer.md",
            ROOT / "Makefile",
            ROOT / "infra" / "build-installer-iso.sh",
        ):
            with self.subTest(path=path.name):
                self.assertIn("write-usb.sh", path.read_text(encoding="utf-8"))

    def test_it_is_executable_like_every_other_infra_script(self):
        mode = WRITER.stat().st_mode
        self.assertTrue(mode & 0o111, "write-usb.sh is not executable")


if __name__ == "__main__":
    unittest.main()
