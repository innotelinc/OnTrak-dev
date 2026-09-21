#!/usr/bin/env bash
# OnTrak — one-shot bootstrap.
#
# Installs the attribution-guard hooks, creates the virtualenv, installs the Python
# dependencies and fills .env. Safe to re-run: it never overwrites a value that is
# already set, and re-running only re-installs what is missing.
#
# The root compose stack runs the control plane and the console gateway (see
# docker-compose.yml, docs/docker.md). The training machines themselves are not
# containers: they are Incus virtual machines on the host, built by
# infra/bootstrap-host.sh and infra/build-templates.sh.
set -euo pipefail

APP="$(basename "$(pwd)")"
VENV=".venv"

echo "==> ${APP} bootstrap"

# ── attribution guard hooks ───────────────────────────────────────────────────
if [ -d .githooks ]; then
  git config core.hooksPath .githooks
  echo "==> attribution guard hooks installed (.githooks)"
fi

# ── .env ─────────────────────────────────────────────────────────────────────
# Creates .env from .env.example when it is missing, and fills the blanks the
# local stack cannot start without (session-cookie key, console key). Anything
# that already holds a value — a `vault://` reference included — is left alone.
bash scripts/secrets.sh

# ── python environment ────────────────────────────────────────────────────────
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "!! ${PYTHON} not found — install Python 3.11 or newer" >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "==> creating ${VENV}"
  "$PYTHON" -m venv "$VENV"
fi

# Go through `python -m pip`, never ${VENV}/bin/pip. The launcher script is not
# always created: a venv made with --without-pip, or by a `python3 -m venv` whose
# host has python3-venv only half installed, has no pip binary at all — and this
# script then failed on a venv it had just created itself, with
# ".venv/bin/pip: No such file or directory". It is the same pip either way, found
# the way the Dockerfile and CI already find it.
VENV_PY="${VENV}/bin/python"
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  echo "==> ${VENV} has no pip; installing it with ensurepip"
  "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 \
    || { echo "!! could not install pip into ${VENV} — install python3-venv (with pip) and try again" >&2; exit 1; }
fi

echo "==> installing dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt
"$VENV_PY" -m pip install --quiet -e .

# ── optional: PowerShell, for validating guest scripts locally ────────────────
if command -v pwsh >/dev/null 2>&1; then
  echo "==> pwsh found: guest scripts can be syntax-checked locally"
else
  echo "==> pwsh not found (optional): scenario scripts are only parsed at template build"
fi

echo
echo "==> done. Next:"
echo "    make demo        # full student flow, no hypervisor needed"
echo "    make up          # or 'docker compose up -d': one command, first-run setup included"
echo "    make serve       # the same portal, on the host instead"
echo "    make check       # host readiness, including Incus and KVM"
echo
echo "    Building real Windows templates needs a host: infra/bootstrap-host.sh"
