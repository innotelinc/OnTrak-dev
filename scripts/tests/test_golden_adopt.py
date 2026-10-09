#!/usr/bin/env python3
"""Adopting a golden image: the requirement this range cannot satisfy.

``requirements.cdrom_agent=true`` means "every instance made from this image must have
an ``agent:config`` disk". incus-windows' ``tools/pack.sh`` stamps it on everything it
publishes, and OnTrak drives Windows over WinRM, which needs no agent config. Incus
enforces the requirement at *start*, so a clone of an image that still carries it
refuses to come up:

    Error: This virtual machine image requires an agent:config disk be added

which is every Windows template and every Windows session -- a build that reports
success and a range that cannot run anything.

Both scripts that adopt an image clear it. What these tests pin is *which* image they
clear it on, because clearing the imported one is not enough. Measured on a host that
had just built the image (Incus 7.5.1): take an image whose property has been cleared,
make a VM from it, and publish that VM with ``incus publish`` -- the image that comes
out carries ``requirements.cdrom_agent: true`` again. The build publishes exactly such
a VM as ``$IMAGE_ALIAS``, and that alias is the one the range clones from, so the
clear has to be applied a second time, after the publish.

The scripts are not executed here: that is incus, a hypervisor, a 5 GiB Windows
evaluation ISO and two hours. These are assertions about their shape, which is the
part a reviewer breaks by accident.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
BUILD = ROOT / "infra" / "build-golden-image.sh"
IMPORT = ROOT / "infra" / "import-golden-image.sh"

CLEAR = 'image set-property "%s" requirements.cdrom_agent=""'
PUBLISH = 'publish "$BUILD_VM" --alias "$IMAGE_ALIAS"'


class GoldenAdoptTests(unittest.TestCase):
    def setUp(self):
        if not BUILD.is_file():  # pragma: no cover - a missing file is a failure below
            self.fail(f"the build script is missing: {BUILD}")
        self.build = BUILD.read_text(encoding="utf-8")

    def test_the_build_clears_it_on_the_image_it_imports(self):
        """Without this the build dies one step after the import.

        The very next thing it does is create a VM from that image to apply
        ``post-install.ps1``, and that is where the requirement stops it.
        """
        self.assertIn(
            CLEAR % "$IMPORT_ALIAS",
            self.build,
            "the build no longer clears requirements.cdrom_agent on the image it imports, "
            "so the VM it makes to apply post-install.ps1 cannot start",
        )

    def test_the_build_clears_it_on_the_image_it_publishes(self):
        """This is the alias the range clones from; `incus publish` re-stamps it."""
        self.assertIn(
            CLEAR % "$IMAGE_ALIAS",
            self.build,
            "the build no longer clears requirements.cdrom_agent on the image it publishes "
            "as $IMAGE_ALIAS. incus publish puts it back, so every template and every "
            "session built from that alias fails to start with 'This virtual machine "
            "image requires an agent:config disk be added'",
        )

    def test_the_clear_on_the_published_alias_comes_after_the_publish(self):
        """Order is the whole point: before the publish there is nothing to clear.

        Clear it before, and the property arrives with the publish untroubled -- a
        green build and an image nothing can start.
        """
        self.assertIn(PUBLISH, self.build, "the build no longer publishes with --alias")
        self.assertLess(
            self.build.index(PUBLISH),
            self.build.index(CLEAR % "$IMAGE_ALIAS"),
            "requirements.cdrom_agent is cleared on $IMAGE_ALIAS before it is published; "
            "the publish puts it back and the clear is wasted",
        )

    def test_the_published_alias_is_the_one_the_range_uses(self):
        """The clear is useless if it names a different image than the range clones.

        Templates and sessions resolve `incus.image_alias`, and the build publishes
        under `ONTRAK_IMAGE_ALIAS`, whose default is that same alias.
        """
        self.assertIn('IMAGE_ALIAS="${ONTRAK_IMAGE_ALIAS:-ontrak-win-base}"', self.build)


class ImportAdoptTests(unittest.TestCase):
    """The image an operator brings in by hand needs the same treatment."""

    def setUp(self):
        if not IMPORT.is_file():  # pragma: no cover
            self.fail(f"the import script is missing: {IMPORT}")
        self.import_ = IMPORT.read_text(encoding="utf-8")

    def test_the_import_clears_it_on_the_alias_it_publishes(self):
        self.assertIn(
            CLEAR % "$ALIAS",
            self.import_,
            "infra/import-golden-image.sh no longer clears requirements.cdrom_agent on the "
            "image it imports, so nothing on this host can start a clone of it",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
