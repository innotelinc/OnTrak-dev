#!/usr/bin/env python3
"""Compare this machine with the sizing tier the image was built for.

The installer ISO can be built as a **tier** — ``dev``, ``class``, ``full`` — which
is one image built for one class of machine. The tier file says which machine it is
for and bakes the settings that go with it, including the warm pool's target and
ceiling. This is the other half of that, and it runs on the installed host at first
boot (from ``ontrak-firstboot.sh``):

* it says what the image was built for, and what this machine actually is;
* it says so, loudly, when the image is on a **smaller** machine than its tier;
* and it writes the pool sizing that fits the RAM it found, because prewarming to a
  target this host cannot hold is the documented way a range host starts swapping
  (docs/operations.md, "RAM is the binding constraint").

That last part is arithmetic on this page rather than a policy: RAM ≈ overhead +
``max(live, pool)`` × 4 GiB per VM. The pool is what the tier sizes, so the ceiling
the tier names is clamped to ``(RAM − overhead) ÷ 4 GiB``. On a machine that meets
its tier this changes nothing — the clamp never binds — and on one that does not, it
is the difference between a slow first class and a host that swaps.

It writes nothing unless the host still has a pool target to clamp, and it exits 0
even when it complains: a machine below its tier is a range host with a smaller pool,
not a failed build. Everything it prints goes to the first boot's log.

    tier-check.py --tier-file /etc/ontrak/tier.env \\
                  --firstboot-env /etc/ontrak/firstboot.env \\
                  --write-env /var/lib/ontrak/tier-effective.env

The machine's own CPU count and RAM, and the arithmetic's constants, can all be
given on the command line, which is how the tests hold the clamp down without a
machine of each size.

Runs on the installed host's stock python3: standard library only.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

DEFAULT_TIER_FILE = "/etc/ontrak/tier.env"
DEFAULT_FIRSTBOOT_ENV = "/etc/ontrak/firstboot.env"

# 4 GiB per student VM: the profile's limits.memory, infra/incus/profile.yaml.
PER_VM_GIB = 4
# 8-16 GiB of host overhead (docs/operations.md). The low end is the one that page
# is written against: its own 15 GiB host keeps one warm machine, and 8 GiB + 1 × 4
# GiB is what makes that come out right.
OVERHEAD_GIB = 8

POOL_TARGET = "ONTRAK_POOL__DEFAULT_TARGET"
POOL_MAX = "ONTRAK_POOL__MAX_TOTAL"

SETTING_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def read_settings(path: pathlib.Path) -> dict[str, str]:
    """The KEY=value lines of a settings file, comments and blanks skipped."""
    if not path.is_file():
        return {}
    settings: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = SETTING_RE.match(line)
        if match:
            settings[match.group(1)] = match.group(2)
    return settings


def as_int(settings: dict[str, str], key: str) -> int | None:
    value = settings.get(key)
    if value is None or not value.strip().lstrip("-").isdigit():
        return None
    return int(value)


def machine_cpu() -> int:
    return os.cpu_count() or 1


def machine_mem_gib() -> int:
    """MemTotal from /proc/meminfo, which is what the machine really has."""
    try:
        for line in pathlib.Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kib = int(line.split()[1])
                return kib // 1024 // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 1024**3)
    except (ValueError, OSError):
        return 0


def effective_pool(
    target: int | None,
    ceiling: int | None,
    mem_gib: int,
    *,
    overhead_gib: int,
    per_vm_gib: int,
) -> tuple[int | None, int | None, int]:
    """The pool this machine can hold, and how many VMs that is."""
    room = mem_gib - overhead_gib
    fits = max(0, room // per_vm_gib) if per_vm_gib > 0 else 0
    if target is None and ceiling is None:
        return None, None, fits
    if ceiling is not None:
        ceiling = min(ceiling, fits)
    if target is not None:
        target = min(target, ceiling if ceiling is not None else fits)
    return target, ceiling, fits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier-file", type=pathlib.Path, default=pathlib.Path(DEFAULT_TIER_FILE))
    parser.add_argument(
        "--firstboot-env", type=pathlib.Path, default=pathlib.Path(DEFAULT_FIRSTBOOT_ENV)
    )
    parser.add_argument(
        "--write-env",
        type=pathlib.Path,
        help="write the effective pool sizing here, for the caller to source",
    )
    parser.add_argument("--cpu", type=int, help="override the machine's CPU count")
    parser.add_argument("--mem-gib", type=int, help="override the machine's RAM in GiB")
    parser.add_argument("--overhead-gib", type=int, default=OVERHEAD_GIB)
    parser.add_argument("--per-vm-gib", type=int, default=PER_VM_GIB)
    args = parser.parse_args(argv)

    tier = read_settings(args.tier_file)
    if not tier:
        print(f"no sizing tier: {args.tier_file} is absent or empty")
        return 0

    settings = read_settings(args.firstboot_env)
    cpu = args.cpu if args.cpu is not None else machine_cpu()
    mem_gib = args.mem_gib if args.mem_gib is not None else machine_mem_gib()

    label = tier.get("TIER_LABEL") or tier.get("TIER_NAME") or "?"
    wants_cpu = as_int(tier, "TIER_MIN_CPU")
    wants_mem = as_int(tier, "TIER_MIN_MEM_GIB")
    target = as_int(settings, POOL_TARGET)
    ceiling = as_int(settings, POOL_MAX)

    print(f"tier      : {label} — {tier.get('TIER_TITLE', '(untitled)')}")
    print(f"machine   : {cpu} vCPU / {mem_gib} GiB")
    wants = [f"{wants_cpu} vCPU" if wants_cpu else "", f"{wants_mem} GiB" if wants_mem else ""]
    print(
        "tier wants: "
        + " / ".join(part for part in wants if part)
        + (f", {tier['TIER_STUDENTS']} students" if tier.get("TIER_STUDENTS") else "")
    )

    shortfalls = []
    if wants_cpu and cpu < wants_cpu:
        shortfalls.append(f"{cpu} vCPU (wants {wants_cpu})")
    if wants_mem and mem_gib < wants_mem:
        shortfalls.append(f"{mem_gib} GiB (wants {wants_mem})")
    if shortfalls:
        print(
            f"[!] this machine is below the {label} tier: "
            + ", ".join(shortfalls)
            + " — the range will come up, but size the class to what this host has "
            "(docs/operations.md, the capacity table)"
        )
    if tier.get("TIER_NOTE"):
        print(f"note      : {tier['TIER_NOTE']}")

    new_target, new_ceiling, fits = effective_pool(
        target,
        ceiling,
        mem_gib,
        overhead_gib=args.overhead_gib,
        per_vm_gib=args.per_vm_gib,
    )

    if target is None and ceiling is None:
        print("pool      : the tier sets none; leaving the pool as configured")
        return 0

    if fits == 0:
        print(
            f"[!] no room to prewarm: {mem_gib} GiB is the {args.overhead_gib} GiB of host "
            f"overhead and nothing over it at {args.per_vm_gib} GiB a machine — the pool "
            "is pinned to 0, and machines are cloned on demand instead"
        )
    if (new_target, new_ceiling) != (target, ceiling):
        print(
            f"[!] pool clamped to fit this machine: target {target} → {new_target}, "
            f"ceiling {ceiling} → {new_ceiling} "
            f"({mem_gib} GiB holds {args.overhead_gib} GiB of overhead + "
            f"{fits} × {args.per_vm_gib} GiB)"
        )
    print(f"pool      : target {new_target}, ceiling {new_ceiling}")

    if args.write_env:
        args.write_env.parent.mkdir(parents=True, exist_ok=True)
        # Only what changed is written, and only the two keys this script decides.
        args.write_env.write_text(
            "\n".join(
                [
                    "# Written by ontrak-tier-check.py at first boot: the pool sizing",
                    "# this machine's RAM holds. Values not listed keep /etc/ontrak/",
                    "# firstboot.env's own.",
                    f"{POOL_TARGET}={new_target}",
                    f"{POOL_MAX}={new_ceiling}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"wrote     : {args.write_env}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
