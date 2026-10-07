#!/usr/bin/env python3
"""The rewrites the golden build makes to incus-windows' tools/pack.sh.

``infra/build-golden-image.sh`` cannot be run in a test: it wants incus, a
hypervisor that can virtualise SMM, a 5 GiB Windows evaluation ISO and a couple of
hours. So the part of it that *can* be checked — the edits it makes to the pinned
third-party ``tools/pack.sh`` — lives in ``infra/incus-windows-pack.sh``, and this
drives that file the way the build does: source it, call one rewrite against a
pack.sh, read the result.

Two properties are worth more than the rest.

* A rewrite **applies**. A ``sed`` whose anchor has moved is a build that quietly
  runs with upstream's defaults instead: 4 vCPU on a host that has four cores, a
  60 GiB disk whose publish wedges Incus's own database, and an EXIT trap that
  deletes a disk holding three hours of Windows. So the tests that matter as much
  as the applying ones are the two that remove an anchor and assert the failure is
  *reported* rather than swallowed.

* A rewrite is **idempotent**. A build that failed is re-run against the same
  checkout, so applying a rewrite twice has to be a no-op. It once was not:
  ``io.cache=none`` was appended on every run, and a checkout that had been patched
  four times carried the line five times.

The fixture is the upstream lines these rewrites touch, at the commit the local
checkout is on (``ae3b612``), tabs and all — upstream indents its shell blocks with
them, and a rewrite that only matched spaces would look perfect against a fixture
that had been tidied. When a real checkout is present every rewrite is run against
the real file as well, so this is not only ever exercised against a copy; on CI,
where the checkout does not exist, that test skips.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
LIBRARY = ROOT / "infra" / "incus-windows-pack.sh"
# gitignored, and only present on a host that has built the image at least once.
REAL_PACK_SH = ROOT / "build" / "incus-windows" / "tools" / "pack.sh"

# tools/pack.sh at upstream ae3b612, trimmed to the lines the rewrites touch plus
# their neighbours — the neighbours are there so an over-eager ``sed`` has
# something wrong to hit. ``apparmr | incus config set`` in particular is the line
# a "comment out the Secure Boot set" rewrite must not touch.
UPSTREAM_PACK_SH = "\n".join(
    [
        "#!/bin/sh",
        "set -eu",
        "",
        "TMPDIR=\"${PROJROOT}/tmp/\"",
        "",
        "name=build$(head -c6 /dev/urandom | od -tx1 -vAn | xargs printf %s)",
        "cleanup() {",
        "\tincus image rm \"${name}\" || :",
        "\tincus delete -f \"${name}\"",
        "}",
        "trap cleanup EXIT INT QUIT TERM",
        "",
        "printf '[+] Launching the VM\\n'",
        "",
        "incus init \"${name}\" --empty --vm -c security.secureboot=false -c limits.cpu=4 -c limits.memory=8GB -c image.os=windows -d root,size=30GiB",
        "incus config device set \"${name}\" root io.bus=virtio-blk",
        "incus config device add \"${name}\" iso disk source=\"${WINDIR}/${WINFILE}\" boot.priority=10",
        "incus config device add \"${name}\" incusagent disk source=\"agent:config\"",
        "apparmr | incus config set \"${name}\" raw.apparmor=-",
        "",
        "if [ X11e = X\"${VERSION}\" ]; then",
        "\tincus config device add \"${name}\" tpm tpm",
        "\tincus config device set \"${name}\" root size=60GiB",
        "\tincus config set \"${name}\" security.secureboot=true",
        "fi",
        "",
        "python3 \"${PROGBASE}/click.py\" \"${name}\"",
        "",
    ]
) + "\n"

# Every rewrite, with the arguments the build passes. Used against the real
# checkout, where re-listing them per test would only invite a typo.
REWRITES = (
    ("pack_vm_size", ("2", "4GB")),
    ("pack_disk_cache_none", ()),
    ("pack_disk_size", ("32GiB",)),
    ("pack_keep_vm", ("ontrak-winpack-build",)),
    ("pack_no_secureboot", ()),
    ("pack_qemu_accel", ()),
)


def _run(function: str, path: Path, *args: str) -> subprocess.CompletedProcess:
    """Call one rewrite against ``path``, the way the build calls it."""
    call = " ".join(shlex.quote(part) for part in (function, str(path), *args))
    # `set -euo pipefail` because that is the shell this file is sourced into when
    # it matters; a rewrite that only worked without it would not be the one the
    # build runs. The function is the last command, so its status is the status.
    script = f"set -euo pipefail\n. {shlex.quote(str(LIBRARY))}\n{call}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


class PackShRewriteTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-packsh-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _fixture(self, text: str = UPSTREAM_PACK_SH, name: str = "pack.sh") -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def _apply(self, function: str, *args: str, text: str = UPSTREAM_PACK_SH) -> Path:
        """Write a fixture, apply one rewrite to it, and assert it applied."""
        path = self._fixture(text)
        result = _run(function, path, *args)
        self.assertEqual(result.returncode, 0, f"{function} did not apply: {result.stderr}")
        return path

    def _lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines()

    def _the(self, path: Path, needle: str) -> str:
        for line in self._lines(path):
            if needle in line:
                return line
        raise AssertionError(f"no line in {path.name} contains {needle!r}")

    # ------------------------------------------------------------ the rewrites --

    def test_the_build_vm_is_sized(self):
        path = self._apply("pack_vm_size", "2", "4GB")
        line = self._the(path, "incus init")
        self.assertIn("limits.cpu=2", line)
        self.assertIn("limits.memory=4GB", line)
        self.assertNotIn("limits.cpu=4 ", line)
        self.assertNotIn("limits.memory=8GB", line)

    def test_the_write_cache_bypass_follows_the_bus_and_is_written_once(self):
        path = self._apply("pack_disk_cache_none")
        lines = self._lines(path)
        bus = next(index for index, line in enumerate(lines) if "root io.bus=virtio-blk" in line)
        self.assertIn("io.cache=none", lines[bus + 1])
        self.assertEqual("\n".join(lines).count("root io.cache=none"), 1)

    def test_the_disk_is_sized_at_creation_and_the_resize_is_dropped(self):
        path = self._apply("pack_disk_size", "32GiB")
        text = path.read_text(encoding="utf-8")
        self.assertIn("incus init \"${name}\" ", text)
        self.assertIn("-d root,size=32GiB", text)
        self.assertNotIn("size=30GiB", text)
        self.assertNotIn("root size=60GiB", text)

    def test_the_build_vm_is_named_and_never_deleted(self):
        path = self._apply("pack_keep_vm", "ontrak-winpack-build")
        lines = self._lines(path)
        self.assertIn("name=ontrak-winpack-build", lines)
        self.assertFalse(
            [line for line in lines if "incus delete" in line],
            "the build VM can still be deleted out from under a failed build",
        )
        # The image rm is pack.sh cleaning up the image it published under the same
        # name. That one should stay: the export is what this range imports.
        self.assertTrue([line for line in lines if "incus image rm" in line])

    def test_secure_boot_and_the_tpm_are_turned_off(self):
        path = self._apply("pack_no_secureboot")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("incus config device add \"${name}\" tpm tpm", text)
        self.assertNotIn("security.secureboot=true", text)
        self.assertIn("# ONTRAK_NO_SECUREBOOT: no TPM on the build VM", text)
        self.assertIn("# ONTRAK_NO_SECUREBOOT: no Secure Boot on the build VM", text)
        # Upstream's own default on the init line is not what is being turned off.
        self.assertIn("-c security.secureboot=false", text)
        # And a line that merely mentions `incus config set` is left alone.
        self.assertIn("apparmr | incus config set \"${name}\" raw.apparmor=-", text)

    def test_the_accelerator_hook_lands_before_the_installer_starts(self):
        """The accelerator has to be in place before `click.py` boots Windows.

        Everything above it in pack.sh is still just configuration; `click.py` is
        where the VM is started, so a hook after it would be a hook that runs
        against a guest that has already failed to start.
        """
        path = self._apply("pack_qemu_accel")
        lines = self._lines(path)
        hook = next(index for index, line in enumerate(lines) if "ONTRAK_QEMU_ACCEL_BEGIN" in line)
        installer = next(index for index, line in enumerate(lines) if "click.py" in line)
        self.assertLess(hook, installer, "the accelerator is set after the VM is started")
        text = path.read_text(encoding="utf-8")
        self.assertIn("ONTRAK_QEMU_ACCEL_END", text)
        # The rewrite calls the shared helper rather than deciding anything: the
        # templates and the student sessions ask ontrak/qemu.py the same question.
        self.assertIn('"${ONTRAK_QEMU_ACCEL_HELPER}" --apply "${name}"', text)
        # Unset helper means this host has KVM and the hook does nothing at all.
        self.assertIn('\tif [ -n "${ONTRAK_QEMU_ACCEL_HELPER:-}" ]; then', text)

    def test_the_accelerator_hook_is_not_added_again(self):
        path = self._apply("pack_qemu_accel")
        once = path.read_text(encoding="utf-8")
        for _ in range(3):
            result = _run("pack_qemu_accel", path)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), once)
        self.assertEqual(once.count("ONTRAK_QEMU_ACCEL_BEGIN"), 1)

    # ------------------------------------------------------------ idempotence --

    def test_every_rewrite_is_a_no_op_the_second_time(self):
        path = self._fixture()
        for function, args in REWRITES:
            first = _run(function, path, *args)
            self.assertEqual(first.returncode, 0, f"{function} did not apply: {first.stderr}")
        once = path.read_text(encoding="utf-8")
        for function, args in REWRITES:
            again = _run(function, path, *args)
            self.assertEqual(again.returncode, 0, f"{function} refused on a second run: {again.stderr}")
        self.assertEqual(path.read_text(encoding="utf-8"), once, "a rewrite changed the file on a second run")

    def test_the_write_cache_bypass_is_not_added_again(self):
        """The specific doubling that was measured, kept as its own test.

        The line was appended on every run before it was guarded, so a checkout
        patched four times carried `io.cache=none` five times.
        """
        path = self._apply("pack_disk_cache_none")
        for _ in range(3):
            result = _run("pack_disk_cache_none", path)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8").count("root io.cache=none"), 1)

    # --------------------------------------------------- anchors that moved --

    def test_a_moved_sizing_anchor_is_reported_not_swallowed(self):
        moved = UPSTREAM_PACK_SH.replace("limits.cpu=4 -c limits.memory=8GB", "limits.cpus=4")
        path = self._fixture(moved)
        result = _run("pack_vm_size", path, "2", "4GB")
        self.assertNotEqual(
            result.returncode,
            0,
            "a pack.sh with no sizing line was reported as sized; the build would "
            "run with upstream's 4 vCPU and never say so",
        )
        self.assertEqual(path.read_text(encoding="utf-8"), moved, "the file was edited anyway")

    def test_a_moved_vm_name_anchor_is_reported_not_swallowed(self):
        moved = UPSTREAM_PACK_SH.replace(
            "name=build$(head -c6 /dev/urandom | od -tx1 -vAn | xargs printf %s)",
            "vm_name=build$RANDOM",
        )
        path = self._fixture(moved)
        result = _run("pack_keep_vm", path, "ontrak-winpack-build")
        self.assertNotEqual(
            result.returncode,
            0,
            "a pack.sh that no longer names its VM as expected was reported as pinned",
        )

    def test_a_pack_sh_with_no_installer_call_is_reported_not_swallowed(self):
        moved = UPSTREAM_PACK_SH.replace(
            'python3 "${PROGBASE}/click.py" "${name}"', "sh run-windows-install"
        )
        path = self._fixture(moved)
        result = _run("pack_qemu_accel", path)
        self.assertNotEqual(
            result.returncode,
            0,
            "a pack.sh that no longer starts Windows with click.py was reported as "
            "hooked; on a host that cannot virtualise SMM the build would then die "
            "at 'KVM: entry failed' with nothing having said why",
        )
        self.assertEqual(path.read_text(encoding="utf-8"), moved, "the file was edited anyway")

    def test_a_rewrite_refuses_a_file_it_cannot_read(self):
        result = _run("pack_keep_vm", self.directory / "absent.sh", "ontrak-winpack-build")
        self.assertNotEqual(result.returncode, 0)

    # ------------------------------------------------------- the real checkout --

    @unittest.skipUnless(REAL_PACK_SH.is_file(), "no incus-windows checkout on this host")
    def test_the_real_checkout_applies_every_rewrite_and_stays_patched(self):
        """The fixture is a reading of upstream; this is upstream.

        It matters because a fixture is written by whoever wrote the rewrite, so it
        can only ever agree with it. The real file is tab-indented, has trailing
        lines between the anchors, and is the thing that will actually be built.
        """
        path = self._fixture(REAL_PACK_SH.read_text(encoding="utf-8"), name="real-pack.sh")
        for function, args in REWRITES:
            result = _run(function, path, *args)
            self.assertEqual(result.returncode, 0, f"{function} did not apply to the real checkout: {result.stderr}")
        once = path.read_text(encoding="utf-8")
        self.assertNotIn("name=build$(", once)
        self.assertNotIn("size=60GiB", once)
        self.assertIn("-d root,size=32GiB", once)
        for function, args in REWRITES:
            again = _run(function, path, *args)
            self.assertEqual(again.returncode, 0, f"{function} refused on a second run: {again.stderr}")
        self.assertEqual(path.read_text(encoding="utf-8"), once)


if __name__ == "__main__":
    unittest.main()
