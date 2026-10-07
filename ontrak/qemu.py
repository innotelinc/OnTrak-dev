"""Which QEMU accelerator a Windows guest gets, and what to do when KVM cannot
give it one.

Every Windows guest in this range — the golden image incus-windows builds, the
scenario templates cloned from it, the machines students sit in front of — is a
QEMU virtual machine. Nothing in this repository writes a QEMU command line:
``incusd`` builds it from the instance's config, and the only handle from outside
is the two raw keys it appends.

    raw.qemu.conf   extra sections for the generated config file
    raw.qemu        extra arguments, appended to the command line

Both were checked against incusd rather than assumed. For a VM, incusd writes
``/run/incus/<instance>/qemu.conf`` and runs

    qemu-system-x86_64 ... -cpu host,hv_passthrough ... -readconfig /run/incus/<instance>/qemu.conf

so the accelerator lives in the config file (``[machine] accel = "kvm"``) while
the CPU model is on the command line. ``raw.qemu.conf`` is merged into that file
and does override ``[machine] accel``; ``raw.qemu`` is appended last, and QEMU
takes the last ``-cpu``, so it overrides the hard-coded one.

That matters because KVM is not always available to a Windows guest. Windows 11
needs Secure Boot, in OVMF Secure Boot means SMM, and a host whose own
virtualisation is *nested on AMD* cannot virtualise SMM at all. Measured on WSL2
(Hyper-V) on a Ryzen: the instance reports ``RUNNING`` and then goes ``ERROR`` ten
to twenty seconds later, and its qemu log ends

    KVM: entry failed, hardware error 0xffffffff
    ... EIP=00008000 ... SMM=1 HLT=0

Turning Secure Boot and the TPM off does not help — OVMF uses SMM for its runtime
services either way — ``-machine smm=off`` only trades the crash for a guest that
spins without writing a sector, and a legacy-BIOS (CSM/SeaBIOS) build hangs the
same way. The host is not broken; there is one thing it cannot do.

QEMU can still do it, in software. This module decides whether KVM will serve and,
when it will not, hands the guest the two settings that move it to TCG:

    [machine]
    accel = "tcg"

and ``-cpu max``, because incusd hard-codes ``-cpu host,hv_passthrough`` for a VM
and QEMU refuses that model without KVM:

    qemu-system-x86_64: CPU model 'host' requires KVM or HVF

``-accel tcg,thread=multi`` is what a hand-written command line would say, and it
is what this range asks for where it writes one. It cannot be asked for *here*:
QEMU refuses ``-accel`` beside the ``-machine accel=`` incusd already emitted
("The -accel and \\"-machine accel=\\" options are incompatible"), and writing
``accel = "tcg,thread=multi"`` into the section is rejected in turn ("invalid
accelerator tcg,thread=multi") because ``accel`` takes a name, not options. TCG's
multi-threaded mode is the default for x86_64 guests anyway.

Nothing else about the guest is touched. Q35, UEFI via OVMF, SMM, the writable
per-instance VARS store, the Secure-Boot-capable firmware and TPM 2.0 on swtpm
all stay exactly as Incus configured them: the point is to run the same machine
more slowly, not a lesser one. In particular this must never be a reason to drop
``security.secureboot`` or the ``tpm`` device — a guest that boots without them is
not the guest this range teaches on.
"""

from __future__ import annotations

import fcntl
import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

KVM = "kvm"
TCG = "tcg"

# Where incus-windows' build VM lives: tools/pack.sh runs `incus init`/`incus
# start` with no --project at all. See main().
DEFAULT_PROJECT = "default"

# What incusd puts in the generated config, and what it appends to the command
# line. Both are only correct under KVM; the replacements are theirs.
KVM_CPU_MODEL = "-cpu host,hv_passthrough"
TCG_CPU_MODEL = "-cpu max"

TCG_MACHINE_SECTION = '[machine]\naccel = "tcg"\n'

# `ioctl(/dev/kvm, KVM_GET_API_VERSION)` — asking the device to answer, rather than
# trusting that the node exists. Same check infra/installer/install-test.sh makes.
_KVM_GET_API_VERSION = 0xAE00

# Where OVMF's CODE and VARS images live. Incus ships its own under /opt/incus;
# the ovmf package keeps the distribution's under /usr/share. Which one a guest
# boots depends on how Incus was packaged, so both are searched and no file name
# is assumed.
OVMF_DIRS = (
    "/opt/incus/share/qemu",
    "/usr/share/OVMF",
    "/usr/share/edk2/ovmf",
    "/usr/share/qemu",
)

