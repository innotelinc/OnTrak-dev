"""The QEMU accelerator decision, and the instance config it writes.

The rule is small enough to state in one line — use KVM unless this host's KVM is
nested on AMD, in which case QEMU has to emulate the CPU — but both halves of the
range depend on it and neither can be run in a test: the golden build wants two
hours and a Windows ISO, and a template build wants a guest that answers WinRM. So
the decision is a pure function of three facts (:func:`choose`) and everything
around it here is exercised against those facts or against ``FakeIncus``.

What is worth pinning down is not "tcg was returned" but the two ways this can
hurt a range that was working:

* it must not change a host where KVM works — including a nested *Intel* host,
  which is asserted to keep KVM because nothing here has measured it failing;
  and
* it must not silently disable anything the guest needs. The values it writes are
  an accelerator and a CPU model, and nothing in this file may touch
  ``security.secureboot`` or the ``tpm`` device.
"""

from __future__ import annotations

import shutil

import pytest

from ontrak import qemu
from ontrak.qemu import KVM, TCG, Host, choose, missing_packages, ovmf, reason, tcg_instance_config

from .helpers import FakeIncus

BARE_METAL_AMD = Host(kvm=True, virt="none", vendor="AuthenticAMD")
BARE_METAL_INTEL = Host(kvm=True, virt="none", vendor="GenuineIntel")
WSL_AMD = Host(kvm=True, virt="wsl", vendor="AuthenticAMD")
WSL_INTEL = Host(kvm=True, virt="wsl", vendor="GenuineIntel")
NO_KVM = Host(kvm=False, virt="wsl", vendor="GenuineIntel")


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_a_host_with_usable_kvm_gets_it():
    assert choose(BARE_METAL_AMD) == KVM
    assert choose(BARE_METAL_INTEL) == KVM


def test_nested_amd_kvm_falls_back_to_software_emulation():
    """The measurement this exists for: WSL2/Hyper-V on a Ryzen."""
    assert choose(WSL_AMD) == TCG


def test_nested_intel_kvm_keeps_kvm_because_nothing_here_measured_otherwise():
    """The rule is one measurement, not a theory about all nesting."""
    assert choose(WSL_INTEL) == KVM


def test_a_host_with_no_kvm_cannot_use_it():
    assert choose(NO_KVM) == TCG


def test_the_operator_can_settle_it_either_way():
    assert choose(WSL_AMD, "kvm") == KVM
    assert choose(BARE_METAL_INTEL, "tcg") == TCG
    assert choose(NO_KVM, "  TCG  ") == TCG  # forgiving about how it is typed
    assert choose(WSL_AMD, "banana") == TCG  # nonsense does not overrule the host


def test_reason_names_the_accelerator_and_says_which_host_it_is():
    fallback = reason(WSL_AMD, TCG)
    assert "TCG" in fallback and "wsl" in fallback
    assert "KVM+SMM" in fallback
    assert "hardware virtualisation" in reason(BARE_METAL_AMD, KVM)
    assert "no usable /dev/kvm" in reason(NO_KVM, TCG)


# --------------------------------------------------------------------------- #
# the instance config
# --------------------------------------------------------------------------- #
def test_the_config_moves_the_accelerator_and_the_cpu_model():
    values = tcg_instance_config()
    assert values["raw.qemu.conf"] == '[machine]\naccel = "tcg"\n'
    assert values["raw.qemu"] == "-cpu max"


def test_applying_it_to_what_it_already_wrote_changes_nothing():
    """A build re-run against the same checkout must not append again."""
    once = tcg_instance_config()
    assert tcg_instance_config(once["raw.qemu"], once["raw.qemu.conf"]) == {}
    assert tcg_instance_config(once["raw.qemu"], once["raw.qemu.conf"]) == {}


def test_an_existing_raw_qemu_keeps_its_arguments_and_gains_the_cpu():
    """Legacy workloads set ``-M pc -cpu pentium2`` this way; it must survive.

    The CPU model is appended rather than replaced, so QEMU's last-wins picks ours
    up — and the ``-M pc`` the workload needs is still on the command line.
    """
    values = tcg_instance_config("-M pc -cpu pentium2 -vga cirrus", "")
    assert values["raw.qemu"] == "-M pc -cpu pentium2 -vga cirrus -cpu max"


def test_an_accel_somebody_else_set_is_left_alone():
    values = tcg_instance_config("", '[machine]\naccel = "kvm"\n')
    assert "raw.qemu.conf" not in values, "an operator's own accel was overwritten"


def test_nothing_here_touches_secure_boot_or_the_tpm():
    values = tcg_instance_config()
    assert not [key for key in values if "secureboot" in key.lower()]
    assert not [key for key in values if "tpm" in key.lower()]


# --------------------------------------------------------------------------- #
# applying it to an instance
# --------------------------------------------------------------------------- #
def test_apply_writes_nothing_where_kvm_works():
    incus = FakeIncus()
    incus.add_instance("tpl-x", running=False)
    assert qemu.apply(incus, "tpl-x", KVM) == KVM
    assert incus.configs == []


