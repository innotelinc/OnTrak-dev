#!/usr/bin/env bash
#
# OnTrak first-boot provisioning.
#
# Runs once, from ontrak-firstboot.service, on the first boot of a host installed
# from the OnTrak installer ISO. It turns a bare Ubuntu machine into a range host:
#
#   1. the packages the install itself does not need but the range does
#      (git, docker, and Docker's compose plugin)
#   2. the OnTrak checkout, cloned from GitHub
#   3. infra/bootstrap-host.sh   — Incus, the lab bridge, the ontrak project
#   4. make setup                — venv, dependencies, guard hooks, .env
#   5. the portal stack          — docker compose up -d --build
#
# Everything here is idempotent, so a failed first boot is fixed by running it
# again, by hand:
#
#   sudo /usr/local/sbin/ontrak-firstboot.sh --force
#
# Output goes to the journal, to the console and to
# /var/log/ontrak-firstboot.log — a host with no display still shows progress on
# its serial console.
#
# Settings come from /etc/ontrak/firstboot.env (optional; the ISO installs a
# .example). See infra/installer/README.md.
#
#   ONTRAK_REPO_URL         git remote to install
#                           (default: https://github.com/innotelinc/OnTrak.git)
#   ONTRAK_BRANCH           branch to check out (default: main)
#   ONTRAK_GIT_TOKEN        token for a private remote; tried only after the
#                           anonymous clone fails
#   ONTRAK_BUILD_TEMPLATES  1 to also build the golden Windows image and every
#                           scenario template (hours, and needs Windows media —
#                           see docs/operations.md; off by default)
#
#   ONTRAK_STORAGE_DRIVER, ONTRAK_STORAGE_SOURCE, … are passed straight through
#   to infra/bootstrap-host.sh, so an operator can pick ZFS or btrfs for a real
#   class from /etc/ontrak/firstboot.env.

set -euo pipefail

CONFIG="${ONTRAK_FIRSTBOOT_CONFIG:-/etc/ontrak/firstboot.env}"
LOG="/var/log/ontrak-firstboot.log"
STATE_DIR="/var/lib/ontrak"
DONE_FLAG="${STATE_DIR}/firstboot.done"
REPO_URL="${ONTRAK_REPO_URL:-https://github.com/innotelinc/OnTrak.git}"
BRANCH="${ONTRAK_BRANCH:-main}"
REPO_DIR="${ONTRAK_REPO_DIR:-/opt/ontrak}"
INSTALL_LOGIN="${ONTRAK_INSTALL_LOGIN:-}"

