#!/usr/bin/env python3
"""The sizing tiers: one image per class of machine, from one tier file each.

``infra/installer/tiers/*.env`` are read twice: by ``infra/build-installer-iso.sh``,
for the tier's label and its hostname default, and by
``infra/installer/render-tier.py``, which writes the tier's files into the ISO tree.
Two readers of one file is exactly the shape that drifts — and this pair drifted for
real: the build used to source the tier file as shell, and a tier file is not shell
(``TIER_TITLE=OnTrak class range`` is an assignment followed by the command
``class``), so the title came back empty and the note was a syntax error.

So the tests hold down two things at once: that every tier in the repository is
usable, and that **both readers see the same bytes**. The second is a real command —
the build's own ``tier_value`` (a ``sed`` over the line) run against each key, and
compared with what the parser read.

The rest is the parser's own edges, each of which is a way a tier file could be
wrong quietly: a value that needs quoting, a pool ceiling below its own target, a
tier whose name does not match its file, a key that is a typo. Every one of them
would otherwise surface as a machine that installed with the wrong settings.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent
RENDER = ROOT / "infra" / "installer" / "render-tier.py"
TIERS = ROOT / "infra" / "installer" / "tiers"
README = ROOT / "infra" / "installer" / "README.txt"

REQUIRED = ("TIER_NAME", "TIER_LABEL", "TIER_TITLE", "TIER_STUDENTS", "TIER_MIN_CPU", "TIER_MIN_MEM_GIB", "TIER_HOSTNAME")


def load_by_path(path: Path):
    """Import a hyphenated script — the same way test_patch_grub.py does."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load_by_path(RENDER)


