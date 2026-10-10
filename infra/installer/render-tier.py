#!/usr/bin/env python3
"""Render a sizing tier into the installer ISO's tree.

A **tier** is one image built for one class of machine: ``dev`` for the small host
or nested VM an OnTrak checkout is developed on, ``class`` for a class of 8-12,
``full`` for a cohort. The tiers live in ``tiers/*.env`` beside this file, one file
each, and a build that is given several of them emits one ISO per tier
(``ONTRAK_ISO_TIERS`` in ``infra/build-installer-iso.sh``).

What a tier writes into the ISO tree, all of it under ``/ontrak/``:

``tier.env``
    the ``TIER_*`` keys: what machine this image is for. ``tier-check.py`` reads it
    on the installed host, reports how that machine compares, and clamps the warm
    pool to the RAM it actually has — a pool target this host cannot hold is the
    documented way a range host starts swapping (docs/operations.md).

``firstboot.env``
    the ``ONTRAK_*`` keys of the tier file, with the comments above them: the
    settings the installed host would otherwise have to be told by hand. This is
    the *active* file, not the ``.example`` — choosing a tier is choosing these
    settings — and it is what ``/etc/ontrak/firstboot.env`` becomes, installed by
    the autoinstall's late-commands.

``README.txt``
    a paragraph naming the tier, because the file a person reads on the machine
    they installed from this image should say which image it was.

Tier files are plain ``KEY=value``: everything after the first ``=`` is the value,
for this reader and for the build's (``tier_value`` in
``infra/build-installer-iso.sh``, which reads only the label and the hostname
default). They are **not** shell, and must not be — ``TIER_TITLE=OnTrak class range``
is not a shell assignment, bash would run ``class`` — so values take no quotes, no
``$`` and no inline ``#``, and anything the two readers could see differently is
rejected here rather than discovered on a machine. This script is also the only
thing that validates a tier file at all.

    render-tier.py --tier dev --iso-tree /tmp/extract --tiers-dir infra/installer/tiers

Runs on the ISO build's stock python3: standard library only.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# The keys a tier file must define, in the order they are written back out.
REQUIRED = (
    "TIER_NAME",
    "TIER_LABEL",
    "TIER_TITLE",
    "TIER_STUDENTS",
    "TIER_MIN_CPU",
    "TIER_MIN_MEM_GIB",
    "TIER_HOSTNAME",
)
OPTIONAL = ("TIER_NOTE",)
INTEGER = ("TIER_MIN_CPU", "TIER_MIN_MEM_GIB")

# Keys a tier may not define: the pool ceiling below its own target is the one way
# a tier file can contradict itself, and the first boot would then clamp a number
# it never should have had.
POOL_TARGET = "ONTRAK_POOL__DEFAULT_TARGET"
POOL_MAX = "ONTRAK_POOL__MAX_TOTAL"

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Values are plain, and this is the check that makes that true for both readers.
BAD_VALUE_RE = re.compile(r"""["'$`#]""")

TIER_MARKER = "SIZING TIER"


class TierError(SystemExit):
    """A tier file that cannot be used, with what to do about it."""

    def __init__(self, message: str) -> None:
        super().__init__(f"tier: {message}")


