#!/usr/bin/env python3
"""Give the live-server boot menu one OnTrak entry per autoinstall profile.

An image built as a sizing tier also carries that tier's label in its titles
(`… (unattended) [dev]`), so two OnTrak sticks in front of the operator are told
apart by the menu they are looking at. See docs/installer.md, "Sizing tiers".

Ubuntu's own menu says **"Try or Install Ubuntu Server"**, which is both wrong for
this image (there is no live session on a server ISO — the only thing that boots is
the installer) and silent about the thing an operator most needs to know: that
booting it erases a disk. So every entry that loads ``/casper/vmlinuz`` is retitled,
pointed at an autoinstall, and *duplicated*:

``Install OnTrak on this machine's disk (unattended)``
    the ``machine`` profile: ``ds=nocloud;s=/cdrom/nocloud/``. Unattended, stops on
    identity, and takes the machine's own disk — the bare-metal install.

``Install OnTrak on the disk you choose (USB stick, or another disk)``
    the ``choose-disk`` profile: ``ds=nocloud;s=/cdrom/nocloud-choose-disk/``. The
    same install with the storage screen left up, so the target is the operator's
    choice. That is how a stick is installed, and the reason this entry also boots
    ``toram`` — the live filesystem is copied into RAM, so the medium being
    installed to is free to be erased. Without it, installing onto the stick you
    booted from would pull the ground out from under the running installer.

The original entry stays first in the menu, so grub's default is unchanged: an
unanswered boot still installs a machine unattended.

Applied to every grub.cfg the ISO boots from (BIOS and UEFI menus are separate
files), and idempotent — the second run over the same file adds nothing.

    patch-grub.py boot/grub/grub.cfg --machine-dir /nocloud \\
        --choose-dir /nocloud-choose-disk

Exits non-zero if an entry loads no ``/casper/*vmlinuz`` at all, which is how a base
image that is not the live-server one is caught.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

TITLE_MACHINE = "Install OnTrak on this machine's disk (unattended)"
TITLE_CHOOSE = "Install OnTrak on the disk you choose (USB stick, or another disk)"

# Every installer title this script writes begins with this, whichever profile and
# which tier it names — which is how a re-run recognises its own work.
OURS_PREFIX = "Install OnTrak on "

# `menuentry "title" --class ubuntu --class gnu-linux {` — the title is the first
# quoted string, and the options after it are Canonical's.
MENUENTRY = re.compile(r"""^(?P<indent>\s*)menuentry\s+(?P<title>"[^"]*"|'[^']*')""")
KERNEL = re.compile(r"^(?P<indent>\s*)(?P<command>linux(?:efi|16)?)(?P<gap>\s+)(?P<rest>\S.*|\S)$")
# Everything this script may have put on a kernel line, so that a file it has
# already patched comes out the same when it is patched again: the arguments are
# stripped and re-added rather than appended, which is what keeps a re-run — the
# normal recovery from a build that failed — from stacking them up.
# The unquoted form is listed too, so that an image written before the datasource
# was quoted (see `run_args`) is normalised rather than left with both.
# The trailing `\s*` and the absence of a `\b` after the datasource matter: `\b`
# matches *inside* `/cdrom/nocloud/`, so the trailing slash would be left behind.
RUN_ARGS = re.compile(
    r'\s*(?:autoinstall\s+"ds=nocloud;s=[^"]*"'
    r"|autoinstall\s+ds=nocloud;s=\S+"
    r"|\btoram\b|\bconsole=ttyS0)\s*"
)


def braces(line: str) -> int:
    """Net brace depth of a line, ignoring braces inside quoted strings."""
    depth = 0
    quote = ""
    for char in line:
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Line spans of every menuentry, brace-matched."""
    found = []
    index = 0
    while index < len(lines):
        if not MENUENTRY.match(lines[index]):
            index += 1
            continue
        start = index
        depth = 0
        while index < len(lines):
            depth += braces(lines[index])
            if depth <= 0 and index > start:
                break
            index += 1
        found.append((start, index))
        index += 1
    return found


def kernel_line(block: list[str]) -> int | None:
    """Index within the block of the ``linux …/casper/…vmlinuz`` line, if any."""
    for offset, line in enumerate(block):
        match = KERNEL.match(line)
        if match and "/casper/" in match.group("rest") and "vmlinuz" in match.group("rest"):
            return offset
    return None


def run_args(datasource: str, *, toram: bool) -> str:
    """The kernel arguments that select an autoinstall off the media.

    The datasource argument is **quoted, and that is not cosmetic**. Grub ends an
    argument at a `;`, so the unquoted `autoinstall ds=nocloud;s=/cdrom/nocloud/`
    reaches the kernel as `autoinstall ds=nocloud` — the seed directory is dropped,
    cloud-init finds no autoinstall, and everything appended after it (including
    `console=ttyS0`) is dropped with it. It fails silently: no error at the prompt,
    an installer that asks every question, and nothing at all on the serial console.
    Measured on the built image by typing both forms at grub's own command line:
    unquoted boots to a kernel with no output, quoted reports
    `Command line: … autoinstall ds=nocloud;s=/cdrom/nocloud/ console=ttyS0`.

    `console=ttyS0` as well as the VGA console, so a headless machine can be
    installed and watched over serial — and the smoke test has something to read.
    """
    parts = [f'autoinstall "ds=nocloud;s=/cdrom{datasource}/"']
    if toram:
        parts.append("toram")
    parts.append("console=ttyS0")
    return " ".join(parts)


def titles(tier: str = "") -> tuple[str, str]:
    """The two titles, marked with the sizing tier's label when the image has one.

    The label is on the *menu* and not only in the file name, because the operator
    choosing a stick is reading this menu: `[dev]` says which of two OnTrak sticks is
    the one sized for the machine in front of them. Everything else about the entries
    is identical, so a tier never changes what an install does — only what it was
    built for (see docs/installer.md, "Sizing tiers").
    """
    if not tier:
        return TITLE_MACHINE, TITLE_CHOOSE
    return f"{TITLE_MACHINE} [{tier}]", f"{TITLE_CHOOSE} [{tier}]"


def retitle_line(line: str, ours: str) -> str:
    """A menuentry line with our title in place of Canonical's."""
    match = MENUENTRY.match(line)
    assert match is not None  # every block starts with a line this matched
    return f"{match.group('indent')}menuentry {retitle(match.group('title'), ours)}{line[match.end():]}"


