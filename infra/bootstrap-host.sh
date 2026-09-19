#!/usr/bin/env bash
#
# Prepare an Ubuntu host to run OnTrak.
#
#   sudo infra/bootstrap-host.sh
#
# Idempotent: safe to re-run. Creates the storage pool, the lab bridge (with
# ontrak.lab as the DNS domain, so guests resolve fileserver.ontrak.lab),
# the `ontrak` Incus project, and the resource-limits profile.
#
# Environment overrides:
#   ONTRAK_STORAGE_DRIVER   dir (default) | btrfs | zfs | lvm
#   ONTRAK_STORAGE_SOURCE   e.g. /dev/nvme1n1 for zfs/lvm, or a loop file path
#   ONTRAK_STORAGE_POOL     pool name (default: default)
#   ONTRAK_NETWORK          bridge name (default: ontrak0)
#   ONTRAK_NET_CIDR         bridge address (default: 10.20.0.1/24)
#   ONTRAK_DHCP_RANGE       DHCP range (default: 10.20.0.100-10.20.0.200)
#   ONTRAK_DOMAIN           DNS domain (default: ontrak.lab)
#   ONTRAK_INCUS_PROJECT     project name (default: ontrak)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

STORAGE_DRIVER="${ONTRAK_STORAGE_DRIVER:-dir}"
STORAGE_SOURCE="${ONTRAK_STORAGE_SOURCE:-}"
STORAGE_POOL="${ONTRAK_STORAGE_POOL:-default}"
NETWORK="${ONTRAK_NETWORK:-ontrak0}"
NET_CIDR="${ONTRAK_NET_CIDR:-10.20.0.1/24}"
DHCP_RANGE="${ONTRAK_DHCP_RANGE:-10.20.0.100-10.20.0.200}"
DOMAIN="${ONTRAK_DOMAIN:-ontrak.lab}"
PROJECT="${ONTRAK_INCUS_PROJECT:-ontrak}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo (it installs packages and creates bridges)"

# ---------------------------------------------------------------- preflight --
[[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
log "host: ${PRETTY_NAME:-unknown}"

# Only Debian and Ubuntu are handled here: the script installs Incus from
# packages. Anything else is told so once, instead of failing three apt calls
# later with a message about a missing binary.
if ! command -v apt-get >/dev/null 2>&1; then
  die "this script installs Incus with apt, so it handles Debian and Ubuntu only.
    Install Incus yourself (https://linuxcontainers.org/incus/docs/main/install/),
    then either re-run this script on a Debian/Ubuntu host or start the stack with
    ONTRAK_LAB_SETUP=off (the portal works, it just cannot create machines)."
fi

if [[ ! -e /dev/kvm ]]; then
  die "no /dev/kvm — this host cannot run virtual machines.
    On bare metal: enable VT-x/AMD-V in firmware.
    In a VM: enable nested virtualisation, or run the lab on bare metal.
    (kernel $(uname -r); 'kvm-ok' from cpu-checker diagnoses it)"
fi
log "KVM available"

# ------------------------------------------------------------------ packages --
export DEBIAN_FRONTEND=noninteractive
log "installing packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg jq \
  qemu-kvm qemu-utils \
  xorriso genisoimage \
  python3-venv python3-pip \
  >/dev/null

if ! command -v incus >/dev/null; then
  # Upstream (Zabbly) packages track current Incus; the Ubuntu archive copy is
  # often a few releases behind, and Windows VM support moves fast.
  log "installing incus from the upstream stable repository"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://pkgs.zabbly.com/key.asc | gpg --dearmor -o /etc/apt/keyrings/zabbly.gpg
  # shellcheck disable=SC1091  # read fresh, for the codename this host reports
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
  echo "deb [signed-by=/etc/apt/keyrings/zabbly.gpg] https://pkgs.zabbly.com/incus/stable ${codename} main" \
    > /etc/apt/sources.list.d/zabbly-incus-stable.list
  if apt-get update -qq && apt-get install -y incus >/dev/null 2>&1; then
    log "installed incus from pkgs.zabbly.com"
  else
    warn "upstream repository failed; falling back to the distribution package"
    rm -f /etc/apt/sources.list.d/zabbly-incus-stable.list
    apt-get update -qq
    apt-get install -y incus || die "could not install incus"
  fi
fi

if [[ "$STORAGE_DRIVER" == "zfs" ]]; then
  apt-get install -y --no-install-recommends zfsutils-linux >/dev/null
fi

# -------------------------------------------------------------- incus daemon --
# Ubuntu ships incus.socket; make sure the daemon is up before talking to it.
systemctl enable --now incus.socket >/dev/null 2>&1 || true
systemctl enable --now incus >/dev/null 2>&1 || true

for _ in $(seq 1 30); do
  if incus info >/dev/null 2>&1; then break; fi
  sleep 1
done
incus info >/dev/null 2>&1 || die "the incus daemon is not answering; check: systemctl status incus"

# Printed only now: `incus version` against a daemon that is not up yet says
# "Server version: unreachable", which on a first run reads like a failure.
incus version | sed 's/^/    /'

add_to_group() {
  local user="${SUDO_USER:-}" group="incus"
  [[ -n "$user" && "$user" != "root" ]] || return 0
  if ! id -nG "$user" 2>/dev/null | grep -qw "$group"; then
    usermod -aG "$group" "$user"
    warn "added $user to the $group group — log out and back in for it to take effect"
  fi
}
add_to_group

# The driver of a pool, from the JSON API rather than the table columns: those
# letters are not stable across releases (`-c n,d` was the driver in 6.x and is
# the *description* in 7.x, where the driver is `n,D`), and a wrong letter here
# reads as "the pool has no driver" instead of failing.
storage_driver() {
  incus storage list --format=json 2>/dev/null \
    | jq -r --arg pool "$STORAGE_POOL" '.[] | select(.name == $pool) | .driver' \
      2>/dev/null | head -1
}

if incus storage list --format=csv -c n 2>/dev/null | grep -qx "$STORAGE_POOL"; then
  driver="$(storage_driver)"
  log "storage pool '$STORAGE_POOL' already exists ($driver)"
  if [[ "$driver" == "dir" ]]; then
    warn "pool '$STORAGE_POOL' uses the dir driver: clones are full copies, so"
    warn "provisioning and reset will be slow with a large class. See docs/operations.md"
  fi
else
  log "creating storage pool '$STORAGE_POOL' (driver: $STORAGE_DRIVER)"
  case "$STORAGE_DRIVER" in
    dir)
      incus storage create "$STORAGE_POOL" dir
      warn "dir driver chosen: correct but slow for many simultaneous clones."
      warn "For a real class use ZFS or btrfs: ONTRAK_STORAGE_DRIVER=zfs ONTRAK_STORAGE_SOURCE=/dev/nvme1n1 sudo -E infra/bootstrap-host.sh"
      ;;
    zfs|btrfs|lvm)
      [[ -n "$STORAGE_SOURCE" ]] || die "$STORAGE_DRIVER needs ONTRAK_STORAGE_SOURCE (a block device or file)"
      incus storage create "$STORAGE_POOL" "$STORAGE_DRIVER" source="$STORAGE_SOURCE"
      ;;
    *)
      die "unsupported ONTRAK_STORAGE_DRIVER '$STORAGE_DRIVER' (dir|btrfs|zfs|lvm)"
      ;;
  esac