def parse(path: pathlib.Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Read a tier file into its keys, and its comments keyed by the line below.

    Comments are kept because the file written to the installed host is a settings
    file somebody will read there: its reasons belong next to its values.
    """
    values: dict[str, str] = {}
    comments: dict[str, list[str]] = {}
    pending: list[str] = []

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            pending = []
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        if "=" not in line:
            raise TierError(f"{path}:{number}: not a KEY=value line: {line!r}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not KEY_RE.match(key):
            raise TierError(f"{path}:{number}: {key!r} is not a settings key")
        if not value:
            raise TierError(f"{path}:{number}: {key} is empty")
        if BAD_VALUE_RE.search(value):
            raise TierError(
                f"{path}:{number}: {key} is not a plain value. Tier files are read by "
                "the build as shell and by this script without one, so they take no "
                "quotes, no expansion and no inline # comment."
            )
        if not (key.startswith("TIER_") or key.startswith("ONTRAK_")):
            raise TierError(f"{path}:{number}: {key} is neither a TIER_* nor an ONTRAK_* key")
        if key in values:
            raise TierError(f"{path}:{number}: {key} is set twice")
        values[key] = value
        if pending:
            comments[key] = list(pending)
        pending = []

    return values, comments


def validate(name: str, path: pathlib.Path, values: dict[str, str]) -> None:
    if values.get("TIER_NAME") != name:
        raise TierError(
            f"{path}: TIER_NAME is {values.get('TIER_NAME')!r} but the file is {name}.env"
        )
    missing = [key for key in REQUIRED if key not in values]
    if missing:
        raise TierError(f"{path}: no {', '.join(missing)}")
    for key in INTEGER:
        try:
            if int(values[key]) < 1:
                raise ValueError
        except ValueError:
            raise TierError(f"{path}: {key} must be a whole number of at least 1") from None
    if POOL_TARGET in values and POOL_MAX in values:
        try:
            target, ceiling = int(values[POOL_TARGET]), int(values[POOL_MAX])
        except ValueError:
            raise TierError(f"{path}: the pool sizes must be whole numbers") from None
        if target > ceiling:
            raise TierError(
                f"{path}: {POOL_TARGET} is {target} and {POOL_MAX} is {ceiling}: a "
                "target above the ceiling can never be reached, so the tier would "
                "prewarm less than it says."
            )


def write_tier_env(values: dict[str, str], path: pathlib.Path) -> None:
    lines = [
        "# The sizing tier this image was built for — written by",
        "# infra/installer/render-tier.py at build time, and read by",
        "# ontrak-tier-check.py on this host at first boot.",
        "#",
        "# Do not edit: it describes the image. The settings it implies are in",
        "# /etc/ontrak/firstboot.env, which is the file to change.",
        "",
    ]
    for key in REQUIRED + OPTIONAL:
        if key in values:
            lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_firstboot_env(
    values: dict[str, str], comments: dict[str, list[str]], path: pathlib.Path, name: str
) -> bool:
    """Write the tier's ONTRAK_* settings. False when the tier has none."""
    settings = [key for key in values if key.startswith("ONTRAK_")]
    if not settings:
        return False

    lines = [
        f"# OnTrak first-boot settings — the ones the *{name}* sizing tier bakes in.",
        "#",
        "# Written into the installer image by infra/installer/render-tier.py, and",
        "# installed here by the installer's late-commands. Edit it here, or in",
        "# /etc/ontrak/firstboot.env.example, which documents every setting there is.",
        "",
    ]
    pending: list[str] = []
    for key in settings:
        seen = comments.get(key, [])
        # Deduplicate a comment block shared by two settings, so a run of them
        # above both keys is not printed twice.
        fresh = [line for line in seen if line not in pending]
        lines.extend(fresh)
        pending = list(seen)
        lines.append(f"{key}={values[key]}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return True


def annotate_readme(values: dict[str, str], path: pathlib.Path) -> bool:
    """Name the tier in README.txt, as a section under its title.

    It goes above the instructions rather than at the end because those instructions
    are the same for every image, and this is the one line that says which image the
    reader is holding. False when the README already names a tier.
    """
    text = path.read_text(encoding="utf-8")
    if TIER_MARKER in text:
        return False

    label = values["TIER_LABEL"]
    heading = f"{TIER_MARKER}: {label}"
    block = "\n".join(
        [
            heading,
            "-" * len(heading),
            f"This image is the {label} tier ({values['TIER_TITLE']}): it is built for a host",
            f"of at least {values['TIER_MIN_CPU']} vCPU and {values['TIER_MIN_MEM_GIB']} GiB of RAM, and sized for",
            f"{values['TIER_STUDENTS']} students. The settings that go with it are in",
            "/etc/ontrak/firstboot.env; the first boot compares this machine with the tier",
            "and clamps the warm pool to the RAM it finds — see docs/installer.md,",
            '"Sizing tiers". An untiered image is the same installer with none of this.',
            "",
            "",
        ]
    )
    # Under the title block, which is the first blank line in the file.
    title, separator, body = text.partition("\n\n")
    if not separator:
        # An unexpected README: keep the file, add the section rather than lose it.
        path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
        return True
    path.write_text(f"{title}\n\n{block}{body}", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", required=True)
    parser.add_argument("--tiers-dir", required=True, type=pathlib.Path)
    parser.add_argument("--iso-tree", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)

    name = args.tier
    path = args.tiers_dir / f"{name}.env"
    if not path.is_file():
        known = sorted(p.stem for p in args.tiers_dir.glob("*.env"))
        raise TierError(
            f"no {name}.env in {args.tiers_dir}"
            + (f" (there is: {', '.join(known)})" if known else " (the directory is empty)")
        )

    values, comments = parse(path)
    validate(name, path, values)

    payload = args.iso_tree / "ontrak"
    payload.mkdir(parents=True, exist_ok=True)
    write_tier_env(values, payload / "tier.env")
    print(f"wrote {payload / 'tier.env'} ({name})")

    if write_firstboot_env(values, comments, payload / "firstboot.env", name):
        print(f"wrote {payload / 'firstboot.env'} (the tier's settings, not the example)")
    else:
        print(f"tier {name} bakes no settings: no firstboot.env on this image")

    readme = payload / "README.txt"
    if readme.is_file():
        if annotate_readme(values, readme):
            print(f"named the tier in {readme}")
        else:
            print(f"{readme} already names its tier")
    else:
        print(f"no {readme} to annotate (the build copies it before this runs)")

    print(f"tier {name}: {values['TIER_TITLE']} ({values['TIER_STUDENTS']} students)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
