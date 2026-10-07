#!/usr/bin/env python3
"""Publishing scenario templates without building them, and without a registry.

``infra/publish-templates.sh`` exports every built template (``incus export``) and
pushes it to a registry, so a host that cannot build one can still run it. A real run
is hours of Windows guests and gigabytes of push, so what is checked here is the part
that has been wrong before and the part that is easiest to get subtly wrong:

* where the script looks for ``oras``. ``ONTRAK_ORAS`` is documented as "oras binary
  to use (default: ``oras`` on PATH)", and pointing it at a binary inside the
  checkout -- ``ONTRAK_ORAS=build/bin/oras`` -- is a *relative* path. The push runs
  from inside the export directory, so a relative path would resolve there instead
  and fail late, after the login, looking like the binary is missing. Same trap and
  same fix as ``infra/publish-golden-image.sh`` (see ``test_publish_golden.py``).
* that the layer is named by file name and pushed from inside the export directory.
  ``oras`` refuses an absolute layer path, and the name it is handed becomes the
  title the puller writes -- the importing host reads back ``<name>.tar.gz``, so a
  path from this host would arrive on the other side as a directory path.
* that a ``tpl-`` instance which is *not* a built template -- no ``clean`` snapshot --
  is refused before anything is exported, because exporting a 12 GiB disk only to
  discover the mistake is the expensive way to learn it.

All of it is driven through an ``incus`` stub, so nothing here needs a hypervisor.
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
SCRIPT = ROOT / "infra" / "publish-templates.sh"

# A stand-in for oras: records the directory it ran in, the name it was reached by
# and every argument, then succeeds. Appended rather than overwritten, because a run
# with no arguments pushes one layer per template. The script only ever runs
# `oras login` (skipped here, because the `gh` stub below hands it no token) and
# `oras push`.
ORAS_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  printf 'argv0=%s\\n' "$0"
  for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
  printf 'end-call\\n'
} >> "$STUB_RECORD"
exit 0
"""

# Shadowing `gh` with something that has no token keeps the test offline: with no
# token the script skips `oras login` entirely, so the push is the only call.
GH_STUB = """#!/bin/sh
exit 1
"""

# The hypervisor, stubbed to the answers the script actually asks for: which
# templates exist, whether one is present, whether it carries the `clean` snapshot,
# the pool's name, and the export itself. Every call is recorded, so a test can
# assert both what was asked and what was never asked.
INCUS_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_INCUS_RECORD"
# Drop `--project <name>` the way the real client consumes it.
args=""
while [ $# -gt 0 ]; do
  case "$1" in
    --project|-p) shift 2 ;;
    *) args="$args $1"; shift ;;
  esac
done
# shellcheck disable=SC2086
set -- $args
cmd="${1:-}"
shift || true
case "$cmd" in
  list)
    # `list <name> --format=csv -c s` asks one instance's status; a bare
    # `list --format=csv -c n` asks for the names of all of them.
    if [ -n "${1:-}" ] && [ "${1#--format}" = "$1" ]; then
      printf 'STOPPED\\n'
    else
      cat "$STUB_INCUS_TEMPLATES"
    fi
    ;;
  info) exit 0 ;;
  snapshot) cat "$STUB_INCUS_SNAPSHOTS" ;;
  profile) cat "$STUB_INCUS_POOL" ;;
  export) printf 'a backup of %s\\n' "$1" > "$2" ;;
