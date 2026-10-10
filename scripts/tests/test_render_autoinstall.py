#!/usr/bin/env python3
"""The installer ISO's two autoinstalls, and why they are one template.

``infra/build-installer-iso.sh`` cannot be run in a test — it downloads a 3 GiB
Ubuntu ISO, remasters it and optionally boots it. The part of it that decides what
the *installed machine* is, though, is ``infra/installer/render-autoinstall.py``,
and that runs anywhere.

Two properties are what matter.

* Both profiles are **real**: the ISO carries ``machine`` (this machine's disk,
  identity the only screen) and ``choose-disk`` (the same install, storage screen
  up too, which is how an operator installs onto a USB stick). Both land in the
  datasource directory the boot entries name, with the ``meta-data`` the nocloud
  datasource requires beside them, and the rendered YAML parses.

* They differ **only** in which screens stay up. That is the whole reason they are
  one template with a profile: a second copy of the file is how the two quietly
  stop being the same install, and the failure lands on whichever profile is used
  less — the USB one, which has no fallback entry to compare against.

A leftover ``@TOKEN@`` is the third failure worth pinning: the installer ignores a
key it does not recognise and installs with a default nobody chose, so it has to be
fatal at render time instead.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
RENDER = ROOT / "infra" / "installer" / "render-autoinstall.py"
AUTOINSTALL = ROOT / "infra" / "installer" / "autoinstall"
TEMPLATE = AUTOINSTALL / "user-data.dist"
META_DATA = AUTOINSTALL / "meta-data"

PASSWORD_HASH = "$6$ontrak$0123456789abcdef"


class RenderAutoinstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-autoinstall-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.tree = self.directory / "iso"
        self.tree.mkdir()

    def _render(
        self, profile: str, template: Path | None = None, hostname: str = "ontrak-range"
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "python3",
                str(RENDER),
                "--profile",
                profile,
                "--iso-tree",
                str(self.tree),
                "--template",
                str(template or TEMPLATE),
                "--meta-data",
                str(META_DATA),
                "--username",
                "ontrak",
                "--hostname",
                hostname,
                "--password-hash",
                PASSWORD_HASH,
                "--release",
                "24.04.5",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def _autoinstall(self, profile: str) -> tuple[dict, str]:
        """Render a profile, assert it succeeded, and return its parsed autoinstall."""
        result = self._render(profile)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        # The path the build patches the boot entries with is what it prints.
        datasource = result.stdout.strip().splitlines()[-1]
        self.assertTrue(
            datasource.startswith("/"),
            f"the renderer did not print a datasource directory: {datasource!r}",
        )
        user_data = self.tree / datasource.lstrip("/") / "user-data"
        self.assertTrue(user_data.is_file(), f"{datasource}/user-data was not written")
        return yaml.safe_load(user_data.read_text(encoding="utf-8")), datasource

    # -- both profiles are real ------------------------------------------

    def test_machine_profile_installs_on_this_machines_disk_after_identity_alone(self):
        autoinstall, datasource = self._autoinstall("machine")
        self.assertEqual(datasource, "/nocloud")
        config = autoinstall["autoinstall"]
        self.assertEqual(config["interactive-sections"], ["identity"])
        self.assertEqual(config["storage"]["layout"]["name"], "lvm")
        self.assertEqual(config["identity"]["username"], "ontrak")
        self.assertEqual(config["identity"]["hostname"], "ontrak-range")
        self.assertEqual(config["identity"]["password"], PASSWORD_HASH)
        self.assertEqual(config["version"], 1)

    def test_choose_disk_profile_leaves_the_storage_screen_up(self):
        """The target disk is the operator's decision — an installer cannot know
        which disk is the USB stick somebody booted from."""
        autoinstall, datasource = self._autoinstall("choose-disk")
        self.assertEqual(datasource, "/nocloud-choose-disk")
        config = autoinstall["autoinstall"]
        self.assertEqual(config["interactive-sections"], ["identity", "storage"])

    def test_each_datasource_gets_the_meta_data_nocloud_requires(self):
        for profile, datasource in (("machine", "/nocloud"), ("choose-disk", "/nocloud-choose-disk")):
            with self.subTest(profile=profile):
                self._autoinstall(profile)
                written = self.tree / datasource.lstrip("/") / "meta-data"
                self.assertEqual(written.read_text(encoding="utf-8"), META_DATA.read_text(encoding="utf-8"))

    # -- ... and they are the same install --------------------------------

    def test_the_two_profiles_differ_only_in_which_screens_stay_up(self):
        """One template with two profiles, so the install cannot drift per profile.

        Compared as configuration rather than as text: the template's own comments
        mention ``@INTERACTIVE_SECTIONS@``, so the two renderings legitimately differ
        in prose. What must not differ is a key the installer acts on.
        """
        machine, _ = self._autoinstall("machine")
        choose, _ = self._autoinstall("choose-disk")

        self.assertEqual(machine["autoinstall"]["interactive-sections"], ["identity"])
        self.assertEqual(choose["autoinstall"]["interactive-sections"], ["identity", "storage"])

        # With that one key levelled, the rest of the install has to be identical.
        choose["autoinstall"]["interactive-sections"] = machine["autoinstall"]["interactive-sections"]
        self.assertEqual(
            machine,
            choose,
            "the two autoinstalls differ somewhere other than interactive-sections",
        )

    # -- a token nobody replaced is fatal ---------------------------------

    def test_a_token_the_renderer_does_not_know_is_fatal(self):
        template = self.directory / "user-data.dist"
        template.write_text(
            TEMPLATE.read_text(encoding="utf-8") + "\n  # @NOT_A_VALUE@\n", encoding="utf-8"
        )
        result = self._render("machine", template=template)
        self.assertNotEqual(result.returncode, 0, "a leftover token was rendered into the ISO")
        self.assertIn("@NOT_A_VALUE@", result.stderr)

    def test_a_hostname_that_looks_like_a_token_is_refused_not_rendered(self):
        """A value substituted in one pass may not re-introduce a token silently.

        ``@NOT_A_VALUE@`` is not a valid hostname, so refusing it is right — the
        point of the test is that the refusal happens here rather than in an install
        that used a default nobody chose.
        """
        result = self._render("machine", hostname="@NOT_A_VALUE@")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("@NOT_A_VALUE@", result.stderr)

    def test_an_unknown_profile_is_refused(self):
        result = self._render("something-else")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("choose-disk", result.stderr, "the refusal does not name the profiles")


if __name__ == "__main__":
    unittest.main()
