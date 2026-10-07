#!/usr/bin/env python3
"""The WinRM bootstrap this range adds to incus-windows' autounattend.

A Windows image nobody can reach cannot be provisioned, and OnTrak provisions
Windows over WinRM. The image incus-windows builds has WinRM enabled -- but only
for the Domain and Private firewall profiles, and a clone comes up on whatever
profile Windows decides for the lab bridge, which on every host measured here is
**Public**. Every inbound packet is then dropped: the guest has an address, it
answers ARP, and every port times out *without a RST*, 135 and 445 included.

The repair for that lives in ``infra/windows/post-install.ps1`` and is applied
*over WinRM, to a clone of the image* -- so it can only fix a guest that is already
reachable, and the step that opens the door is locked behind it. Hence this: the
same fix, moved into the install, where no network is involved.

It is in two pieces, and the split is the lesson this file now carries:

* ``pack_unattend_winrm`` adds a **declarative firewall group** to the
  autounattend, active on every profile. That is the mechanism upstream already
  uses for Remote Desktop, and being declarative it cannot fail at run time.
* the **listener** half -- ``Enable-PSRemoting -SkipNetworkProfileCheck``, without
  which opening a port opens it onto nothing -- is
  ``infra/windows/golden-local/main.ps1``, which upstream's own ``OEM/main.ps1``
  dot-sources from the unattended ISO at first logon. That is tested by
  ``scripts/tests/test_golden_local.py``.

Why the listener half is not a ``RunSynchronousCommand`` as well, which is what it
was first: an over-long ``Path`` does not fail the command, it **invalidates the
whole answer file**, and Windows Setup then blocks the installation. A
490-character one-liner produced

    [setup.exe] SMI data results dump: Source = .../RunSynchronousCommand/[Order="4"]/Path
    [setup.exe] SMI data results dump: Description = Value is invalid.
    Error [0x060432] IBS  The provided unattend file is not valid; hrResult = 0x80220005

and a guest that never rebooted, never ran sysprep, never wrote ``unattendgc``,
and sat at 0.6 of a core forever while ``click.py`` waited for a STOPPED that could
not come. Upstream's own commands on that pass are ~100 characters. So
``test_it_adds_no_command_to_the_answer_file`` fails if anything puts a command
back in, and ``test_every_command_stays_short_enough_for_windows_to_accept`` fails
if one gets long -- which is the shape of the mistake, not the mistake itself.

One more property matters, and it is about *not* breaking a two hour Windows
install: the rewritten autounattend is still **well-formed XML**. A malformed one
does not fail quietly; it fails Windows Setup. Every test below re-parses the file.

The real checkout is exercised as well when it is present, so this is not only
ever run against a fixture written by whoever wrote the rewrite; on CI, where the
checkout does not exist, that test skips.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
LIBRARY = ROOT / "infra" / "incus-windows-pack.sh"
# gitignored, and only present on a host that has built the image at least once.
REAL_UNATTEND = ROOT / "build" / "incus-windows" / "unattend" / "11e" / "Autounattend.xml"

WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"

# The longest Path Windows Setup was measured to accept is not known; what is known
# is that ~490 characters is rejected outright and that upstream's own commands,
# which work, are around 100. This bound is deliberately far below the failure point
# and far above anything a command that belongs here needs: if a rewrite ever wants
# more than this, it wants a script on the ISO instead.
MAX_COMMAND_LENGTH = 260

# The lines the rewrite touches, in the shape upstream writes them: the RDP
# firewall group this one is modelled on, and the specialize pass's last
# RunSynchronousCommand -- which is deliberately left alone, because the rewrite
# must not add one.
UPSTREAM_UNATTEND = f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component xmlns:wcm="{WCM}" language="neutral" name="Networking-MPSSVC-Svc" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <FirewallGroups>
        <FirewallGroup wcm:action="add" wcm:keyValue="RemoteDesktop">
          <Active>true</Active>
          <Group>Remote Desktop</Group>
          <Profile>all</Profile>
        </FirewallGroup>
      </FirewallGroups>
    </component>
    <component xmlns:wcm="{WCM}" language="neutral" name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Description>Prevent Automatic Device Encryption</Description>
          <Path>cmd.exe /c "reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\BitLocker /v PreventDeviceEncryption /t REG_DWORD /d 1 /f"</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
</unattend>
"""