def retitle(original_literal: str, ours: str) -> str:
    """Our title for an entry, keeping what tells two entries apart.

    ``ours`` is the whole title — the sizing tier's label included, when the image
    has one (see ``titles``). Two things survive from the title being replaced: the kernel
    variant, because the standard and HWE entries are otherwise indistinguishable in
    the menu, and — for a menu this script has never seen — Canonical's own name, as
    a subtitle.

    A title this script wrote is replaced *whatever it said*, so re-running over a
    patched file, or building a tier into a tree that already carried a different
    one, leaves one title rather than a growing list of them.
    """
    original = original_literal.strip("\"'")
    marks = " [HWE kernel]" if "hwe" in original.lower() else ""

    if original.startswith(OURS_PREFIX):
        return f'"{ours}{marks}"'  # ours already: a re-run, or another tier
    if "ubuntu server" in original.lower() or "try or install" in original.lower():
        return f'"{ours}{marks}"'
    # Some other menu Canonical ships: name it rather than losing the distinction.
    return f'"{ours}{marks} [{original}]"'


def patch(text: str, machine_dir: str, choose_dir: str, tier: str = "") -> tuple[str, int, int]:
    """Return the patched grub.cfg, how many installer entries it has, and how many
    were added.

    ``tier`` is the sizing tier's label, if the image has one: it is added to the
    titles so the menu says which machine each image was built for.

    A file that already names the choose-disk datasource was written by a previous
    run — a failed build is re-run against a tree that may still be patched. That is
    a property of the *file* and not of an entry, because the first run emitted one
    entry per destination: pairing again would double the menu on every run.
    """
    lines = text.splitlines(keepends=True)
    machine_title, choose_title = titles(tier)
    paired = f"ds=nocloud;s=/cdrom{choose_dir}/" in text
    out: list[str] = []
    cursor = 0
    entries = 0
    added = 0

    for start, end in blocks(lines):
        block = lines[start : end + 1]
        offset = kernel_line(block)
        if offset is None:
            continue  # "Boot from the next volume", "UEFI Firmware Settings", …
        entries += 1

        line = block[offset]
        match = KERNEL.match(line)
        assert match is not None  # kernel_line() matched the same expression
        # Everything before the arguments, with any earlier run of ours removed, so
        # that a re-run re-adds them rather than stacking them up.
        head = f"{match.group('indent')}{match.group('command')}{match.group('gap')}"
        rest = RUN_ARGS.sub("", match.group("rest")).rstrip()
        newline = "\n" if line.endswith("\n") else ""
        # Retitling reads the entry as Canonical wrote it, so a second title is never
        # derived from one this script just replaced.
        original = list(block)

        # `toram` on the choose-disk entry frees the medium: installing onto the stick
        # the machine booted from is the point of it. Not on the unattended install,
        # which targets a different disk and has no reason to copy the ISO into RAM.
        if paired:
            targets = (
                [(choose_dir, True, choose_title)]
                if f"ds=nocloud;s=/cdrom{choose_dir}/" in line
                else [(machine_dir, False, machine_title)]
            )
        else:
            targets = [(machine_dir, False, machine_title), (choose_dir, True, choose_title)]

        out.extend(lines[cursor:start])
        for datasource, toram, title in targets:
            destination = list(original)
            destination[offset] = f"{head}{rest} {run_args(datasource, toram=toram)}{newline}"
            destination[0] = retitle_line(destination[0], title)
            out.extend(destination)
            added += 1
        cursor = end + 1

    if not entries:
        return text, 0, 0
    out.extend(lines[cursor:])
    return "".join(out), entries, added - entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    parser.add_argument("--machine-dir", default="/nocloud")
    parser.add_argument("--choose-dir", default="/nocloud-choose-disk")
    parser.add_argument(
        "--tier",
        default="",
        help="sizing tier label for the menu titles, e.g. dev (default: none)",
    )
    args = parser.parse_args(argv)

    for path in args.files:
        text = path.read_text(encoding="utf-8")
        patched, entries, added = patch(text, args.machine_dir, args.choose_dir, args.tier)
        if not entries:
            raise SystemExit(f"{path}: no /casper/vmlinuz boot entry to patch")
        path.write_text(patched, encoding="utf-8")
        suffix = f", {added} added for the choose-disk install" if added else " (already patched)"
        print(f"{path}: {entries} installer entr{'y' if entries == 1 else 'ies'}{suffix}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
