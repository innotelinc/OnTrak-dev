#!/usr/bin/env python3
"""Pulling a published golden image into a directory the import can trust.

``infra/pull-golden-image.sh`` fetches the artifact ``infra/publish-golden-image.sh``
uploaded. It cannot be run against a registry in a test (that is a multi-gigabyte
download), so the parts that *can* be checked are the decisions it makes before and
after the pull, and those are the ones with consequences:

* **Which tag.** The publish side defaults to ``win11e-<today>``, so a puller that
  defaulted to the same thing would ask for a tag no other day pushed, and the
  registry's answer to that -- ``unauthorized``, because a private package does not
  admit whether a tag exists -- reads like a credential problem. The puller takes the
  newest ``win11e-*`` tag the repository actually holds, and ignores anything else in
  there.

* **Which directory.** ``make golden-import`` verifies *the disk it finds*. An export
  directory holding an older ``disk.qcow2`` therefore passes that check while not
  being what was just pulled -- and the image every template and session is cloned
  from changes underneath the range. So a destination with anything in it is refused
  unless the operator says so.

* **What landed.** A pull that reports success and leaves one of the two layers is
  the same hazard, one level down. Both are checked by the names the publish side
  pushes, and a missing one stops with a message rather than an invitation to import.

* **What it does not do.** It never imports. That is ``make golden-import``, and a
  test asserts ``incus`` is not run here, because "the puller has grown an import"
  is the kind of drift a stub can catch and a reviewer cannot.

The stub ``oras`` records the directory and arguments it was given, answers
``repo tags`` from the environment, and writes the layers a real pull would write.
The stub ``gh`` has no token, so the login is skipped and nothing here touches the
network.
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
SCRIPT = ROOT / "infra" / "pull-golden-image.sh"

# A stand-in for oras. It records every invocation (so a test can assert what was
# asked for, and in which directory), answers `repo tags` from STUB_TAGS, and -- on a
# `pull` -- writes the layers named in STUB_PULL_FILES into the `-o` directory, which
# is what the publish side's two layers become on the other end.
ORAS_STUB = """#!/bin/sh
{
  printf 'cwd=%s\\n' "$PWD"
  for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
  printf 'end\\n'
} >> "$STUB_RECORD"

if [ "${1:-}" = "repo" ] && [ "${2:-}" = "tags" ]; then
  [ -n "${STUB_TAGS:-}" ] && printf '%s\\n' "$STUB_TAGS"
  exit 0
fi

if [ "${1:-}" = "pull" ]; then
  dir="."
  prev=""
  for arg in "$@"; do
    [ "$prev" = "-o" ] && dir="$arg"
    prev="$arg"
  done
  mkdir -p "$dir"
  for layer in ${STUB_PULL_FILES:-disk.qcow2 incus.tar.xz}; do
    : > "$dir/$layer"
  done
fi
exit 0
"""

# No token to hand the script, so `oras login` is skipped: deterministic and offline
# even on a machine that happens to have `gh` installed and signed in.
GH_STUB = """#!/bin/sh
exit 1
"""

# Importing is `make golden-import`'s job. If this puller ever runs incus, the record
# exists and the test that says so fails.
INCUS_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_INCUS_RECORD"
exit 1
"""


class PullGoldenTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-pull-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name, body in (("oras", ORAS_STUB), ("gh", GH_STUB), ("incus", INCUS_STUB)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self.record = self.directory / "oras-calls.txt"
        self.incus_record = self.directory / "incus-calls.txt"
        self.destination = self.directory / "golden-export"

    def _pull(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.directory),
            "STUB_RECORD": str(self.record),
            "STUB_INCUS_RECORD": str(self.incus_record),
            "ONTRAK_GOLDEN_REPOSITORY": "ghcr.io/example/ontrak-golden",
            "ONTRAK_GOLDEN_PULL_DIR": str(self.destination),
        }
        env.update(extra)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=self.directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _calls(self) -> list[list[str]]:
        """The recorded invocations, split at each recorded `end`."""
        if not self.record.is_file():
            return []
        calls: list[list[str]] = []
        current: list[str] = []
        for line in self.record.read_text(encoding="utf-8").splitlines():
            if line == "end":
                calls.append(current)
                current = []
            else:
                current.append(line)
        return calls

    def _arguments(self) -> list[str]:
        return [line for call in self._calls() for line in call if line.startswith("arg=")]

    def test_a_named_tag_is_pulled_into_the_destination(self):
        result = self._pull("win11e-2026-10-06")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        arguments = self._arguments()
        self.assertIn("arg=pull", arguments)
        self.assertIn("arg=ghcr.io/example/ontrak-golden:win11e-2026-10-06", arguments)
        self.assertIn("arg=-o", arguments)
        self.assertIn(f"arg={self.destination}", arguments)
        self.assertIn(f"make golden-import ARGS={self.destination}", result.stdout)
        self.assertTrue((self.destination / "disk.qcow2").is_file())

    def test_the_default_is_the_newest_published_tag(self):
        """Not today's date: the publish default is, and a host set up on another day
        would ask for a tag that was never pushed."""
        result = self._pull(
            STUB_TAGS="win11e-2026-09-30\nnotes\nwin11e-2026-10-01\nwin11e-2026-10-09"
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        arguments = self._arguments()
        self.assertIn("arg=pull", arguments)
        self.assertIn("arg=ghcr.io/example/ontrak-golden:win11e-2026-10-09", arguments)
        self.assertNotIn("arg=ghcr.io/example/ontrak-golden:notes", arguments)

    def test_a_repository_with_no_golden_tag_says_so(self):
        result = self._pull(STUB_TAGS="notes\narchive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("win11e-", result.stderr)
        self.assertNotIn("arg=pull", self._arguments(), "a pull ran with no tag chosen")

    def test_a_list_prints_the_tags_and_pulls_nothing(self):
        result = self._pull("--list", STUB_TAGS="win11e-2026-10-01\nwin11e-2026-10-09")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], ["win11e-2026-10-01", "win11e-2026-10-09"])
        self.assertNotIn("arg=pull", self._arguments())

    def test_a_destination_with_anything_in_it_is_refused(self):
        """The hazard: the import verifies the disk it finds, and an older disk is a
        complete Windows install too."""
        self.destination.mkdir()
        (self.destination / "disk.qcow2").write_bytes(b"last week's disk")
        result = self._pull("win11e-2026-10-06")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not empty", result.stderr)
        self.assertNotIn("arg=pull", self._arguments(), "it pulled over an existing export anyway")
        self.assertEqual((self.destination / "disk.qcow2").read_bytes(), b"last week's disk")

    def test_force_pulls_over_a_used_destination(self):
        self.destination.mkdir()
        (self.destination / "disk.qcow2").write_bytes(b"last week's disk")
        result = self._pull("win11e-2026-10-06", ONTRAK_GOLDEN_FORCE="1")
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("arg=pull", self._arguments())
        self.assertEqual((self.destination / "disk.qcow2").read_bytes(), b"")

    def test_a_pull_that_leaves_no_disk_stops_instead_of_inviting_an_import(self):
        result = self._pull("win11e-2026-10-06", STUB_PULL_FILES="incus.tar.xz")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a complete export", result.stderr)
        self.assertNotIn("make golden-import", result.stdout)

    def test_it_never_imports(self):
        """Importing verifies the disk and republishes the alias; that is
        `make golden-import`, and a puller that grew an import would do it unasked."""
        result = self._pull("win11e-2026-10-06")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            self.incus_record.is_file(),
            f"the puller ran incus ({self.incus_record.read_text() if self.incus_record.is_file() else ''})",
        )

    def test_help_prints_the_header(self):
        """A command an operator retypes needs its usage in the file they read first,
        and `--help` has to agree with it."""
        result = self._pull("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Pull a published golden image", result.stdout)
        self.assertIn("ONTRAK_GOLDEN_PULL_DIR", result.stdout)
        self.assertNotIn("#", result.stdout.splitlines()[0], "the header was printed with its comment markers")
        self.assertNotIn("arg=pull", self._arguments())

    def test_an_unknown_option_is_reported(self):
        result = self._pull("--tag", "win11e-2026-10-06")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option", result.stderr)
        self.assertNotIn("arg=pull", self._arguments())


if __name__ == "__main__":
    unittest.main()
