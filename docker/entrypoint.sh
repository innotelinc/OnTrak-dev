#!/usr/bin/env bash
# ============================================================================
# OnTrak container entrypoint.
#
#   docker run --rm -p 8080:8080 --env-file .env ontrak              # portal
#   docker run --rm ontrak demo run --students 3                     # a class, in memory
#   docker run --rm ontrak doctor                                    # preflight
#   docker run --rm -it ontrak bash                                  # a shell
#
# Anything that is not a bare verb is handed straight to the CLI, so
# `docker run ontrak catalog list` works exactly like `ontrak catalog list` on a
# host. The only work this script does on the way in is the work a boot needs:
# make the state and media directories exist (they may be fresh volumes). There
# is no account seeding: sign-in is Authentik's, so accounts are created by a
# sign-in, not by this container.
# ============================================================================
set -euo pipefail

cd /app

log() { printf 'ontrak: %s\n' "$*" >&2; }

# Secrets the stack has to agree on (session cookies, the console JSON key) can
# arrive two ways. On a later run they are interpolated from .env by compose;
# on the very first run — `docker compose up` with no .env yet — compose cannot
# interpolate a value that does not exist, so lab-setup writes them to a shared
# volume and they are read from there. The environment wins when it has a value.
SECRETS_FILE="${ONTRAK_SECRETS_FILE:-/run/ontrak/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  for key in ONTRAK_PORTAL__SECRET ONTRAK_GUAC__SECRET_KEY; do
    eval "current=\${$key:-}"
    [ -n "$current" ] && continue
    value="$(sed -n "s/^${key}=//p" "$SECRETS_FILE" | head -1)"
    [ -n "$value" ] || continue
    export "$key=$value"
    log "$key read from the shared secrets file"
  done
fi

STATE_DIR="${ONTRAK_PATHS__STATE:-state}"
MEDIA_DIR="${ONTRAK_PATHS__MEDIA:-media}"

# Fresh named volumes mount empty and root-owned; the app expects both to exist.
mkdir -p "$STATE_DIR" "$MEDIA_DIR" 2>/dev/null || true

command="${1:-serve}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$command" in
  serve|demo-serve)
    # An empty session key means nobody can sign in, and compose happily starts
    # a container with it; say so here rather than let the portal 500 on login.
    if [ -z "${ONTRAK_PORTAL__SECRET:-}" ]; then
      log "no ONTRAK_PORTAL__SECRET and no ${SECRETS_FILE}: logins will fail."
      log "run 'docker compose up' again once .env exists (lab-setup writes it)."
    fi
    # The hypervisor is on the host. When it is not reachable the portal still
    # serves scenarios, tickets, grading and the admin panel — but nothing can
    # be provisioned, so name the reason once, here, instead of leaving it to
    # whichever page the student opens first.
    if [ ! -S /var/lib/incus/unix.socket ]; then
      log "no Incus socket on the host: training machines are unavailable"
      log "  install it on the host:  sudo infra/bootstrap-host.sh"
      log "  or point at a cluster:   ONTRAK_INCUS__REMOTE=<name> (docs/docker.md)"
      log "  or run without one:      ONTRAK_DEMO__ENABLED=true"
    fi
    log "starting the portal on ${ONTRAK_PORTAL__HOST:-0.0.0.0}:${ONTRAK_PORTAL__PORT:-8080}"
    exec python3 -m ontrak serve
    ;;
  demo)
    exec python3 -m ontrak demo "$@"
    ;;
  bash|sh)
    exec "$command" "$@"
    ;;
  *)
    # `doctor`, `catalog list`, `scenario validate`, `pool status`, … — the CLI
    # is the interface, so no second copy of it lives here.
    exec python3 -m ontrak "$command" "$@"
    ;;
esac
