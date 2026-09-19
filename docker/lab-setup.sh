#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# OnTrak — first-run lab setup, from a container, against the host.
#
# `docker compose up` runs this before the portal (`depends_on:
# service_completed_successfully`), so one command is enough for a working lab:
#
#   1. `.env` in the project directory — generated, never overwriting a value.
#   2. `/run/ontrak/secrets.env` on the shared volume the portal and the console
#      gateway read from. Compose cannot interpolate a secret that does not
#      exist yet (interpolation happens when the project is loaded, before any
#      container runs), and `env_file` is read even earlier, so on a first run
#      the two services that must agree on a key take it from this file.
#   3. Incus on the host: installed, initialised, and given the OnTrak project,
#      storage pool, lab bridge and profiles. This is done by running the host's
#      own `infra/bootstrap-host.sh` inside the host's namespaces, so a host
#      prepared this way and a host prepared by hand end up identical.
#
# It never blocks the control plane. A machine that cannot run VMs (Docker
# Desktop, no /dev/kvm, a container without host access) is reported and
# skipped, and the portal still comes up — in demo mode, or against a remote
# Incus cluster.
#
# Environment (from .env, through the compose file):
#   ONTRAK_LAB_SETUP        auto (default) | force | off
#   ONTRAK_LAB_BOOTSTRAP    host script to run (default: infra/bootstrap-host.sh)
#   ONTRAK_STORAGE_DRIVER   dir (default) | btrfs | zfs | lvm — passed to the host
#   ONTRAK_STORAGE_SOURCE   e.g. /dev/nvme1n1 for zfs/lvm
#   ONTRAK_INCUS__REMOTE    a remote cluster name turns the host step off
#   ONTRAK_PROJECT_DIR      where the checkout is mounted (default /project)
#   ONTRAK_SECRETS_FILE     where the shared secrets land (default /run/ontrak/secrets.env)
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

PROJECT_DIR="${ONTRAK_PROJECT_DIR:-/project}"
SECRETS_FILE="${ONTRAK_SECRETS_FILE:-/run/ontrak/secrets.env}"
MODE="${ONTRAK_LAB_SETUP:-auto}"
BOOTSTRAP="${ONTRAK_LAB_BOOTSTRAP:-infra/bootstrap-host.sh}"
REMOTE="${ONTRAK_INCUS__REMOTE:-local}"

# Everything goes to stdout: this output is read as a report of what the first
# run did, and interleaving two streams through `docker compose logs` scrambles
# the order of exactly the lines a new operator needs. There is no fatal exit
# here on purpose — a host that cannot run VMs is reported and skipped, and the
# control plane still comes up, which is the whole promise of one command.
log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }

# ── 1. secrets ──────────────────────────────────────────────────────────────
# A first run has no .env, and both the portal (session cookies) and the console
# gateway (the JSON auth key) need the same values. scripts/secrets.sh owns the
# rules — it never overwrites a value that is already there — so it is the one
# generator, called here instead of a second copy of it.
write_secrets() {
  mkdir -p "$(dirname "$SECRETS_FILE")" 2>/dev/null || true
  if [ ! -f "$PROJECT_DIR/scripts/secrets.sh" ]; then
    warn "no scripts/secrets.sh at ${PROJECT_DIR} — cannot generate .env"
    return 0
  fi

  ( cd "$PROJECT_DIR" && bash scripts/secrets.sh ) || {
    warn "scripts/secrets.sh failed; fill in .env by hand and re-run 'docker compose up'"
    return 0
  }

  # Readable, not secret-mode: the console gateway is a Tomcat image that runs
  # its entrypoint as its own unprivileged user, and it has to read this file.
  # The volume is mounted into those two containers and nothing else.
  umask 022
  {
    printf '# Written by docker/lab-setup.sh on the first run. The portal and the\n'
    printf '# console gateway read this when their environment has no value — which is\n'
    printf '# what makes a bare "docker compose up" work before a .env exists.\n'
    grep -E '^(ONTRAK_PORTAL__SECRET|ONTRAK_GUAC__SECRET_KEY)=' \
      "$PROJECT_DIR/.env"
  } >"$SECRETS_FILE"
  chmod 644 "$SECRETS_FILE" 2>/dev/null || true

  # .env was just created inside a bind-mounted directory, so it is owned by the
  # container's root. Hand it to whoever owns the checkout, or the operator
  # cannot edit it.
  local owner
  owner="$(stat -c '%u:%g' "$PROJECT_DIR" 2>/dev/null || true)"
  if [ -n "$owner" ] && [ "$owner" != "0:0" ]; then
    chown "$owner" "$PROJECT_DIR/.env" 2>/dev/null || true
  fi

  local keys
  keys="$(grep -c '^ONTRAK_.*SECRET' "$SECRETS_FILE" 2>/dev/null || true)"
  ok "secrets ready (${keys:-0} shared key(s)) — ${PROJECT_DIR}/.env and ${SECRETS_FILE}"
}

# ── 2. the host's Incus ─────────────────────────────────────────────────────
# The host namespace is entered through pid 1, which the compose service asks
# for with `pid: host` + `privileged: true`. The host script resolves its paths
# from its own location, so it is run from a location the host can read: the
# checkout at its own host path when that is knowable, otherwise a staged copy
# (see host_visible_bootstrap).
#
# The namespaces are listed rather than `-a`, which also asks for the *time*
# namespace: where that one is restricted (a container host, a hardened kernel)
# `nsenter -a` fails outright with "reassociate to namespace 'ns/time' failed",
# and a detector that treats that as "no host access" skips installing Incus on
# a machine that would have been perfectly capable. mount, uts, ipc, net and pid
# are the ones installing packages and starting a daemon actually need.
HOST_NS="-m -u -i -n -p"
host_run() {
  # shellcheck disable=SC2086  # the list is meant to split into arguments
  nsenter -t 1 $HOST_NS -- "$@"
}