fi

# ------------------------------------------------------------ lab network ----
if incus network list --format=csv -c n 2>/dev/null | grep -qx "$NETWORK"; then
  log "network '$NETWORK' already exists"
else
  log "creating managed bridge '$NETWORK' ($NET_CIDR, domain $DOMAIN)"
  incus network create "$NETWORK" \
    ipv4.address="$NET_CIDR" \
    ipv4.nat=true \
    ipv4.dhcp=true \
    ipv4.dhcp.ranges="$DHCP_RANGE" \
    ipv6.address=none \
    dns.domain="$DOMAIN" \
    dns.mode=managed
fi
# Keep the DNS domain applied even on re-runs: guests resolve <name>.ontrak.lab.
incus network set "$NETWORK" dns.domain="$DOMAIN"
incus network set "$NETWORK" ipv4.dhcp.ranges="$DHCP_RANGE"

# ----------------------------------------------------------------- project ---
if incus project list --format=csv -c n 2>/dev/null | grep -qx "$PROJECT"; then
  log "project '$PROJECT' already exists"
else
  log "creating project '$PROJECT'"
  incus project create "$PROJECT" \
    -c features.images=false \
    -c features.profiles=true \
    -c features.storage.volumes=true
fi

# The project gets its own default profile: without a root disk device and a NIC,
# nothing can be created in it.
if ! incus --project "$PROJECT" profile device get default root pool >/dev/null 2>&1; then
  log "configuring the project's default profile"
  incus --project "$PROJECT" profile device add default root disk path=/ pool="$STORAGE_POOL" size=48GiB
fi
if ! incus --project "$PROJECT" profile device get default eth0 network >/dev/null 2>&1; then
  incus --project "$PROJECT" profile device add default eth0 nic network="$NETWORK"
fi

if ! incus --project "$PROJECT" profile list --format=csv -c n | grep -qx "ontrak-student"; then
  log "creating the ontrak-student limits profile"
  incus --project "$PROJECT" profile create ontrak-student
fi
incus --project "$PROJECT" profile edit ontrak-student < "$PROJECT_ROOT/infra/incus/profile.yaml"

# ------------------------------------------------------------------ summary ---
DRIVER="$(storage_driver)"
BRIDGE_IP="$(incus network get "$NETWORK" ipv4.address)"
cat <<EOF

$(log "bootstrap complete")

  incus project   : $PROJECT
  storage pool    : $STORAGE_POOL ($DRIVER)
  lab bridge      : $NETWORK ($BRIDGE_IP, DNS domain $DOMAIN)
  profile         : ontrak-student (2 vCPU / 4 GiB — edit infra/incus/profile.yaml to taste)

Next steps:
  1. make venv && make doctor          # verify the control plane sees all of this
  2. make golden                       # build the golden Windows image (long)
  3. infra/lab-services.sh             # create the intranet targets the scenarios test against
  4. make templates                    # build tpl-<scenario> + clean snapshots
  5. make serve                        # sign-in is Authentik's — see docs/operations.md

Guests on $NETWORK can reach the internet through NAT but cannot reach this host's
management network. Keep the portal and Guacamole off this bridge (see docs/operations.md).
EOF