# ------------------------------------------------------------------ logging --
mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
# Tee to the log *and* the console: under systemd the console copy lands in the
# journal and on the serial console, which is the only view a headless install
# has.
exec > >(tee -a "$LOG") 2>&1

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m--- %s\033[0m\n' "$*"; }

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

[[ $EUID -eq 0 ]] || die "run this with sudo"

if [[ -f "$CONFIG" ]]; then
  log "reading $CONFIG"
  # shellcheck disable=SC1090  # operator-provided, by design
  set -a; . "$CONFIG"; set +a
  # Re-read the settings the file may have just defined.
  REPO_URL="${ONTRAK_REPO_URL:-$REPO_URL}"
  BRANCH="${ONTRAK_BRANCH:-$BRANCH}"
  REPO_DIR="${ONTRAK_REPO_DIR:-$REPO_DIR}"
  INSTALL_LOGIN="${ONTRAK_INSTALL_LOGIN:-$INSTALL_LOGIN}"
else
  log "no $CONFIG: using defaults"
fi

if [[ -e "$DONE_FLAG" && $FORCE -eq 0 ]]; then
  log "already provisioned ($DONE_FLAG); re-run with --force to do it again"
  exit 0
fi

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
step "OnTrak first-boot provisioning (started $STARTED)"
log "host      : $(hostname)  (kernel $(uname -r))"
log "repo      : $REPO_URL ($BRANCH)"
log "checkout  : $REPO_DIR"

# ------------------------------------------------------------------ network --
step "waiting for the network"
# The first boot races the NIC coming up, and apt/git both need it. Give up
# eventually rather than hanging the boot forever: the operator can --force.
NET_OK=0
for _ in $(seq 1 60); do
  if getent hosts github.com >/dev/null 2>&1 \
     || curl -fsS -m 5 -o /dev/null https://github.com 2>/dev/null; then
    NET_OK=1; break
  fi
  sleep 5
done
if [[ $NET_OK -eq 1 ]]; then
  log "network is up"
else
  warn "no route to github.com after 5 minutes — continuing anyway"
  warn "once the network is up, re-run: sudo /usr/local/sbin/ontrak-firstboot.sh --force"
fi

# ----------------------------------------------------------------- packages --
step "installing the range host's packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# git: clone the checkout. docker.io + docker-compose-v2: the portal stack
# (portal, guacd, guacamole, the lab-setup job) is a compose project.
apt-get install -y --no-install-recommends git ca-certificates
apt-get install -y docker.io docker-compose-v2
systemctl enable --now docker >/dev/null 2>&1 || warn "could not start docker.service"
docker compose version >/dev/null 2>&1 || die "docker compose (v2) is not available after install"
log "$(git --version); $(docker --version); $(docker compose version --short 2>/dev/null | sed 's/^/compose /')"

# --------------------------------------------------------------------- repo --
step "installing the OnTrak checkout"
checkout() {
  local url="$1"
  if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" remote set-url origin "$url"
    git -C "$REPO_DIR" fetch --prune origin "$BRANCH"
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
  else
    rm -rf "$REPO_DIR"
    git clone --branch "$BRANCH" "$url" "$REPO_DIR"
  fi
}

# A private remote is the normal case for a real estate, so try the anonymous
# clone first and fall back to a token rather than failing on a 404 that reads
# like a network problem.
git config --global --get-all safe.directory | grep -qxF "$REPO_DIR" \
  || git config --global --add safe.directory "$REPO_DIR"

if ! checkout "$REPO_URL"; then
  if [[ -n "${ONTRAK_GIT_TOKEN:-}" ]]; then
    warn "anonymous clone failed; retrying with ONTRAK_GIT_TOKEN"
    TOKEN_URL="$(printf '%s' "$REPO_URL" \
      | sed -E "s#^https://#https://x-access-token:${ONTRAK_GIT_TOKEN}@#")"
    checkout "$TOKEN_URL" || die "could not clone $REPO_URL even with a token"
    # Never leave the token in the checkout's config.
    git -C "$REPO_DIR" remote set-url origin "$REPO_URL"
  else
    die "could not clone $REPO_URL.
    If the repository is private, put a token in $CONFIG:
      ONTRAK_GIT_TOKEN=github_pat_…
    then re-run: sudo /usr/local/sbin/ontrak-firstboot.sh --force"
  fi
fi
log "checkout at $(git -C "$REPO_DIR" rev-parse --short HEAD) ($(git -C "$REPO_DIR" log -1 --pretty=%s))"

# -------------------------------------------------------------------- incus --
step "preparing the host hypervisor (infra/bootstrap-host.sh)"
# This is the longest step: it installs Incus, creates the storage pool, the lab
# bridge and the ontrak project. It is idempotent, so re-running is safe.
if "$REPO_DIR/infra/bootstrap-host.sh"; then
  log "host hypervisor is ready"
  BOOTSTRAP_OK=1
else
  BOOTSTRAP_OK=0
  warn "infra/bootstrap-host.sh failed — the portal will come up, but it cannot"
  warn "create machines until this is fixed. The usual cause is no /dev/kvm:"
  warn "enable VT-x/AMD-V in firmware (bare metal) or nested virtualisation (VM)."
fi

# ------------------------------------------------------- operator's account --
# The interactive installer creates the admin login; put it in the groups the
# range needs so the operator is not fighting sudo on first sign-in.
if [[ -z "$INSTALL_LOGIN" ]]; then
  INSTALL_LOGIN="$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')"
fi
if [[ -n "$INSTALL_LOGIN" ]]; then
  log "adding $INSTALL_LOGIN to the incus and docker groups"
  usermod -aG incus "$INSTALL_LOGIN" 2>/dev/null || warn "no incus group yet"
  usermod -aG docker "$INSTALL_LOGIN" 2>/dev/null || warn "no docker group yet"
  warn "$INSTALL_LOGIN must log out and back in for the new groups to apply"
fi

# ------------------------------------------------------------------- portal --
step "make setup (venv, dependencies, guard hooks, .env)"
# `make setup` needs the venv tooling and pip; bootstrap-host.sh installed both.
if ! make -C "$REPO_DIR" setup; then
  warn "'make setup' failed; continuing to the portal stack, which does not need it"
fi

step "starting the portal stack (docker compose up -d --build)"
# `make up` is the supported one-command first run: it seeds .env (make secrets)
# and then builds and starts portal + guacd + guacamole + the one-shot lab-setup
# job. The image build needs the internet.
if ! make -C "$REPO_DIR" up; then
  die "'make up' failed. Inspect: cd $REPO_DIR && docker compose logs
    The most common causes are no internet access for the image build, and a
    port already bound (see ONTRAK_PORTAL__PORT in $REPO_DIR/.env)."
fi

PORT="$(sed -n 's/^ONTRAK_PORTAL__PORT=//p' "$REPO_DIR/.env" 2>/dev/null | tail -1)"
PORT="${PORT:-8080}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

# ------------------------------------------------- optional: range contents --
# The golden Windows image and the scenario templates are hours of work and need
# Windows media that is not redistributable, so they are opt-in.
if [[ "${ONTRAK_BUILD_TEMPLATES:-0}" == "1" ]]; then
  step "building the golden image and every scenario template"
  warn "this takes hours; watch it with: journalctl -fu ontrak-firstboot"
  make -C "$REPO_DIR" golden || warn "'make golden' failed (see docs/operations.md)"
  make -C "$REPO_DIR" templates || warn "'make templates' failed (see docs/operations.md)"
fi

# --------------------------------------------------------------------- done --
printf '\n%s\n' "=== OnTrak range host is up ==="
cat <<EOF

  portal      http://${IP:-<this-host>}:${PORT}
  console     http://${IP:-<this-host>}:${PORT}/guacamole/
  sign in     through Authentik (set ONTRAK_PORTAL__OIDC_* in $REPO_DIR/.env)
  checkout    $REPO_DIR
  log         $LOG
  hypervisor  $([[ "${BOOTSTRAP_OK:-0}" == 1 ]] && echo ready || echo 'NOT ready — see the warnings above')

Next steps, from $REPO_DIR:
  make check                       # preflight: Python, Incus, KVM, storage, secrets
  make provision-plan              # DNS + TLS + edge through Cerulean (needs CERULEAN_API_TOKEN)
  make golden && make templates    # Windows image, then the scenario templates
  infra/lab-services.sh            # the intranet targets the scenarios test against

EOF

if [[ "${BOOTSTRAP_OK:-0}" != 1 ]]; then
  warn "not marking first boot complete: re-run after fixing the hypervisor"
  exit 1
fi

date -u +%Y-%m-%dT%H:%M:%SZ >"$DONE_FLAG"
log "first boot complete (finished $(cat "$DONE_FLAG"))"