esac
exit 0
"""


class PublishTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-publish-templates-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.oras = self._stub(self.bin / "oras", ORAS_STUB)
        self._stub(self.bin / "gh", GH_STUB)
        self._stub(self.bin / "incus", INCUS_STUB)

        # Where the export is written. Pinned so the assertions can name it; on a real
        # host the script prefers the project's own storage pool, which is the large
        # filesystem while / usually is not.
        self.workdir = self.directory / "export"
        self.workdir.mkdir()

        self.templates = self.directory / "templates.txt"
        self.snapshots = self.directory / "snapshots.txt"
        self.pool = self.directory / "pool.txt"
        self.templates.write_text("tpl-alpha\ntpl-beta\n", encoding="utf-8")
        self.snapshots.write_text("clean\n", encoding="utf-8")
        self.pool.write_text("main-pool\n", encoding="utf-8")

        self.record = self.directory / "oras-call.txt"
        self.incus_record = self.directory / "incus-calls.txt"

    def _stub(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def _publish(
        self, *names: str, oras: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.directory),
            "STUB_RECORD": str(self.record),
            "STUB_INCUS_RECORD": str(self.incus_record),
            "STUB_INCUS_TEMPLATES": str(self.templates),
            "STUB_INCUS_SNAPSHOTS": str(self.snapshots),
            "STUB_INCUS_POOL": str(self.pool),
            "ONTRAK_ORAS": oras if oras is not None else "oras",
            "ONTRAK_TEMPLATE_WORKDIR": str(self.workdir),
            "ONTRAK_TEMPLATE_REPOSITORY": "ghcr.io/example/ontrak-template",
        }
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT), *names],
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

    # -- the oras path trap ------------------------------------------------

    def test_a_relative_oras_path_still_reaches_the_push(self):
        """The regression: `ONTRAK_ORAS=bin/oras`, from the checkout, as documented."""
        result = self._publish("tpl-alpha", oras="bin/oras")
        self.assertEqual(
            result.returncode,
            0,
            f"a relative ONTRAK_ORAS did not reach the push:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn(f"argv0={self.oras}", self._recorded(), "oras was reached under some other name")

    def test_an_absolute_oras_path_is_left_alone(self):
        result = self._publish("tpl-alpha", oras=str(self.oras))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"argv0={self.oras}", self._recorded())

    def test_a_bare_name_is_still_looked_up_on_path(self):
        result = self._publish("tpl-alpha", oras="oras")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"argv0={self.oras}", self._recorded())

    def test_a_missing_oras_is_reported_before_anything_is_exported(self):
        result = self._publish("tpl-alpha", oras="bin/absent-oras")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oras not found", result.stderr)
        self.assertFalse(self.record.is_file(), "something was uploaded with no oras")
        self.assertFalse(
            self.incus_record.is_file(),
            "the guard ran after the export had already started",
        )

    # -- what actually gets pushed ----------------------------------------

    def test_the_layer_is_pushed_from_inside_the_export_directory_by_file_name(self):
        """Both halves of why the naming matters, asserted together.

        A puller has to receive ``tpl-alpha.tar.gz`` -- not a path from this host --
        and ``oras`` refuses an absolute layer path, so the push has to run inside the
        export directory, under the very binary that was asked for.
        """
        result = self._publish("tpl-alpha")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        lines = self._recorded()
        self.assertIn(f"cwd={self.workdir}", lines)
        self.assertIn("arg=ghcr.io/example/ontrak-template:tpl-alpha", lines)
        self.assertIn("arg=--artifact-type", lines)
        self.assertIn("arg=application/vnd.ontrak.template.v1", lines)
        self.assertIn(
            "arg=tpl-alpha.tar.gz:application/vnd.ontrak.template.backup.v1.tar.gz",
            lines,
            "the layer was not named relative to the export directory",
        )

    def test_the_export_is_instance_only(self):
        """Exporting the snapshot too would store every template twice.

        The ``clean`` snapshot is the same disk as the instance it was taken from, so
        each template would be pushed at double size for no gain -- the importing host
        takes the snapshot again, where it is a cheap copy-on-write copy.
        """
        result = self._publish("tpl-alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.incus_record.read_text(encoding="utf-8")
        exports = [line for line in calls.splitlines() if " export " in line]
        self.assertEqual(len(exports), 1, calls)
        self.assertIn("--instance-only", exports[0])
        self.assertIn("--force", exports[0])

    def test_every_template_is_published_when_none_is_named(self):
        result = self._publish()
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        lines = self._recorded()
        self.assertIn("arg=ghcr.io/example/ontrak-template:tpl-alpha", lines)
        self.assertIn("arg=ghcr.io/example/ontrak-template:tpl-beta", lines)

    def test_the_tag_prefix_is_honoured(self):
        result = self._publish("tpl-alpha", ONTRAK_TEMPLATE_TAG_PREFIX="v1-")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertIn("arg=ghcr.io/example/ontrak-template:v1-tpl-alpha", lines)
        self.assertIn(
            "arg=tpl-alpha.tar.gz:application/vnd.ontrak.template.backup.v1.tar.gz",
            lines,
            "the tag prefix belongs in the tag, not in the file name",
        )

    # -- refusing what is not a template -----------------------------------

    def test_an_instance_without_the_clean_snapshot_is_refused_before_the_export(self):
        """A `tpl-` instance that never finished building is not a template.

        The expensive mistake this guards is exporting a 12 GiB Windows disk and only
        then noticing the fault was never applied -- the snapshot is what `ontrak`
        clones, so its absence is the definition of "not built".
        """
        self.snapshots.write_text("prebuild\n", encoding="utf-8")
        result = self._publish("tpl-alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean", result.stderr)
        self.assertFalse(self.record.is_file(), "a half-built template was pushed anyway")
        self.assertNotIn(" export ", self.incus_record.read_text(encoding="utf-8"))

    def test_publishing_nothing_is_an_error_not_a_traceback(self):
        """An empty `incus list` means "build them first", not a silent success."""
        self.templates.write_text("", encoding="utf-8")
        result = self._publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no templates found", result.stderr)


if __name__ == "__main__":
    unittest.main()