class RepoTiersTests(unittest.TestCase):
    """The tiers this repository ships, not a fixture of one."""

    def tier_files(self) -> list[Path]:
        return sorted(TIERS.glob("*.env"))

    def test_the_repository_ships_the_tiers_the_docs_name(self):
        names = sorted(path.stem for path in self.tier_files())
        self.assertEqual(names, ["class", "dev", "full"])

    def test_every_tier_is_usable(self):
        self.assertTrue(self.tier_files(), "no tier files to check")
        for path in self.tier_files():
            with self.subTest(tier=path.stem):
                values, _ = render.parse(path)
                render.validate(path.stem, path, values)

    def test_every_tier_says_which_machine_it_is_for(self):
        for path in self.tier_files():
            values, _ = render.parse(path)
            with self.subTest(tier=path.stem):
                for key in REQUIRED:
                    self.assertIn(key, values, f"{path} has no {key}")
                self.assertGreater(int(values["TIER_MIN_MEM_GIB"]), 0)
                self.assertGreater(int(values["TIER_MIN_CPU"]), 0)
                self.assertTrue(values["TIER_NOTE"], "a tier without its note says nothing at first boot")

    def test_both_readers_see_the_same_file(self):
        """The build reads a value with `sed -n "s/^KEY=//p"`; the renderer parses the
        line. A value the two read differently — one expanded, one literal, or one
        that needed quoting to survive a shell — would put one machine's settings on
        an image the build labelled with another's.

        This is the check that caught the pair drifting: sourcing the file as shell
        silently dropped every value with a space in it.
        """
        for path in self.tier_files():
            values, _ = render.parse(path)
            with self.subTest(tier=path.stem):
                for key, value in values.items():
                    # The build's `tier_value`, verbatim.
                    result = subprocess.run(
                        ["bash", "-c", f'sed -n "s/^{key}=//p" "{path}" | head -1'],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.rstrip("\n"), value, f"{key} reads differently")

    def test_the_build_reads_tier_files_without_a_shell(self):
        """The build must not `.` a tier file: it is plain settings, not shell.

        Sourcing one is how this pair drifted the first time — `TIER_TITLE=OnTrak
        class range` runs `class`, so the title came back empty and the note was a
        syntax error, while the parser read both perfectly.
        """
        build = (ROOT / "infra" / "build-installer-iso.sh").read_text(encoding="utf-8")
        self.assertIn("tier_value()", build, "the build does not read tiers plainly")
        self.assertNotIn('. "$TIER_FILE"', build, "the build sources a tier file as shell")
        self.assertNotIn("source \"$TIER_FILE\"", build, "the build sources a tier file as shell")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-tier-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _tier(self, text: str, name: str = "dev") -> Path:
        path = self.directory / f"{name}.env"
        path.write_text(text, encoding="utf-8")
        return path

    def _render(self, *extra: str) -> subprocess.CompletedProcess:
        tree = self.directory / "tree"
        (tree / "ontrak").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(README, tree / "ontrak" / "README.txt")
        return subprocess.run(
            [
                "python3",
                str(RENDER),
                "--tier",
                extra[0] if extra else "dev",
                "--tiers-dir",
                str(self.directory),
                "--iso-tree",
                str(tree),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def _valid(self, **overrides: str) -> str:
        """A usable tier file, with any key replaced — a duplicate key is not a
        usable way to override one, since it is rejected in its own right."""
        values = {
            "TIER_NAME": "dev",
            "TIER_LABEL": "dev",
            "TIER_TITLE": "OnTrak dev range",
            "TIER_STUDENTS": "1-2",
            "TIER_MIN_CPU": "4",
            "TIER_MIN_MEM_GIB": "15",
            "TIER_HOSTNAME": "ontrak-dev",
            "TIER_NOTE": "a note",
            "ONTRAK_POOL__DEFAULT_TARGET": "1",
            "ONTRAK_POOL__MAX_TOTAL": "1",
        }
        values.update(overrides)
        return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"

    def test_a_tier_file_is_parsed_into_its_keys(self):
        values, comments = render.parse(self._tier("# a reason\nTIER_HOSTNAME=ontrak-dev\n"))
        self.assertEqual(values, {"TIER_HOSTNAME": "ontrak-dev"})
        self.assertEqual(comments["TIER_HOSTNAME"], ["# a reason"])

    def test_a_quoted_value_is_rejected(self):
        """A quoted value would reach the build with its quotes on and the host without
        them: one file, two settings."""
        path = self._tier('TIER_TITLE="OnTrak dev"\n')
        with self.assertRaises(SystemExit) as caught:
            render.parse(path)
        self.assertIn("plain value", str(caught.exception))

    def test_a_value_that_expands_is_rejected(self):
        with self.assertRaises(SystemExit):
            render.parse(self._tier("TIER_HOSTNAME=ontrak-$HOSTNAME\n"))

    def test_a_key_that_is_neither_tier_nor_setting_is_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            render.parse(self._tier("ONTRAKX_POOL__MAX_TOTAL=1\n"))
        self.assertIn("neither a TIER_* nor an ONTRAK_* key", str(caught.exception))

    def test_a_missing_key_is_rejected(self):
        path = self._tier("TIER_NAME=dev\nTIER_LABEL=dev\n")
        values, _ = render.parse(path)
        with self.assertRaises(SystemExit) as caught:
            render.validate("dev", path, values)
        self.assertIn("no TIER_TITLE", str(caught.exception))

    def test_a_name_that_disagrees_with_the_file_is_rejected(self):
        path = self._tier("TIER_NAME=class\n")
        values, _ = render.parse(path)
        with self.assertRaises(SystemExit) as caught:
            render.validate("dev", path, values)
        self.assertIn("the file is dev.env", str(caught.exception))

    def test_a_pool_target_above_its_ceiling_is_rejected(self):
        path = self._tier(
            self._valid(ONTRAK_POOL__DEFAULT_TARGET="6", ONTRAK_POOL__MAX_TOTAL="2")
        )
        values, _ = render.parse(path)
        with self.assertRaises(SystemExit) as caught:
            render.validate("dev", path, values)
        self.assertIn("target above the ceiling", str(caught.exception))

    def test_a_floor_that_is_not_a_number_is_rejected(self):
        path = self._tier(self._valid(TIER_MIN_MEM_GIB="lots"))
        values, _ = render.parse(path)
        with self.assertRaises(SystemExit) as caught:
            render.validate("dev", path, values)
        self.assertIn("whole number", str(caught.exception))

    # -- what it writes into the ISO tree ----------------------------------

    def test_an_unknown_tier_names_the_ones_there_are(self):
        self._tier("TIER_NAME=dev\n")
        result = self._render("workstation")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no workstation.env", result.stderr)
        self.assertIn("there is: dev", result.stderr)

    def test_a_tier_writes_its_record_and_its_settings(self):
        self._tier(self._valid())
        result = self._render()
        self.assertEqual(result.returncode, 0, result.stderr)
        tree = self.directory / "tree" / "ontrak"
        record = (tree / "tier.env").read_text(encoding="utf-8")
        self.assertIn("TIER_NAME=dev", record)
        self.assertIn("TIER_MIN_MEM_GIB=15", record)
        settings = (tree / "firstboot.env").read_text(encoding="utf-8")
        self.assertIn("ONTRAK_POOL__DEFAULT_TARGET=1", settings)
        self.assertNotIn("TIER_NAME", settings, "the tier record and its settings are two files")
        # The settings file is what the host reads, so it has to be shell.
        sourced = subprocess.run(
            ["bash", "-c", f'set -a; . "{tree / "firstboot.env"}"; printf "%s" "$ONTRAK_POOL__MAX_TOTAL"'],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(sourced.stdout, "1", sourced.stderr)

    def test_a_tier_is_named_in_the_readme_once(self):
        self._tier(self._valid())
        self.assertEqual(self._render().returncode, 0)
        readme = self.directory / "tree" / "ontrak" / "README.txt"
        once = readme.read_text(encoding="utf-8")
        self.assertIn("SIZING TIER: dev", once)
        self.assertIn("at least 4 vCPU and 15 GiB", once)
        self.assertEqual(self._render().returncode, 0)
        self.assertEqual(readme.read_text(encoding="utf-8"), once, "the section was added twice")
        # ...and above the instructions that are the same for every image.
        self.assertLess(once.index("SIZING TIER"), once.index("This ISO installs"))

    def test_a_tier_without_settings_writes_no_firstboot_env(self):
        """A tier may describe a machine without changing what the host does."""
        self._tier("\n".join(line for line in self._valid().splitlines() if not line.startswith("ONTRAK_")) + "\n")
        result = self._render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bakes no settings", result.stdout)
        self.assertFalse((self.directory / "tree" / "ontrak" / "firstboot.env").exists())


if __name__ == "__main__":
    unittest.main()
