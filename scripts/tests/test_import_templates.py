#!/usr/bin/env python3
"""Restoring scenario templates on a host that cannot build them.

``infra/import-templates.sh`` is the other half of ``infra/publish-templates.sh``: it
pulls each published backup with ``oras``, restores it with ``incus import``, and then
re-does the two things that are facts about *this* host rather than about the scenario
-- the ``clean`` snapshot the range clones, and the QEMU accelerator the guest has to
run on here.

Three parts of it are worth pinning down without a registry and without a hypervisor:

* the same relative-``oras`` trap the publish side documents. ``oras pull`` runs from
  inside the import directory, so a relative ``ONTRAK_ORAS`` has to be pinned first or
  the pull fails after the login, looking like a missing binary (see
  ``test_publish_templates.py``).
* that an existing template is not silently overwritten. Re-importing would drop a
  template students may be running, so it is a skip unless ``ONTRAK_TEMPLATE_FORCE``
  says otherwise.
* that the accelerator is re-applied to virtual machines and never to containers:
  ``raw.qemu.conf`` is a VM setting, and Incus rejects it on a container, which is what
  would make every Linux template unimportable.
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
SCRIPT = ROOT / "infra" / "import-templates.sh"

# Stands in for oras. `repo tags` answers with what the registry holds; `pull` writes
# the layer the way a real pull names it -- after the tag's title -- so the script has
# the `<name>.tar.gz` it goes on to import. Every call is recorded.
ORAS_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_RECORD"
case "${1:-}" in
  repo) printf 'tpl-alpha\\ntpl-beta\\n' ;;
  pull)
    printf 'cwd=%s\n' "$PWD" >> "$STUB_RECORD"
    ref="${2:-}"
    name="${ref##*:}"
    printf 'a backup of %s\\n' "$name" > "$name.tar.gz"
    ;;
esac
exit 0
"""

# No token, so `oras login` is skipped and the test stays offline.
GH_STUB = """#!/bin/sh
exit 1
"""

# The hypervisor. Answers what the script asks: whether an instance is already here
# (STUB_INCUS_INFO_EXIT: 0 present, 1 absent), what kind it is, and the import and
# snapshot calls themselves, which are only recorded.
INCUS_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_INCUS_RECORD"
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
  info) exit "${STUB_INCUS_INFO_EXIT:-1}" ;;
  list)
    if [ -n "${1:-}" ] && [ "${1#--format}" = "$1" ]; then
      cat "$STUB_INCUS_KIND"
    fi
    ;;
esac
exit 0
"""

# Stands in for the python that infra/qemu-accel.sh execs (`ONTRAK_PY` is the script's
# own documented hook), so the accelerator branch can be observed without a
# hypervisor. It records the module call it was handed and succeeds.
PY_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_PY_RECORD"
exit 0
"""


class ImportTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-import-templates-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.oras = self._stub(self.bin / "oras", ORAS_STUB)
        self._stub(self.bin / "gh", GH_STUB)
        self._stub(self.bin / "incus", INCUS_STUB)
        self.python = self._stub(self.bin / "accel-python", PY_STUB)

        self.workdir = self.directory / "import"
        self.workdir.mkdir()
        self.kind = self.directory / "kind.txt"
        self.kind.write_text("container\n", encoding="utf-8")

        self.record = self.directory / "oras-call.txt"
        self.incus_record = self.directory / "incus-calls.txt"
        self.py_record = self.directory / "python-call.txt"

    def _stub(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def _import(
        self, *names: str, oras: str | None = None, present: bool = False, **extra: str
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.directory),
            "STUB_RECORD": str(self.record),
            "STUB_INCUS_RECORD": str(self.incus_record),
            "STUB_INCUS_KIND": str(self.kind),
            "STUB_INCUS_INFO_EXIT": "0" if present else "1",
            "STUB_PY_RECORD": str(self.py_record),
            "ONTRAK_PY": str(self.python),
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

    def _oras_calls(self) -> list[str]:
        self.assertTrue(
            self.record.is_file(),
            "the import script never reached oras, so nothing was pulled",
        )
        return self.record.read_text(encoding="utf-8").splitlines()

    def _incus_calls(self) -> str:
        return self.incus_record.read_text(encoding="utf-8") if self.incus_record.exists() else ""

    # -- the oras path trap ------------------------------------------------

    def test_a_relative_oras_path_reaches_the_pull_from_the_import_directory(self):
        """The twin of the publish-side regression, with the pull as the late failure.

        ``oras pull`` also runs from inside the working directory, so a relative
        ``ONTRAK_ORAS`` has to be pinned before the script changes directory.
        """
        result = self._import("tpl-alpha", oras="bin/oras")
        self.assertEqual(
            result.returncode,
            0,
            f"a relative ONTRAK_ORAS did not reach the pull:\n{result.stdout}\n{result.stderr}",
        )
        calls = self._oras_calls()
        self.assertIn("pull ghcr.io/example/ontrak-template:tpl-alpha", calls)

    def test_the_pull_is_run_from_the_working_directory(self):
        """The tarball has to land where the import reads it from, not beside it."""
        result = self._import("tpl-alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cwd={self.workdir}", self._oras_calls())
        self.assertIn(f"import {self.workdir}/tpl-alpha.tar.gz tpl-alpha", self._incus_calls())

    # -- what gets restored -------------------------------------------------

    def test_a_named_template_is_pulled_imported_and_snapshotted(self):
        result = self._import("tpl-alpha")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        calls = self._incus_calls()
        self.assertIn("import", calls)
        self.assertIn("snapshot create tpl-alpha clean", calls)
        self.assertIn("restored 1", result.stdout)

    def test_no_arguments_restores_every_tag_in_the_repository(self):
        result = self._import()
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("restored 2", result.stdout)
        calls = self._incus_calls()
        self.assertIn("snapshot create tpl-alpha clean", calls)
        self.assertIn("snapshot create tpl-beta clean", calls)

    def test_an_existing_template_is_skipped_rather_than_overwritten(self):
        """A template here may be in use; replacing it is an explicit decision."""
        result = self._import("tpl-alpha", present=True)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("skipping", result.stdout)
        self.assertFalse(self.record.is_file(), "an existing template was pulled over")
        self.assertIn("restored 0", result.stdout)

    def test_force_replaces_an_existing_template(self):
        result = self._import("tpl-alpha", present=True, ONTRAK_TEMPLATE_FORCE="1")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        calls = self._incus_calls()
        self.assertIn("delete tpl-alpha", calls)
        self.assertIn("import", calls)
        self.assertIn("restored 1", result.stdout)

    # -- the part that is a fact about this host ----------------------------

    def test_a_container_is_not_given_a_qemu_accelerator(self):
        """`raw.qemu.conf` is a VM setting; Incus rejects it on a container.

        Every Linux scenario template is a container, so re-applying the accelerator
        unconditionally would make fourteen of the twenty-one unimportable.
        """
        result = self._import("tpl-alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            self.py_record.is_file(),
            "the accelerator was applied to a container",
        )

    def test_a_virtual_machine_is_re_asked_for_this_host_accelerator(self):
        """A guest built on a KVM host carries no override, and its clones would ask
        for KVM on a host that needed the import because it cannot give it."""
        self.kind.write_text("virtual-machine\n", encoding="utf-8")
        result = self._import("tpl-alpha")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertTrue(self.py_record.is_file(), "the accelerator was never re-applied")
        self.assertIn(
            "-m ontrak.qemu apply tpl-alpha ontrak",
            self.py_record.read_text(encoding="utf-8"),
            "the accelerator was pointed at something other than the restored template",
        )


if __name__ == "__main__":
    unittest.main()