def test_apply_points_a_guest_at_the_emulator_once():
    incus = FakeIncus()
    incus.add_instance("tpl-x", running=False)
    assert qemu.apply(incus, "tpl-x", TCG) == TCG
    written = {key: value for name, key, value in incus.configs if name == "tpl-x"}
    assert written["raw.qemu.conf"] == '[machine]\naccel = "tcg"\n'
    assert written["raw.qemu"] == "-cpu max"

    before = len(incus.configs)
    qemu.apply(incus, "tpl-x", TCG)
    assert len(incus.configs) == before, "a second apply rewrote the config"


def test_apply_does_not_clobber_a_raw_qemu_set_somewhere_else():
    incus = FakeIncus()
    incus.add_instance("tpl-legacy", running=False)
    incus.set_configs("tpl-legacy", {"raw.qemu": "-M pc -cpu pentium2"})
    qemu.apply(incus, "tpl-legacy", TCG)
    written = {key: value for name, key, value in incus.configs if name == "tpl-legacy"}
    assert written["raw.qemu"] == "-M pc -cpu pentium2 -cpu max"


# --------------------------------------------------------------------------- #
# what the software path needs on the host
# --------------------------------------------------------------------------- #
def test_ovmf_is_detected_wherever_it_is_installed(tmp_path, monkeypatch):
    """Incus keeps its own copies and the ovmf package keeps the distro's.

    So the names are searched for in both, and a host that has only one of them —
    or a differently-versioned one — is still read correctly.
    """
    (tmp_path / "OVMF_CODE_4M.fd").write_bytes(b"code")
    (tmp_path / "OVMF_VARS_4M.ms.fd").write_bytes(b"vars")
    (tmp_path / "not-firmware.txt").write_text("")
    monkeypatch.setattr(qemu, "OVMF_DIRS", (str(tmp_path),))
    assert ovmf() == {
        "code": str(tmp_path / "OVMF_CODE_4M.fd"),
        "vars": str(tmp_path / "OVMF_VARS_4M.ms.fd"),
    }


def test_ovmf_reports_nothing_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(qemu, "OVMF_DIRS", (str(tmp_path / "absent"),))
    assert ovmf() == {}


@pytest.mark.skipif(shutil.which("dpkg-query") is None, reason="no dpkg-query on this host")
def test_a_package_that_is_not_installed_is_named():
    assert missing_packages(("ontrak-no-such-package",)) == ["ontrak-no-such-package"]


@pytest.mark.skipif(shutil.which("dpkg-query") is None, reason="no dpkg-query on this host")
def test_an_installed_package_is_not_reported_as_missing():
    assert "bash" not in missing_packages(("bash",))


# --------------------------------------------------------------------------- #
# the entry point infra/qemu-accel.sh execs
# --------------------------------------------------------------------------- #
def test_the_cli_prints_the_accelerator(capsys, monkeypatch):
    monkeypatch.setenv("ONTRAK_QEMU_ACCEL", TCG)
    assert qemu.main(["accel"]) == 0
    assert capsys.readouterr().out.strip() == TCG


def test_the_cli_reports_what_it_found(capsys, monkeypatch):
    """The report is the contract; the exit code is *this host's* readiness.

    ``check`` is a preflight for the machine that will build the guests, so on a
    TCG host that has what it needs it exits 0. Both host-dependent inputs are
    pinned here rather than read: a CI runner has neither the packages nor the
    firmware, and a test that asserted its readiness would be asserting the
    runner rather than this module.
    """
    monkeypatch.setenv("ONTRAK_QEMU_ACCEL", TCG)
    monkeypatch.setattr(qemu, "missing_packages", lambda: [])
    monkeypatch.setattr(qemu, "ovmf", lambda: {"code": "/ovmf/CODE.fd", "vars": "/ovmf/VARS.fd"})
    assert qemu.main(["check"]) == 0
    out = capsys.readouterr().out
    assert '"accel"' in out and '"ovmf"' in out


def test_the_cli_refuses_a_host_that_cannot_build(capsys, monkeypatch):
    """The reason the preflight exists: a TCG host missing QEMU, OVMF or swtpm
    fails here rather than hours into a build, and names what to install."""
    monkeypatch.setenv("ONTRAK_QEMU_ACCEL", TCG)
    monkeypatch.setattr(qemu, "missing_packages", lambda: ["swtpm"])
    monkeypatch.setattr(qemu, "ovmf", dict)
    assert qemu.main(["check"]) == 1
    assert "swtpm" in capsys.readouterr().err


def test_the_cli_says_how_to_use_it(capsys):
    assert qemu.main(["-h"]) == 0
    assert "apply <instance>" in capsys.readouterr().out
    assert qemu.main([]) == 2
    assert qemu.main(["nonsense"]) == 2
