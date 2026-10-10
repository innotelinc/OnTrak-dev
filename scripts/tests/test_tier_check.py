#!/usr/bin/env python3
"""What a sizing tier does to a machine that is not the machine it was built for.

The installer ISO can be built as a tier — ``dev``, ``class``, ``full`` — and the
tier says which machine it is for and bakes the settings that go with it, including
the warm pool's target and ceiling. ``infra/installer/tier-check.py`` runs at first
boot on the host it landed on, and the whole reason it exists is the case in the
second half of that: **an image on a smaller machine than its tier.**

That case is not hypothetical and it is not cosmetic. ``pool.max_total`` is the
range's RAM guard rail (docs/operations.md): a ceiling above what the host can hold
is how a range host starts swapping during a class, and the symptom arrives through
the portal rather than in a log. So the check clamps the pool to
``(RAM − overhead) ÷ 4 GiB``, and what the tests hold down is that arithmetic, its
edges (a machine with no room at all), and the property that makes the clamp
trustworthy:

* **a machine at its tier's floor is not clamped.** The tiers name a floor and a
  pool; the pool has to be reachable on that floor, or the tier is a lie and the
  first boot quietly reduces it. This is checked against the tier files in the
  repository, not a fixture — the numbers that have to agree are the ones shipped.

The report is checked too, because it is the only thing an operator sees: the host's
first-boot log is where "your image is a class tier and this machine is a laptop"
gets said.
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
CHECK = ROOT / "infra" / "installer" / "tier-check.py"
TIERS = ROOT / "infra" / "installer" / "tiers"

PER_VM = 4
OVERHEAD = 8


def load_by_path(path: Path):
    """Import a hyphenated script — the same way test_patch_grub.py does."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load_by_path(CHECK)


class ArithmeticTests(unittest.TestCase):
    """RAM ≈ overhead + max(live, pool) × 4 GiB — the page's own formula."""

    def fits(self, mem_gib: int) -> tuple[int | None, int | None, int]:
        return check.effective_pool(1, 1, mem_gib, overhead_gib=OVERHEAD, per_vm_gib=PER_VM)

    def test_the_documented_fifteen_gib_host_keeps_its_one_warm_machine(self):
        """docs/operations.md's own example: 4 vCPU / 15 GiB, target 1, ceiling 1."""
        self.assertEqual(check.effective_pool(1, 1, 15, overhead_gib=OVERHEAD, per_vm_gib=PER_VM), (1, 1, 1))

    def test_a_class_tier_on_a_small_machine_is_clamped_to_what_it_holds(self):
        target, ceiling, fits = check.effective_pool(4, 6, 16, overhead_gib=OVERHEAD, per_vm_gib=PER_VM)
        self.assertEqual((target, ceiling, fits), (2, 2, 2))

    def test_a_machine_with_no_room_above_its_overhead_gets_no_pool(self):
        for mem_gib in (0, 4, 8, 11):
            with self.subTest(mem_gib=mem_gib):
                target, ceiling, fits = check.effective_pool(10, 12, mem_gib, overhead_gib=OVERHEAD, per_vm_gib=PER_VM)
                self.assertEqual((target, ceiling, fits), (0, 0, 0))

    def test_a_bigger_machine_than_the_tier_leaves_the_tier_alone(self):
        """The clamp is a ceiling, not a resize: a 256 GiB host keeps a full tier's pool."""
        self.assertEqual(check.effective_pool(10, 12, 256, overhead_gib=OVERHEAD, per_vm_gib=PER_VM), (10, 12, 62))

    def test_a_target_is_pinned_to_the_clamped_ceiling(self):
        """Half a clamp is not a clamp: a target above the ceiling can never be reached."""
        target, ceiling, _ = check.effective_pool(8, 12, 16, overhead_gib=OVERHEAD, per_vm_gib=PER_VM)
        self.assertEqual((target, ceiling), (2, 2))

    def test_a_target_alone_is_still_clamped(self):
        target, ceiling, _ = check.effective_pool(8, None, 20, overhead_gib=OVERHEAD, per_vm_gib=PER_VM)
        self.assertEqual((target, ceiling), (3, None))

    def test_a_tier_that_sets_no_pool_is_left_alone(self):
        self.assertEqual(check.effective_pool(None, None, 16, overhead_gib=OVERHEAD, per_vm_gib=PER_VM), (None, None, 2))


