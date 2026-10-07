#!/usr/bin/env python3
"""Publishing the golden image with the oras binary named the way the docs say.

``infra/publish-golden-image.sh`` uploads a built export to a registry. It cannot be
run in a test for real -- that is a 12 GiB push to GHCR -- but the part of it that
*can* be checked is where it looks for ``oras``, because that is where it has been
wrong.

The layers have to be named relative to the export directory: ``oras`` refuses an
absolute layer path (it reads one as a path traversal), and the name it is given
becomes the title the puller writes on the other side. So the push runs from inside
the export directory. ``ONTRAK_ORAS``, meanwhile, is documented as "oras binary to
use (default: ``oras`` on PATH)", and the natural way to point it at a binary
downloaded into the checkout is ``ONTRAK_ORAS=build/bin/oras`` -- a *relative* path.
Checked from the project root that resolves, so the script's own ``command -v`` guard
passes and the login succeeds; then the push subshell changes directory and the same
path resolves against the export directory instead:

    infra/publish-golden-image.sh: line 137: build/bin/oras: No such file or directory

which is a failure that arrives after the token has been used and reads like the
binary is missing rather than relocated. The script pins a path-shaped ``oras`` to an
absolute one before anything changes directory, and these tests drive it with a stub
to prove the push still finds the binary and still runs from the export directory.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
SCRIPT = ROOT / "infra" / "publish-golden-image.sh"

# A stand-in for oras: records the directory it ran in and the arguments it was
# given, then succeeds. The publish script only ever runs `oras login` (skipped here
# because the stub `gh` below hands it no token) and `oras push`.
ORAS_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'argv0=%s\\n' "$0"
  for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
} > "$STUB_RECORD"
exit 0
"""

# Shadowing `gh` with something that has no token keeps the test offline and
# deterministic: with no token the script skips `oras login` entirely, so the stub
# only has to answer the push. A machine that happens to have `gh` installed and
# signed in would otherwise call GHCR from a unit test.
GH_STUB = """#!/bin/sh
exit 1
"""


class PublishGoldenTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-publish-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.oras = self.bin / "oras"
        self.oras.write_text(ORAS_STUB, encoding="utf-8")
        self.oras.chmod(self.oras.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        gh = self.bin / "gh"
        gh.write_text(GH_STUB, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # A split export, which is what `make golden` writes: metadata plus the disk.
        # Their contents do not matter -- the push is a stub, and verification is
        # skipped, because a real disk is a Windows install this test has no business
        # requiring.
        self.export = self.directory / "export"
        self.export.mkdir()
        (self.export / "incus.tar.xz").write_bytes(b"metadata")
        (self.export / "disk.qcow2").write_bytes(b"disk")

        self.record = self.directory / "oras-call.txt"

    def _publish(self, oras: str, **extra: str) -> subprocess.CompletedProcess:
        """Run the publish script from this directory, with `oras` named by ``oras``."""
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.directory),
            "STUB_RECORD": str(self.record),
            "ONTRAK_ORAS": oras,
            "ONTRAK_SKIP_VERIFY": "1",
            "ONTRAK_GOLDEN_REPOSITORY": "ghcr.io/example/ontrak-golden",
            "ONTRAK_GOLDEN_TAG": "test-tag",
        }
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT), "export"],
            cwd=self.directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _recorded(self) -> list[str]:
        self.assertTrue(
            self.record.is_file(),
            "the publish script never reached oras, so nothing was uploaded",
        )
        return self.record.read_text(encoding="utf-8").splitlines()

    def test_a_relative_oras_path_still_reaches_the_push(self):
        """The regression: `ONTRAK_ORAS=bin/oras`, from the checkout, as documented.

        Before the path was pinned, this is the case that failed and it failed late --
        after verification and after the login, so the operator saw a token used and a
        "No such file or directory" for a file that is plainly there.
        """
        result = self._publish("bin/oras")
        self.assertEqual(
            result.returncode,
            0,
            f"a relative ONTRAK_ORAS did not reach the push:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("published", result.stdout)
        lines = self._recorded()
        self.assertIn(f"argv0={self.oras}", lines, "oras was reached under some other name")

    def test_the_push_runs_from_the_export_directory_under_its_own_name(self):
        """Both halves of why the path has to be pinned, asserted together.

        The push has to run *inside* the export directory -- a puller has to get
        ``disk.qcow2``, not ``build/incus-windows/output/win11e/disk.qcow2`` -- and the
        oras it runs has to be the one that was asked for, not a path re-resolved from
        there.
        """
        result = self._publish("bin/oras")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertIn(f"cwd={self.export}", lines)
        self.assertIn("arg=disk.qcow2:application/vnd.ontrak.golden-image.disk.v1.qcow2", lines)
        self.assertIn("arg=incus.tar.xz:application/vnd.ontrak.golden-image.metadata.v1.tar.xz", lines)
        for line in lines:
            if line.startswith("arg=") and line.endswith("disk.qcow2"):
                self.assertNotIn("/", line.removeprefix("arg=").removesuffix("disk.qcow2"))

    def test_an_absolute_oras_path_is_left_alone(self):
        """The pinning must not break an operator who already passed an absolute path."""
        result = self._publish(str(self.oras))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"argv0={self.oras}", self._recorded())

    def test_a_bare_name_is_still_looked_up_on_path(self):
        """`ONTRAK_ORAS=oras` means "the one on PATH", and pinning must not undo that."""
        result = self._publish("oras")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"argv0={self.oras}", self._recorded())

    def test_a_missing_oras_is_still_reported_clearly(self):
        """The guard that made the original failure read as a missing binary stays."""
        result = self._publish("bin/absent-oras")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oras not found", result.stderr)
        self.assertFalse(self.record.is_file(), "something was uploaded with no oras")


if __name__ == "__main__":
    unittest.main()
