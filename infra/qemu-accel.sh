#!/usr/bin/env bash
#
# qemu-accel.sh — the shell side of the QEMU accelerator decision.
#
#   infra/qemu-accel.sh --accel              kvm or tcg, for this host
#   infra/qemu-accel.sh --reason            the same, as a sentence for a log
#   infra/qemu-accel.sh --check             preflight the software path
#   infra/qemu-accel.sh --apply <instance> [project]   move that guest off KVM
#
# Nothing here decides anything: the decision, the config it writes and the
# preflight all live in ontrak/qemu.py, because both halves of this range need
# them. The golden build is a shell script and incus-windows' tools/pack.sh is
# POSIX sh, but the templates and the student sessions are created by the Python
# runtime — and two implementations of "can this host run a Windows guest" is one
# more than can stay in agreement.
#
# `--apply` is what the build calls on the guest it has just created, through the
# hook infra/incus-windows-pack.sh writes into pack.sh. It is also safe to run by
# hand on a VM that is already in ERROR from the SMM fault.
#
# Environment:
#   ONTRAK_QEMU_ACCEL   kvm or tcg, to overrule the detection for this host
#   ONTRAK_PY           the python to use (default: the checkout's venv)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ONTRAK_PY:-${PROJECT_ROOT}/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "python3 not found" >&2; exit 1; }

# pack.sh calls this from inside the incus-windows checkout, so the package has to
# be importable from where the script is, not from the caller's directory.
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

usage() {
  echo "usage: $0 {--accel|--reason|--check|--apply <instance> [project]}" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
case "$1" in
  --accel)  exec "$PY" -m ontrak.qemu accel ;;
  --reason) exec "$PY" -m ontrak.qemu reason ;;
  --check)  exec "$PY" -m ontrak.qemu check ;;
  --apply)
    [[ $# -ge 2 && $# -le 3 ]] || usage
    exec "$PY" -m ontrak.qemu apply "${@:2}"
    ;;
  *) usage ;;
esac