have_host() {
  host_run /bin/true >/dev/null 2>&1
}

incus_ready() {
  host_run /bin/sh -c 'command -v incus >/dev/null 2>&1 && incus info >/dev/null 2>&1' >/dev/null 2>&1
}

prepare_host() {
  if [ "$MODE" = "off" ]; then
    log "lab setup disabled (ONTRAK_LAB_SETUP=off) — the portal gets no hypervisor"
    return 0
  fi
  if [ "$REMOTE" != "local" ]; then
    log "using the Incus cluster '${REMOTE}' — nothing to install on this host"
    return 0
  fi

  if ! have_host; then
    warn "cannot reach the host's namespaces, so Incus cannot be installed for you"
    warn "  (Docker Desktop, or a Docker daemon on another machine). Pick one:"
    warn "  · on the lab host:  sudo infra/bootstrap-host.sh"
    warn "  · a remote cluster: set ONTRAK_INCUS__REMOTE   (docs/docker.md)"
    warn "  · demo mode:        ONTRAK_DEMO__ENABLED=true  (no training machines)"
    return 0
  fi

  if [ "$MODE" != "force" ] && incus_ready; then
    ok "Incus is already answering on the host — nothing to install"
    report_state
    return 0
  fi

  if [ ! -f "$PROJECT_DIR/$BOOTSTRAP" ]; then
    warn "no host bootstrap at ${PROJECT_DIR}/${BOOTSTRAP}"
    return 0
  fi

  local script
  script="$(host_visible_bootstrap)" || {
    warn "the host cannot read ${BOOTSTRAP}, and staging a copy failed"
    return 0
  }

  log "preparing the host: ${BOOTSTRAP} (installs Incus, creates the pool,"
  log "bridge, project and profiles — this can take a few minutes)"
  printf '      running %s\n' "$script"
  if host_run "$script"; then
    ok "host prepared"
    report_state
  else
    # Named by the path the *host* resolved, not this container's /project: the
    # command printed here is meant to be pasted into a shell on the host, and
    # that is the one place it is guaranteed to work.
    warn "the host bootstrap failed — the portal is up, but training machines"
    warn "will not work. Its output is above; to retry this step alone:"
    warn "  sudo ${script}"
  fi
}

# The host has to be able to read the bootstrap script itself, because the
# script resolves its project root from where the file is and reads
# `infra/incus/profile.yaml` from there. Two ways to arrange that: the checkout
# is on the host at the path this container has it (the usual case — the stack
# is started from the project directory), or the few files it needs are staged
# in the host's /run, a tmpfs that is always writable. Staging goes through
# /proc/1/root, so no host path has to be guessed.
host_visible_bootstrap() {
  local candidate
  for candidate in "${ONTRAK_PROJECT_DIR_HOST:-}" "$PROJECT_DIR"; do
    [ -n "$candidate" ] || continue
    if host_run /bin/sh -c "test -f '$candidate/$BOOTSTRAP'" 2>/dev/null; then
      printf '%s' "$candidate/$BOOTSTRAP"
      return 0
    fi
  done

  # The copy keeps the script at <stage>/infra/bootstrap-host.sh: the script
  # derives its project root from its own location, so the layout is part of
  # the contract, and `infra/` is the whole of what it reads.
  local stage=/run/ontrak-bootstrap
  if ! host_run /bin/sh -c "rm -rf '$stage' && mkdir -p '$stage/infra'" >/dev/null 2>&1; then
    return 1
  fi
  mkdir -p "/proc/1/root$stage/infra" 2>/dev/null || return 1
  cp -a "$PROJECT_DIR/infra/." "/proc/1/root$stage/infra/" 2>/dev/null || return 1
  printf '%s' "$stage/$BOOTSTRAP"
}

report_state() {
  local summary
  summary="$(host_run /bin/sh -c 'incus version 2>/dev/null | head -1; incus storage list --format=csv -c n,d 2>/dev/null | head -3; incus network list --format=csv -c n 2>/dev/null | head -3' 2>/dev/null || true)"
  if [ -n "$summary" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && printf '      %s\n' "$line"
    done <<<"$summary"
  fi
}

# ── run ─────────────────────────────────────────────────────────────────────
log "OnTrak first-run lab setup"
write_secrets
prepare_host

log "ready. Next:"
printf '      portal      http://localhost:%s\n' "${ONTRAK_PORTAL__PORT:-8080}"
# The console address a *student's browser* uses, which is not necessarily this
# host: a deployment puts a TLS console host here, and printing the local port
# regardless would tell the operator something that is not true.
guac_url="${ONTRAK_GUAC__BASE_URL:-http://localhost:${ONTRAK_PORTAL__PORT:-8080}/guacamole/}"
printf '      console     %s\n' "$guac_url"
case "$guac_url" in
  *localhost* | *127.0.0.1*) ;;
  *)
    printf '      %s\n' "            students' browsers resolve that host, not this machine —"
    printf '      %s\n' "            set ONTRAK_GUAC__BASE_URL in .env for a lab on this host."
    ;;
esac
printf '      published   bind %s, port %s (the portal and the console share it)\n' \
  "${ONTRAK_BIND_ADDR:-127.0.0.1}" "${ONTRAK_PORTAL__PORT:-8080}"
printf '      sign in     through Authentik — set the four ONTRAK_PORTAL__OIDC_*\n'
printf '      %s\n' "                  values in .env (docs/operations.md#sign-in)"
printf '      verify lab  docker compose exec portal python3 -m ontrak doctor\n'
exit 0