WINRM_GROUP = "WindowsRemoteManagement"
# The marker the rewrite used to leave next to the command it injected. Its absence
# is the regression this module guards.
WINRM_COMMAND_MARKER = "ONTRAK_WINRM_COMMAND"

# The command an earlier version of this rewrite injected, in the shape it wrote it.
# A checkout is not reset between builds -- `git checkout <ref>` keeps local edits to
# tracked files -- so this is what a re-run against such a checkout finds, and the
# rewrite has to take it out rather than leave it to block the build again.
STALE_INJECTED_COMMAND = f'''        <!-- {WINRM_COMMAND_MARKER} -->
        <RunSynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Description>Make WinRM and RDP reachable on any network profile</Description>
          <Path>cmd.exe /c "powershell -NoProfile -ExecutionPolicy Bypass -Command 'try {{ Enable-PSRemoting -SkipNetworkProfileCheck -Force -ErrorAction Stop }} catch {{ }}; try {{ New-NetFirewallRule -DisplayName OnTrak-WinRM-5985 -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any -ErrorAction Stop }} catch {{ }}; exit 0'"</Path>
        </RunSynchronousCommand>
'''


def _run(function: str, path: Path) -> subprocess.CompletedProcess:
    """Call one rewrite against ``path``, the way the build calls it."""
    script = f"set -euo pipefail\n. {shlex.quote(str(LIBRARY))}\n{function} {shlex.quote(str(path))}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ET.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    found = _children(element, name)
    return found[0] if found else None


def _text(element: ET.Element, name: str) -> str:
    found = _child(element, name)
    return (found.text or "") if found is not None else ""


class UnattendWinrmTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-unattend-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _fixture(self, text: str = UPSTREAM_UNATTEND, name: str = "Autounattend.xml") -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def _apply(self, text: str = UPSTREAM_UNATTEND) -> Path:
        path = self._fixture(text)
        result = _run("pack_unattend_winrm", path)
        self.assertEqual(result.returncode, 0, f"the rewrite did not apply: {result.stderr}")
        # Everything below reads the result as XML, so a malformed file fails here
        # rather than in Windows Setup.
        return path

    def _tree(self, path: Path) -> ET.Element:
        try:
            return ET.parse(path).getroot()
        except ET.ParseError as exc:  # pragma: no cover - only on a broken rewrite
            self.fail(f"the rewritten autounattend is not valid XML: {exc}")

    def _run_synchronous_commands(self, root: ET.Element) -> list[ET.Element]:
        return [element for element in root.iter() if _local(element.tag) == "RunSynchronousCommand"]

    # ---------------------------------------------------------------- the edits --

    def test_winrm_is_opened_on_every_firewall_profile(self):
        """The whole point: the rule has to be active on Public too.

        Remote Desktop is already opened this way two lines above where this lands,
        so it is the same declarative mechanism -- which cannot fail at run time the
        way a command can.
        """
        path = self._apply()
        root = self._tree(path)
        groups = [
            element
            for element in root.iter()
            if _local(element.tag) == "FirewallGroup" and _attr(element, "keyValue") == WINRM_GROUP
        ]
        self.assertEqual(len(groups), 1, "expected exactly one WinRM firewall group")
        group = groups[0]
        self.assertEqual(_text(group, "Group"), "Windows Remote Management")
        self.assertEqual(_text(group, "Profile"), "all")
        self.assertEqual(_text(group, "Active"), "true")

    def test_the_group_sits_inside_the_firewall_groups_element(self):
        path = self._apply()
        root = self._tree(path)
        containers = {id(element) for element in root.iter() if _local(element.tag) == "FirewallGroups"}
        self.assertTrue(containers, "the fixture itself has no FirewallGroups element")
        placement = [
            parent
            for parent in root.iter()
            for child in parent
            if _local(child.tag) == "FirewallGroup" and _attr(child, "keyValue") == WINRM_GROUP
        ]
        self.assertTrue(
            any(id(parent) in containers for parent in placement),
            "the WinRM firewall group was added outside <FirewallGroups>, where Windows ignores it",
        )

    def test_it_adds_no_command_to_the_answer_file(self):
        """The mistake that cost a two hour build, pinned as a test.

        The listener half of this fix used to be injected here as a
        RunSynchronousCommand. Its ``Path`` was long enough that Windows rejected the
        entire answer file with ``0x80220005`` in the specialize pass and blocked the
        installation, leaving a guest that never shut down. The command now lives in
        ``infra/windows/golden-local/main.ps1``, which Setup does not parse, and
        nothing may put one back.
        """
        path = self._apply()
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(
            WINRM_COMMAND_MARKER,
            text,
            "the rewrite injected a RunSynchronousCommand again; its Path is length-limited and "
            "an over-long one invalidates the whole answer file (see this module's docstring)",
        )
        upstream = self._fixture()
        before = len(self._run_synchronous_commands(self._tree(upstream)))
        after = len(self._run_synchronous_commands(self._tree(path)))
        self.assertEqual(after, before, "the rewrite added a RunSynchronousCommand to the answer file")

    def _stale_fixture(self) -> Path:
        """Upstream's autounattend with the old injected command still in it."""
        anchor = "      </RunSynchronous>\n"
        text = UPSTREAM_UNATTEND.replace(anchor, STALE_INJECTED_COMMAND + anchor)
        self.assertIn(
            STALE_INJECTED_COMMAND,
            text,
            "the fixture lost the anchor the stale command is spliced into",
        )
        return self._fixture(text, name="stale-Autounattend.xml")

    def test_it_removes_the_command_an_earlier_version_injected(self):
        """Converging a checkout that a previous build already patched.

        This is the case that would otherwise survive every fix: the build updates
        the checkout with `git checkout`, which does not discard local edits, so the
        bad command stays in the file the next build installs Windows from.
        """
        path = self._stale_fixture()
        result = _run("pack_unattend_winrm", path)
        self.assertEqual(result.returncode, 0, f"the rewrite refused a stale checkout: {result.stderr}")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(WINRM_COMMAND_MARKER, text)
        root = self._tree(path)
        commands = self._run_synchronous_commands(root)
        upstream = len(self._run_synchronous_commands(self._tree(self._fixture())))
        self.assertEqual(
            len(commands),
            upstream,
            "the injected RunSynchronousCommand is still there; Windows would reject the answer "
            "file in the specialize pass again",
        )
        self.assertIn(WINRM_GROUP, text, "removing the command also dropped the firewall group")
        self.assertIn("</unattend>", text, "the file was truncated rather than edited")

    def test_a_marker_with_nothing_to_remove_does_not_truncate_the_file(self):
        """A half-removed command must not take the rest of the file with it.

        The removal runs from the marker to the next closing tag. If that closing tag
        is gone -- an edit that was interrupted, say -- deleting to end of file would
        produce an answer file that fails Windows Setup in the same pass, with the
        same silence. It has to report the anchor as moved instead.
        """
        anchor = "      </RunSynchronous>\n"
        truncated = UPSTREAM_UNATTEND.replace(anchor, STALE_INJECTED_COMMAND.rsplit("</RunSynchronousCommand>", 1)[0] + anchor)
        path = self._fixture(truncated, name="half-stale-Autounattend.xml")
        before = path.read_text(encoding="utf-8")
        result = _run("pack_unattend_winrm", path)
        self.assertNotEqual(result.returncode, 0, "a marker with nothing after it was reported as patched")
        self.assertEqual(path.read_text(encoding="utf-8"), before, "the rewrite truncated the answer file")

    def test_every_command_stays_short_enough_for_windows_to_accept(self):
        """A length bound on the values Windows parses, so the failure is caught here.

        Not a guess about where the limit is -- a bound far below the measured
        failure and far above what belongs in the answer file. ``Path`` values are
        what Windows validates; the driver paths the virtio pass adds are included
        because they live in the same element.
        """
        path = self._apply()
        self._assert_commands_are_short(path)

    def _assert_commands_are_short(self, path: Path) -> None:
        root = self._tree(path)
        commands = self._run_synchronous_commands(root)
        self.assertTrue(commands, "no RunSynchronousCommand at all; the fixture changed")
        for command in commands:
            text = _text(command, "Path")
            self.assertLessEqual(
                len(text),
                MAX_COMMAND_LENGTH,
                f"a RunSynchronousCommand Path of {len(text)} characters is over the "
                f"{MAX_COMMAND_LENGTH}-character bound this range keeps; Windows truncates "
                "nothing and rejects the whole answer file instead. Move the work to "
                "infra/windows/golden-local/main.ps1.",
            )

    # ------------------------------------------------------------ idempotence --

    def test_it_is_a_no_op_the_second_time(self):
        path = self._apply()
        once = path.read_text(encoding="utf-8")
        for _ in range(3):
            result = _run("pack_unattend_winrm", path)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), once, "a second run changed the file")
        self.assertEqual(once.count(WINRM_GROUP), 1)

    # -------------------------------------------------------- anchors that moved --

    def test_a_moved_firewall_anchor_is_reported_not_swallowed(self):
        moved = UPSTREAM_UNATTEND.replace("      </FirewallGroups>\n", "")
        path = self._fixture(moved)
        result = _run("pack_unattend_winrm", path)
        self.assertNotEqual(
            result.returncode,
            0,
            "an autounattend with no FirewallGroups element was reported as patched; the "
            "image would then build and be unreachable",
        )

    def test_a_rewrite_refuses_a_file_it_cannot_read(self):
        result = _run("pack_unattend_winrm", self.directory / "absent.xml")
        self.assertNotEqual(result.returncode, 0)

    # ------------------------------------------------------- the real autounattend --

    @unittest.skipUnless(REAL_UNATTEND.is_file(), "no incus-windows checkout on this host")
    def test_the_real_autounattend_applies_and_stays_valid_xml(self):
        """The fixture is a reading of upstream; this is upstream.

        It matters more here than anywhere else in this range: the file it rewrites
        is the one Windows Setup parses, and a mistake in it is not a failed build
        but a failed operating system -- which is exactly what the length bound
        below caught the hard way.
        """
        path = self._fixture(REAL_UNATTEND.read_text(encoding="utf-8"), name="real-Autounattend.xml")
        result = _run("pack_unattend_winrm", path)
        self.assertEqual(result.returncode, 0, f"the rewrite did not apply to the real file: {result.stderr}")
        root = self._tree(path)
        self.assertTrue(
            [
                element
                for element in root.iter()
                if _local(element.tag) == "FirewallGroup" and _attr(element, "keyValue") == WINRM_GROUP
            ],
            "the real autounattend has no WinRM firewall group after the rewrite",
        )
        self.assertNotIn(
            WINRM_COMMAND_MARKER,
            REAL_UNATTEND.read_text(encoding="utf-8"),
            f"this host's checkout still carries the injected command in {REAL_UNATTEND}; it is the "
            "file that blocked a build's Windows install. Re-sync the checkout before building.",
        )
        self._assert_commands_are_short(path)
        once = path.read_text(encoding="utf-8")
        again = _run("pack_unattend_winrm", path)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), once)


if __name__ == "__main__":
    unittest.main()