# What the software path needs. Incus uses OVMF for a VM's firmware and swtpm for
# its TPM, but neither is a hard dependency of every Incus package, so a host can
# have Incus and still be unable to start the guest this range builds.
REQUIRED_PACKAGES = ("qemu-system-x86", "qemu-utils", "ovmf", "swtpm", "swtpm-tools")


@dataclass(frozen=True)
class Host:
    """The three facts the decision turns on."""

    kvm: bool
    virt: str
    vendor: str


def _kvm_usable(device: str = "/dev/kvm") -> bool:
    """Whether /dev/kvm is *usable*, not merely present.

    A /dev/kvm node that exists but cannot be opened is what a container or an
    unprivileged user sees, and treating that as KVM is how a build gets three
    hours in before noticing. The device is asked to report its API version.
    """
    try:
        fd = os.open(device, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.ioctl(fd, _KVM_GET_API_VERSION, 0)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _detect_virt() -> str:
    """What systemd says this machine is running *inside*, if anything.

    "none" on bare metal. Ubuntu's systemd-detect-virt reports "wsl" under WSL2,
    which is the case this fallback exists for.
    """
    exe = shutil.which("systemd-detect-virt")
    if not exe:
        return "unknown"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [exe], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (proc.stdout or "").strip() or "none"


def _cpu_vendor(cpuinfo: str = "/proc/cpuinfo") -> str:
    """The host's CPU vendor, as /proc/cpuinfo spells it ("AuthenticAMD")."""
    try:
        with open(cpuinfo, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("vendor_id"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


@functools.lru_cache(maxsize=1)
def host() -> Host:
    """Read this host's facts. The only part of this module that touches it.

    Cached for the life of the process: a host does not stop being nested while a
    range serves a class, and asking means running systemd-detect-virt and reading
    /proc/cpuinfo, which the session path would otherwise do once per guest. Tests
    clear it with ``host.cache_clear()``.
    """
    return Host(kvm=_kvm_usable(), virt=_detect_virt(), vendor=_cpu_vendor())


def choose(host_facts: Host, override: str = "") -> str:
    """Which accelerator to ask for. Pure, so a test can hand it any host.

    The nested-AMD rule is a measurement, not a preference: it is the one
    condition under which KVM is known to fail here rather than merely be slow.
    Nothing about a nested Intel host is asserted, so it keeps KVM until someone
    measures otherwise, and ``ONTRAK_QEMU_ACCEL`` settles any host this gets wrong
    in either direction.
    """
    explicit = (override or "").strip().lower()
    if explicit in {KVM, TCG}:
        return explicit
    if not host_facts.kvm:
        return TCG
    if host_facts.virt not in {"", "none", "unknown"} and host_facts.vendor == "AuthenticAMD":
        return TCG
    return KVM


def accel(override: str = "") -> str:
    """The accelerator for this host, honouring ``ONTRAK_QEMU_ACCEL=kvm|tcg``."""
    return choose(host(), override or os.environ.get("ONTRAK_QEMU_ACCEL", ""))


def reason(host_facts: Host, accel_name: str) -> str:
    """One line naming the accelerator and why, for a log."""
    if accel_name == KVM:
        return "accelerator: KVM (hardware virtualisation)"
    if not host_facts.kvm:
        return (
            "accelerator: QEMU TCG (no usable /dev/kvm on this host, so the "
            "software emulator is the only one available)"
        )
    return (
        f"KVM detected but KVM+SMM unavailable under {host_facts.virt}; "
        "using QEMU TCG fallback"
    )


def tcg_instance_config(current_raw_qemu: str = "", current_conf: str = "") -> dict[str, str]:
    """The instance config that moves a guest to software emulation.

    Both settings only ever *add*. An operator who has already set either key is
    keeping something deliberate — a legacy workload's ``-M pc -cpu pentium2`` is
    set this way — so an existing ``accel`` is left alone and the CPU model is
    appended rather than replaced. Passing no current values is therefore the
    "start from nothing" case, and passing back what a previous call set is the
    no-op case, which is what makes re-running a build against the same checkout
    safe.
    """
    values: dict[str, str] = {}
    if "accel" not in (current_conf or ""):
        values["raw.qemu.conf"] = TCG_MACHINE_SECTION
    args = (current_raw_qemu or "").strip()
    if TCG_CPU_MODEL not in args:
        values["raw.qemu"] = f"{args} {TCG_CPU_MODEL}".strip()
    return values


def _config_get(client: Any, instance: str, key: str) -> str:
    """Read one instance config key, tolerating a client that cannot be asked."""
    getter = getattr(client, "config_get", None)
    if getter is None:
        return ""
    return getter(instance, key) or ""


def apply(client: Any, instance: str, accel_name: str = "") -> str:
    """Point a guest at this host's accelerator. Returns what was chosen.

    A no-op under KVM, on purpose: the config incusd writes is already right, and
    writing the TCG values there would be read by the next person as a leftover.
    """
    name = accel_name or accel()
    if name != TCG:
        return name
    values = tcg_instance_config(
        _config_get(client, instance, "raw.qemu"),
        _config_get(client, instance, "raw.qemu.conf"),
    )
    if values:
        client.set_configs(instance, values)
    return name


def ovmf() -> dict[str, str]:
    """Where this host keeps OVMF's CODE and VARS images, if anywhere.

    Detected rather than named. This is for the preflight and for telling an
    operator what is installed — the firmware a guest actually boots is incusd's
    choice and shows up in ``/run/incus/<instance>/qemu.conf``.
    """
    found: dict[str, str] = {}
    for directory in OVMF_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in sorted(names):
            lower = name.lower()
            if not (lower.startswith("ovmf_") and lower.endswith(".fd")):
                continue
            if "_code" in lower:
                found.setdefault("code", os.path.join(directory, name))
            elif "_vars" in lower:
                found.setdefault("vars", os.path.join(directory, name))
    return found


def missing_packages(packages: tuple[str, ...] = REQUIRED_PACKAGES) -> list[str]:
    """Which packages the software path needs are not installed.

    Asked of dpkg rather than by looking for a binary: ``ovmf`` and ``swtpm-tools``
    install files rather than commands, and the package is ``qemu-system-x86`` while
    the binary is ``qemu-system-x86_64``.
    """
    exe = shutil.which("dpkg-query")
    if not exe:
        return []
    missing: list[str] = []
    for package in packages:
        try:
            proc = subprocess.run(  # noqa: S603 - package names are a module constant
                [exe, "-W", "-f=${Status}", package],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if "install ok installed" not in (proc.stdout or ""):
            missing.append(package)
    return missing


def report() -> dict[str, Any]:
    """Everything an operator or a preflight wants to know about this host."""
    host_facts = host()
    name = choose(host_facts, os.environ.get("ONTRAK_QEMU_ACCEL", ""))
    return {
        "accel": name,
        "reason": reason(host_facts, name),
        "kvm": host_facts.kvm,
        "virt": host_facts.virt,
        "vendor": host_facts.vendor,
        "ovmf": ovmf(),
        "missing_packages": missing_packages(),
    }


def _print_report() -> int:
    import json

    data = report()
    print(json.dumps(data, indent=2))
    missing = data["missing_packages"]
    firmware = data["ovmf"]
    if data["accel"] != TCG:
        return 0
    problems = []
    if missing:
        problems.append("missing packages: " + " ".join(missing))
    if "code" not in firmware or "vars" not in firmware:
        problems.append("no OVMF CODE/VARS image found in " + ", ".join(OVMF_DIRS))
    if not problems:
        return 0
    for problem in problems:
        print(f"[x] {problem}", file=sys.stderr)
    print(
        "[x] TCG needs QEMU, OVMF and swtpm. Install them and try again:\n"
        "      apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils "
        "ovmf swtpm swtpm-tools",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    """The entry point ``infra/qemu-accel.sh`` execs.

    ``apply`` is what a build calls on the guest it has just created; the rest is
    for a person and for the builder's preflight.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: python -m ontrak.qemu {accel|reason|check|apply <instance> [project]}")
        return 0 if args else 2
    command = args[0]
    if command == "accel":
        print(accel())
        return 0
    if command == "reason":
        host_facts = host()
        print(reason(host_facts, choose(host_facts, os.environ.get("ONTRAK_QEMU_ACCEL", ""))))
        return 0
    if command == "check":
        return _print_report()
    if command == "apply":
        if len(args) < 2:
            print("usage: python -m ontrak.qemu apply <instance> [project]", file=sys.stderr)
            return 2
        from .config import load_settings
        from .incus import IncusClient

        # The project has to be nameable because incus-windows' tools/pack.sh
        # never passes --project: it builds its VM in the *default* project and
        # this range imports the image into its own one afterwards. That build is
        # what calls this, so the default project is the default here too.
        settings = load_settings()
        settings.incus.project = args[2] if len(args) > 2 else DEFAULT_PROJECT
        client = IncusClient(settings)
        chosen = apply(client, args[1])
        if chosen == TCG:
            print(f"{args[1]}: {TCG} ({TCG_MACHINE_SECTION.splitlines()[1]!r}, {TCG_CPU_MODEL})")
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # `python -m ontrak.qemu`, which infra/qemu-accel.sh execs
    raise SystemExit(main())
