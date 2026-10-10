#!/usr/bin/env python3
"""The installer ISO's boot menu: one install entry per autoinstall profile.

``infra/build-installer-iso.sh`` remasters Ubuntu's live-server ISO and hands its
grub.cfg to ``infra/installer/patch-grub.py``. The build cannot be run in a test — it
downloads a 3.8 GiB ISO and repacks it — but the patch can, and it is the part with
the sharp edges: it rewrites a file Canonical generated, in a language (grub's menu)
that fails closed at the firmware prompt on the machine in front of the operator, not
in a log.

What the tests hold down, in the order they matter:

* the entries that load ``/casper/vmlinuz`` are the only ones touched. Ubuntu's menu
  also has ``Boot from next volume``, ``UEFI Firmware Settings`` and ``Test memory``,
  two of them inside an ``if [ "$grub_platform" = ... ]`` — a patch that rewrote those,
  or that swallowed the ``if``/``else``/``fi`` around them, would break the menu on one
  firmware and not the other.
* the **first** entry stays the unattended install on this machine's disk. It is
  grub's default after a 30-second timeout, so this is what an unanswered boot does —
  and it is what ``infra/installer/install-test.sh`` boots.
* the choose-disk entry boots ``toram``. That is not decoration: it is the entry used
  to install onto the stick the machine booted from, and without ``toram`` the
  installer would be erasing the filesystem it is running from.
* patching is idempotent, because a failed build is re-run against a tree that may
  already have been patched.
* a grub.cfg with no installer entry on it is a **loud** failure: that is what a base
  ISO which is not the live-server one looks like, and the alternative is an ISO whose
  menu does nothing.

The fixture is the menu of the real base image (Ubuntu 24.04.5 live-server), tabs and
all. It is here rather than trimmed to one entry so the ``else`` branch, the quoted
titles and the ``linux16`` memory tester are all in front of the patch — the shape
that a future release would change.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
PATCH = ROOT / "infra" / "installer" / "patch-grub.py"
RENDER = ROOT / "infra" / "installer" / "render-autoinstall.py"

# `xorriso -osirrox on -indev ubuntu-24.04.5-live-server-amd64.iso \
#    -extract /boot/grub/grub.cfg -` — the whole menu, unmodified.
BASE_GRUB_CFG = "\n".join(
    [
        "\tset timeout=30",
        "\t",
        "\tloadfont unicode",
        "\t",
        "\tset menu_color_normal=white/black",
        "\tset menu_color_highlight=black/light-gray",
        "\t",
        '\tmenuentry "Try or Install Ubuntu Server" {',
        "\t\tset gfxpayload=keep",
        "\t\tlinux\t/casper/vmlinuz  ---",
        "\t\tinitrd\t/casper/initrd",
        "\t}",
        '\tmenuentry "Ubuntu Server with the HWE kernel" {',
        "\t\tset gfxpayload=keep",
        "\t\tlinux\t/casper/hwe-vmlinuz  ---",
        "\t\tinitrd\t/casper/hwe-initrd",
        "\t}",
        "\tgrub_platform",
        '\tif [ "$grub_platform" = "efi" ]; then',
        "\tmenuentry 'Boot from next volume' {",
        "\t\texit 1",
        "\t}",
        "\tmenuentry 'UEFI Firmware Settings' {",
        "\t\tfwsetup",
        "\t}",
        "\telse",
        "\tmenuentry 'Test memory' {",
        "\t\tlinux16 /boot/memtest86+x64.bin",
        "\t}",
        "\tfi",
        "",
    ]
)

MACHINE_TITLE = "Install OnTrak on this machine's disk (unattended)"
CHOOSE_TITLE = "Install OnTrak on the disk you choose (USB stick, or another disk)"


def load_by_path(path: Path):
    """Import a hyphenated script — the same way test_authentik_provision.py does."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PatchGrubTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-grub-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.grub = self.directory / "grub.cfg"

    def _patch(self, text: str = BASE_GRUB_CFG, *extra: str) -> subprocess.CompletedProcess:
        self.grub.write_text(text, encoding="utf-8")
        return subprocess.run(
            ["python3", str(PATCH), str(self.grub), *extra],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def _patched(self) -> list[str]:
        return self.grub.read_text(encoding="utf-8").splitlines()

    def _menu_titles(self) -> list[str]:
        """`menuentry "…"` with the opening brace and indentation taken off."""
        titles = []
        for line in self._patched():
            stripped = line.strip()
            if stripped.startswith("menuentry"):
                titles.append(stripped[: stripped.rindex("{")].strip())
        return titles

    def _kernel_lines(self) -> list[str]:
        """The kernel lines of the installer entries — not `linux16 /boot/memtest86+`."""
        return [
            line
            for line in self._patched()
            if line.strip().startswith("linux") and "/casper/" in line
        ]

    # -- the entries that carry an installer -------------------------------

    def test_every_installer_entry_is_offered_for_both_targets(self):
        result = self._patch()
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        titles = self._menu_titles()
        # Two kernel variants (standard and HWE) times two destinations, and the four
        # Canonical entries they sit among.
        self.assertEqual(titles.count(f'menuentry "{MACHINE_TITLE}"'), 1)
        self.assertEqual(titles.count(f'menuentry "{MACHINE_TITLE} [HWE kernel]"'), 1)
        self.assertEqual(titles.count(f'menuentry "{CHOOSE_TITLE}"'), 1)
        self.assertEqual(titles.count(f'menuentry "{CHOOSE_TITLE} [HWE kernel]"'), 1)
        self.assertIn("menuentry 'Boot from next volume'", titles)
        self.assertIn("menuentry 'Test memory'", titles)

    def test_the_default_entry_is_still_the_unattended_install(self):
        """grub boots the first entry after its 30-second timeout without being asked."""
        self._patch()
        self.assertEqual(self._menu_titles()[0], f'menuentry "{MACHINE_TITLE}"')

    def test_the_machine_entry_carries_the_machine_autoinstall(self):
        self._patch()
        machine = [
            line for line in self._kernel_lines() if "ds=nocloud;s=/cdrom/nocloud/" in line
        ]
        self.assertEqual(len(machine), 2, "the machine autoinstall is not on both kernel entries")
        for line in machine:
            self.assertNotIn("toram", line, "an unattended install has no reason to copy the ISO into RAM")

    def test_the_choose_disk_entry_carries_its_own_autoinstall_and_toram(self):
        self._patch()
        choose = [
            line
            for line in self._kernel_lines()
            if "ds=nocloud;s=/cdrom/nocloud-choose-disk/" in line
        ]
        self.assertEqual(len(choose), 2, "the choose-disk autoinstall is not on both kernel entries")
        for line in choose:
            self.assertIn(
                "toram",
                line,
                "a choose-disk entry without toram cannot install onto the stick it booted from",
            )

    def test_the_console_and_the_autoinstall_survive_on_every_entry(self):
        """A headless machine is installed over serial; the smoke test reads that log."""
        self._patch()
        for line in self._kernel_lines():
            self.assertIn("console=ttyS0", line, line)
            self.assertIn('autoinstall "ds=nocloud;s=/cdrom', line, line)

    def test_the_datasource_argument_is_quoted(self):
        """Grub ends an argument at `;`, and that failure is completely silent.

        Unquoted, `autoinstall ds=nocloud;s=/cdrom/nocloud/ console=ttyS0` reaches the
        kernel as `autoinstall ds=nocloud`: no autoinstall is found (the installer asks
        every question), the console argument is dropped with it, and neither the grub
        prompt nor the kernel says a word about it. Measured on the built image by
        typing both forms at grub's command line.
        """
        self._patch()
        for dirname in ("/nocloud", "/nocloud-choose-disk"):
            with self.subTest(datasource=dirname):
                line = next(l for l in self._kernel_lines() if f"/cdrom{dirname}/" in l)
                self.assertIn(f'autoinstall "ds=nocloud;s=/cdrom{dirname}/"', line, line)
                # A bare `ds=nocloud;s=` would be truncated at the semicolon by grub.
                self.assertNotIn("autoinstall ds=nocloud;s=", line, line)

    def test_an_unquoted_datasource_from_an_older_image_is_normalised(self):
        """A re-run over an image built before the quoting must fix it, not double it."""
        self._patch()
        text = self.grub.read_text(encoding="utf-8").replace(
            'autoinstall "ds=nocloud;s=/cdrom/nocloud/"',
            "autoinstall ds=nocloud;s=/cdrom/nocloud/",
        )
        result = self._patch(text)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        patched = "\n".join(self._patched())
        self.assertNotIn("autoinstall ds=nocloud;s=", patched, "the unquoted form survived")

    def test_the_datasource_directories_are_the_ones_the_renderer_writes(self):
        """Two files, one pair of paths. The build passes them in; these are the defaults."""
        renderer = load_by_path(RENDER)
        self._patch()
        patched = self._patched()
        for profile, title in (("machine", MACHINE_TITLE), ("choose-disk", CHOOSE_TITLE)):
            datasource = renderer.DATASOURCES[profile]
            with self.subTest(profile=profile):
                self.assertTrue(
                    any(f"ds=nocloud;s=/cdrom{datasource}/" in line for line in patched),
                    f"{title} does not point at {datasource}, where the renderer writes it",
                )

    # -- what must be left alone ------------------------------------------

    def test_entries_without_an_installer_are_untouched(self):
        self._patch()
        text = "\n".join(self._patched())
        self.assertIn("\tmenuentry 'Boot from next volume' {\n\t\texit 1\n\t}", text)
        self.assertIn("\tmenuentry 'UEFI Firmware Settings' {\n\t\tfwsetup\n\t}", text)
        self.assertIn("\tmenuentry 'Test memory' {\n\t\tlinux16 /boot/memtest86+x64.bin\n\t}", text)
        self.assertIn('\tif [ "$grub_platform" = "efi" ]; then', text)
        self.assertIn("\telse", text)
        self.assertIn("\tfi", text)

    def test_a_quoted_brace_does_not_end_an_entry_early(self):
        """The scanner counts braces outside quotes; Canonical's menu has none, but a
        release that added one would otherwise lose the rest of the file."""
        text = BASE_GRUB_CFG.replace(
            '\tmenuentry "Try or Install Ubuntu Server" {',
            '\tmenuentry "Try or Install Ubuntu Server (with a } in the title)" {',
        )
        result = self._patch(text)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("\tfi", "\n".join(self._patched()))

    # -- re-runs, and the base image that is not this one ------------------

    def test_patching_twice_is_the_same_as_patching_once(self):
        self._patch()
        once = self.grub.read_text(encoding="utf-8")
        result = self._patch(once)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertEqual(self.grub.read_text(encoding="utf-8"), once)

    def test_a_menu_with_no_installer_entry_fails_loudly(self):
        result = self._patch("\tmenuentry 'Test memory' {\n\t\tlinux16 /boot/memtest86+x64.bin\n\t}\n")
        self.assertNotEqual(result.returncode, 0, "an ISO with no install entry was accepted")
        self.assertIn("no /casper/vmlinuz boot entry", result.stderr)


if __name__ == "__main__":
    unittest.main()
