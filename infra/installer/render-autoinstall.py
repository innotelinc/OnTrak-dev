#!/usr/bin/env python3
"""Render the OnTrak autoinstalls into the installer ISO's tree.

The installer ISO carries two autoinstalls, and both come out of the *one*
template here (``autoinstall/user-data.dist``), so the two cannot drift apart:

``machine``
    the machine's own disk (LVM on the largest), the identity screen the only
    interactive one. This is what a bare-metal install is.

``choose-disk``
    the same install, with the installer's storage screen left interactive too,
    so the operator picks the target. That is how a range gets installed onto a
    **USB stick** rather than an internal disk — see docs/installer.md.

The only difference between them is which screens stay up, which is why it is a
profile rather than a second template: everything else about the install has to
stay identical, and a copy of the file is how that stops being true.

It writes the tree the nocloud datasource expects (``<iso-tree>/<datasource>/``,
which is where the boot entries point ``ds=nocloud;s=/cdrom/…/``) and prints the
datasource path for each profile, so the build patches the boot entries with the
paths this actually wrote rather than with a second copy of them.

    render-autoinstall.py --profile machine --iso-tree /tmp/extract \\
        --template autoinstall/user-data.dist --meta-data autoinstall/meta-data \\
        --username ontrak --hostname ontrak-range --password-hash '$6$…' --release 24.04.5

Runs on the ISO build's stock python3: standard library only.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys

# Where in the ISO tree each profile's autoinstall goes. The boot entries are
# pointed at these paths (`ds=nocloud;s=/cdrom/<dir>/`), and the build verifies
# the pair on the finished image: a boot entry naming a directory nothing wrote
# is the one way this can go wrong quietly.
DATASOURCES = {
    "machine": "/nocloud",
    "choose-disk": "/nocloud-choose-disk",
}

# The one thing that differs. `identity` stays up in both: no credential is baked
# into an image that gets copied around, and identity carries defaults so a boot
# nobody answers still ends in a host somebody can sign in to. `storage` is the
# extra one, and it is what makes an install onto a USB stick possible — the
# installer cannot know which disk is the stick, the operator can.
PROFILES = {
    "machine": {"INTERACTIVE_SECTIONS": "[identity]"},
    "choose-disk": {"INTERACTIVE_SECTIONS": "[identity, storage]"},
}

# Every token the template may use. One that is left in the rendered file would be
# ignored by the installer in favour of a default nobody chose, so it is fatal.
TOKENS = (
    "USERNAME",
    "HOSTNAME",
    "PASSWORD_HASH",
    "RELEASE",
    "INTERACTIVE_SECTIONS",
)


def render(template: str, profile: str, values: dict[str, str]) -> str:
    merged = dict(PROFILES[profile])
    # The build supplies the identity and release; the profile supplies the rest.
    # Neither may be missing, and neither may shadow the other's token.
    for key, value in values.items():
        if key in merged and merged[key] != value:
            raise SystemExit(f"profile {profile} and the build disagree about @{key}@")
        merged[key] = value

    missing = [t for t in TOKENS if t not in merged]
    if missing:
        raise SystemExit(f"no value for {', '.join('@' + t + '@' for t in missing)}")

    text = template
    for token in TOKENS:
        text = text.replace(f"@{token}@", merged[token])

    # Anything still shaped like a token is one this script does not know — a typo
    # in the template, or a rename that missed one side. The installer ignores what
    # it does not recognise and falls back to a default nobody chose, so a leftover
    # is fatal here rather than visible only in the install it produced.
    left = sorted(set(re.findall(r"@[A-Z][A-Z_]*@", text)))
    if left:
        raise SystemExit(f"unsubstituted token(s) in the rendered autoinstall: {', '.join(left)}")
    return text


def render_profile(
    profile: str,
    iso_tree: pathlib.Path,
    template: pathlib.Path,
    meta_data: pathlib.Path,
    values: dict[str, str],
) -> str:
    """Write one profile's ``user-data``/``meta-data`` pair; return its datasource path."""
    datasource = DATASOURCES[profile]
    directory = iso_tree / datasource.lstrip("/")
    directory.mkdir(parents=True, exist_ok=True)

    user_data = directory / "user-data"
    user_data.write_text(
        render(template.read_text(encoding="utf-8"), profile, values), encoding="utf-8"
    )
    # subiquity reads the autoinstall out of user-data, but the nocloud datasource
    # requires a meta-data beside it.
    shutil.copyfile(meta_data, directory / "meta-data")
    return datasource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    parser.add_argument("--iso-tree", required=True, type=pathlib.Path)
    parser.add_argument("--template", required=True, type=pathlib.Path)
    parser.add_argument("--meta-data", required=True, type=pathlib.Path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--password-hash", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)

    path = render_profile(
        args.profile,
        args.iso_tree,
        args.template,
        args.meta_data,
        {
            "USERNAME": args.username,
            "HOSTNAME": args.hostname,
            "PASSWORD_HASH": args.password_hash,
            "RELEASE": args.release,
        },
    )
    # Read off the build's own log line rather than assumed: the value is what the
    # boot entries are patched with.
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