class RepoTiersTests(unittest.TestCase):
    def test_every_tier_is_reachable_on_the_machine_it_names(self):
        """The floor and the pool have to agree, or the first boot quietly shrinks it.

        This is the test that would fail if somebody raised a tier's pool to a class
        size without raising the machine it is built for — the numbers are the ones
        the repository ships, read back out of the tier files.
        """
        for path in sorted(TIERS.glob("*.env")):
            settings = check.read_settings(path)
            with self.subTest(tier=path.stem):
                target = check.as_int(settings, check.POOL_TARGET)
                ceiling = check.as_int(settings, check.POOL_MAX)
                if target is None and ceiling is None:
                    self.skipTest(f"{path.stem} sizes no pool")
                mem = check.as_int(settings, "TIER_MIN_MEM_GIB")
                self.assertIsNotNone(mem, f"{path.stem} names no RAM floor")
                effective = check.effective_pool(
                    target, ceiling, mem, overhead_gib=OVERHEAD, per_vm_gib=PER_VM
                )
                self.assertEqual(
                    (effective[0], effective[1]),
                    (target, ceiling),
                    f"{path.stem} prewarms {ceiling} machines but its own floor ({mem} GiB) "
                    "does not hold them",
                )


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="ontrak-tiercheck-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        # The settings file, which write_tier() fills in with the tier's own.
        self.settings = self.directory / "firstboot.env"

    def write_tier(self, **overrides: str) -> Path:
        """The two files a tiered image installs, as render-tier.py writes them.

        The tier's *record* describes the machine; its *settings* — the pool sizing,
        which is what this script decides about — are `firstboot.env`. They are two
        files on the host, and the check reads them as two.
        """
        values = {
            "TIER_NAME": "class",
            "TIER_LABEL": "class",
            "TIER_TITLE": "OnTrak class range — a class of 8-12",
            "TIER_STUDENTS": "8-12",
            "TIER_MIN_CPU": "16",
            "TIER_MIN_MEM_GIB": "64",
            "TIER_HOSTNAME": "ontrak-class",
            "TIER_NOTE": "point the pool at a spare device",
        }
        settings = {
            "ONTRAK_POOL__DEFAULT_TARGET": "4",
            "ONTRAK_POOL__MAX_TOTAL": "6",
        }
        for key, value in overrides.items():
            (settings if key.startswith("ONTRAK_") else values)[key] = value
        self.settings = self.directory / "firstboot.env"
        self.settings.write_text(
            "\n".join(f"{key}={value}" for key, value in settings.items()) + "\n", encoding="utf-8"
        )
        path = self.directory / "tier.env"
        path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
        return path

    def run_check(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(CHECK), "--firstboot-env", str(self.settings), *extra],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_a_machine_below_its_tier_is_told_so_and_clamped(self):
        tier = self.write_tier()
        effective = self.directory / "effective.env"
        result = self.run_check(
            "--tier-file", str(tier), "--cpu", "4", "--mem-gib", "16", "--write-env", str(effective)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below the class tier", result.stdout)
        self.assertIn("4 vCPU (wants 16)", result.stdout)
        self.assertIn("16 GiB (wants 64)", result.stdout)
        self.assertIn("pool clamped", result.stdout)
        self.assertIn("ONTRAK_POOL__MAX_TOTAL=2", effective.read_text(encoding="utf-8"))

    def test_the_written_sizing_is_shell_the_first_boot_can_source(self):
        tier = self.write_tier()
        effective = self.directory / "effective.env"
        self.run_check("--tier-file", str(tier), "--cpu", "4", "--mem-gib", "16", "--write-env", str(effective))
        sourced = subprocess.run(
            [
                "bash",
                "-c",
                f'set -a; . "{effective}"; printf "%s/%s" '
                '"$ONTRAK_POOL__DEFAULT_TARGET" "$ONTRAK_POOL__MAX_TOTAL"',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(sourced.stdout, "2/2", sourced.stderr)

    def test_a_machine_at_its_tiers_floor_is_not_touched(self):
        tier = self.write_tier()
        effective = self.directory / "effective.env"
        result = self.run_check(
            "--tier-file", str(tier), "--cpu", "16", "--mem-gib", "64", "--write-env", str(effective)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("below the class tier", result.stdout)
        self.assertNotIn("pool clamped", result.stdout)
        self.assertIn("ONTRAK_POOL__MAX_TOTAL=6", effective.read_text(encoding="utf-8"))

    def test_no_tier_on_the_image_is_not_a_failure(self):
        effective = self.directory / "effective.env"
        result = self.run_check("--tier-file", str(self.directory / "absent.env"), "--write-env", str(effective))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no sizing tier", result.stdout)
        self.assertFalse(effective.exists(), "a pool was written for an image with no tier")

    def test_a_tier_that_sizes_no_pool_writes_nothing(self):
        tier = self.write_tier()
        self.settings.write_text("", encoding="utf-8")
        effective = self.directory / "effective.env"
        result = self.run_check(
            "--tier-file", str(tier), "--cpu", "16", "--mem-gib", "64", "--write-env", str(effective)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("leaving the pool as configured", result.stdout)
        self.assertFalse(effective.exists())

    def test_the_report_names_the_tier_the_machine_and_its_note(self):
        tier = self.write_tier()
        result = self.run_check("--tier-file", str(tier), "--cpu", "32", "--mem-gib", "128")
        self.assertIn("tier      : class — OnTrak class range — a class of 8-12", result.stdout)
        self.assertIn("machine   : 32 vCPU / 128 GiB", result.stdout)
        self.assertIn("tier wants: 16 vCPU / 64 GiB, 8-12 students", result.stdout)
        self.assertIn("note      : point the pool at a spare device", result.stdout)

    def test_a_comment_line_is_not_a_setting(self):
        path = self.write_tier()
        path.write_text(
            "# TIER_MIN_MEM_GIB=999 is not a setting\n" + path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        settings = check.read_settings(path)
        self.assertEqual(settings["TIER_MIN_MEM_GIB"], "64")
        self.assertNotIn("#", "".join(settings))


if __name__ == "__main__":
    unittest.main()
