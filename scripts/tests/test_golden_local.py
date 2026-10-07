#!/usr/bin/env python3
"""The first-logon script the golden build puts inside the guest.

``infra/windows/golden-local/main.ps1`` is what configures the WinRM listener in the
built image. It matters because the autounattend can only open the *firewall* for
it: on a clone that comes up on a Public network nothing listens, and OnTrak
provisions Windows over WinRM. See ``infra/incus-windows-pack.sh`` for why the two
halves are where they are, and ``scripts/tests/test_unattend_winrm.py`` for why the
listener half is a file on the ISO rather than a line in the answer file.

There are two ways this can silently stop working, and both are tested here:

* the script stops doing its job -- someone drops the ``-SkipNetworkProfileCheck``
  that makes it apply on Public, or the explicit ``-Profile Any`` rules, or adds an
  ``exit`` (which would end upstream's ``OEM/main.ps1`` early and skip the sysprep
  that shuts the build VM down, so the build would wait forever);
* the build stops putting it on the ISO. It reaches the guest only as
  ``tools/pack.sh``'s optional seventh argument -- the directory
  ``OEM/main.ps1`` dot-sources as ``local/main.ps1`` -- so the wiring is a single
  line in ``infra/build-golden-image.sh`` and a single word in a list.

The script itself is not executed here; PowerShell and Windows are not available.
These are assertions about its shape, which is the part a reviewer can break by
accident.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
SCRIPT = ROOT / "infra" / "windows" / "golden-local" / "main.ps1"
BUILD = ROOT / "infra" / "build-golden-image.sh"


class GoldenLocalScriptTests(unittest.TestCase):
    def setUp(self):
        if not SCRIPT.is_file():  # pragma: no cover - a missing file is a failure below
            self.fail(f"the first-logon script is missing: {SCRIPT}")
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_it_configures_the_listener_without_the_profile_check(self):
        """``-SkipNetworkProfileCheck`` is the whole reason the file exists.

        Without it Enable-PSRemoting declines to configure WinRM at all when the
        machine's interfaces are in the Public zone, which is the case this range
        is in.
        """
        self.assertIn("Enable-PSRemoting -SkipNetworkProfileCheck", self.text)
        self.assertNotIn(
            "Enable-PSRemoting -Force",
            self.text,
            "the profile check is back; on a Public network that configures nothing",
        )

    def test_it_opens_winrm_and_rdp_on_every_profile(self):
        self.assertIn("New-NetFirewallRule", self.text)
        self.assertIn("-Profile Any", self.text)
        for port in ("5985", "3389"):
            self.assertIn(port, self.text, f"port {port} is no longer opened")

    def test_it_never_ends_the_caller(self):
        """It is dot-sourced by OEM/main.ps1, which has sysprep left to run.

        An ``exit`` here would end that shell, skip ``sysprep /shutdown``, and leave
        the build waiting for a VM that never stops -- for as long as anyone leaves
        it. Comments may say the word; only a statement counts.
        """
        statements = [
            line.strip()
            for line in self.text.splitlines()
            if re.match(r"^\s*exit\b", line)
        ]
        self.assertEqual(statements, [], "an exit statement would skip the sysprep that stops the build VM")

    def test_every_step_is_contained(self):
        """Nothing in it may take the caller down either."""
        self.assertIn("$ErrorActionPreference = 'Continue'", self.text)
        self.assertGreaterEqual(self.text.count("catch {"), 2, "the steps that can fail are not all wrapped")

    def test_it_replaces_its_rules_instead_of_stacking_them(self):
        """Re-running it on an image that already has the rule must not add another."""
        self.assertIn("Remove-NetFirewallRule", self.text)

    def test_it_announces_itself(self):
        """The guest is unreachable while it runs, so it has to say what it did."""
        self.assertIn("ONTRAK-LOCAL-OK", self.text)


class GoldenLocalWiringTests(unittest.TestCase):
    """The build has to actually put the script on the unattended ISO."""

    def setUp(self):
        if not BUILD.is_file():  # pragma: no cover
            self.fail(f"the build script is missing: {BUILD}")
        self.text = BUILD.read_text(encoding="utf-8")

    def test_the_build_points_at_the_script_directory(self):
        match = re.search(r'^LOCAL="([^"]+)"', self.text, re.MULTILINE)
        self.assertIsNotNone(match, "infra/build-golden-image.sh no longer defines LOCAL")
        definition = match.group(1)
        self.assertIn("infra/windows/golden-local", definition)
        # The definition is relative to the project root at run time; the
        # directory itself is checked against this checkout.
        self.assertTrue(SCRIPT.is_file())

    def test_the_build_forwards_it_to_pack_sh(self):
        """build.sh forwards anything after the target straight to tools/pack.sh.

        That is the only way a file from this repository ends up inside the guest
        before the image is sealed: pack.sh copies the directory to ``local/`` on
        the ISO, and upstream's OEM/main.ps1 dot-sources ``local/main.ps1`` from it.
        """
        self.assertTrue(
            re.search(r'sh build\.sh "\$TARGET" "\$LOCAL"', self.text),
            "infra/build-golden-image.sh no longer passes LOCAL to build.sh, so the "
            "first-logon script never reaches the ISO and the image it builds has no "
            "WinRM listener",
        )

    def test_the_build_fails_loudly_when_the_script_is_gone(self):
        """A missing script is a broken image, not a warning."""
        self.assertTrue(
            re.search(r'\[\[ -f "\$\{LOCAL\}/main\.ps1" \]\]', self.text),
            "the build no longer checks that the first-logon script exists before spending "
            "two hours producing an unreachable image",
        )


if __name__ == "__main__":
    unittest.main()
