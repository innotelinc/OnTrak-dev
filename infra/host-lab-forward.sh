#!/usr/bin/env bash
#
# Let the lab's guests reach the network, when Docker shares the host.
#
#   sudo infra/host-lab-forward.sh            # apply now, and install the unit
#   sudo infra/host-lab-forward.sh --apply    # apply now, once
#   sudo infra/host-lab-forward.sh --check    # exit 0 if the rules are in place
#   sudo infra/host-lab-forward.sh --remove   # remove the rules and the unit
#
# The problem, because it does not look like a firewall when you meet it: a guest
# on the lab bridge resolves names perfectly and then cannot open a single
# connection. DNS works (it is UDP, answered by the host's own resolver), so the
# guest reads as "on the network", and everything built on top of it fails —
#
#     W: Failed to fetch http://deb.debian.org/... Cannot initiate the connection
#        ... - connect (101: Network is unreachable)
#     [ontrak] injection failed
#
# which sends you off to look at the guest's own routing, where nothing is wrong.
#
# What is wrong is on the host. Docker sets the **FORWARD policy to DROP** — that
# is how it keeps containers on different bridges from talking to each other — and
# it accepts only the traffic it knows about. A packet from a guest on `ontrak0`
# arrives on the bridge, is routed out of the host's uplink, and is dropped as
# "unrelated forwarded traffic" before it ever leaves. The lab works right up to
# the point where a guest needs the internet, which is how a template build that
# has to install a package fails inside a guest that reports itself healthy.
#
# So this accepts traffic to and from the lab bridge. Docker's own integration
# point for that is the `DOCKER-USER` chain, which it evaluates before its own
# rules and does not manage itself; on a host with no Docker the policy is usually
# ACCEPT already and there is nothing to do. Both directions are needed: `-i` for
# what the guest sends, `-o` for the replies coming back to it.
#
# On a WSL host the rules do not survive a restart by themselves, which is what
# `--install` is for. IPv4 only, matching the lab bridge (bootstrap creates it with
# `ipv6.address=none`).
#
# Environment:
#   ONTRAK_NETWORK   lab bridge name (default: ontrak0)

set -euo pipefail

UNIT_NAME=ontrak-lab-forward.service
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
BRIDGE="${ONTRAK_NETWORK:-ontrak0}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

forward_policy() {
  iptables -S FORWARD 2>/dev/null | sed -n 's/^-P FORWARD \(.*\)$/\1/p' | head -1
}

# The chain to put the rules in, or nothing when the host does not need them.
#
# Docker's DOCKER-USER is preferred: it is the chain Docker documents for exactly
# this, it is evaluated before Docker's own rules, and Docker does not rewrite it.
# A host without Docker keeps its own FORWARD rules, so when the policy is DROP
# there the rules go straight into FORWARD instead — position 1, ahead of whatever
# dropped the packet.
rules_chain() {
  if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    printf 'DOCKER-USER'
  elif [ "$(forward_policy)" = "DROP" ]; then
    printf 'FORWARD'
  fi
}

bridges_present() {
  ip link show "$BRIDGE" >/dev/null 2>&1
}

rule_present() {
  iptables -C "$1" "$2" "$BRIDGE" -j ACCEPT >/dev/null 2>&1
}

apply_rules() {
  local chain
  chain="$(rules_chain)"
  if [ -z "$chain" ]; then
    ok "nothing to do: no DOCKER-USER chain and FORWARD is $(forward_policy)"
    return 0
  fi
  local direction
  for direction in -i -o; do
    if rule_present "$chain" "$direction"; then
      ok "already accepted: ${direction} ${BRIDGE}"
    else
      iptables -I "$chain" 1 "$direction" "$BRIDGE" -j ACCEPT
      ok "guests can now be forwarded ${direction} ${BRIDGE} (via ${chain})"
    fi
  done
  bridges_present || warn "there is no '${BRIDGE}' interface yet — the rules are in place for when there is"
}

check_rules() {
  local chain missing=0
  chain="$(rules_chain)"
  if [ -z "$chain" ]; then
    ok "nothing needed: no DOCKER-USER chain and FORWARD is $(forward_policy)"
    return 0
  fi
  local direction
  for direction in -i -o; do
    if rule_present "$chain" "$direction"; then
      ok "present in ${chain}: ${direction} ${BRIDGE}"
    else
      warn "missing from ${chain}: ${direction} ${BRIDGE}"
      missing=1
    fi
  done
  return "$missing"
}

remove_rules() {
  # Both chains, because the one in use can change: Docker installed later moves
  # the rules, and a leftover ACCEPT in FORWARD would outlive --remove.
  local chain direction
  for chain in DOCKER-USER FORWARD; do
    iptables -n -L "$chain" >/dev/null 2>&1 || continue
    for direction in -i -o; do
      while rule_present "$chain" "$direction"; do
        iptables -D "$chain" "$direction" "$BRIDGE" -j ACCEPT
        ok "removed from ${chain}: ${direction} ${BRIDGE}"
      done
    done
  done
}

install_unit() {
  command -v systemctl >/dev/null 2>&1 || { warn "no systemd — the rules are applied but will not survive a restart"; return 0; }
  if [ ! -d /run/systemd/system ]; then
    warn "systemd is not running this boot — applied now, but this host will forget at restart"
    return 0
  fi
  cat >"$UNIT_PATH" <<EOF
# Written by infra/host-lab-forward.sh. It accepts forwarded traffic to and from
# the lab bridge (${BRIDGE}), which Docker's FORWARD policy would otherwise drop —
# guests there have DNS and no way out. Idempotent, so it is safe on every boot.
[Unit]
Description=OnTrak: let the lab's guests be forwarded
Documentation=https://github.com/Innotel/OnTrak (infra/host-lab-forward.sh)
# After docker.service because Docker creates DOCKER-USER and this prefers that
# chain; ordering against a unit that is not installed is harmless.
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${SELF} --apply
ExecStop=${SELF} --unapply

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$UNIT_PATH"
  systemctl daemon-reload
  systemctl enable --now "$UNIT_NAME" >/dev/null 2>&1 \
    || systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || true
  ok "installed and enabled ${UNIT_NAME} (re-applies on every boot)"
}

remove_unit() {
  if [ -f "$UNIT_PATH" ]; then
    systemctl disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    ok "removed ${UNIT_NAME}"
  fi
}

[[ $EUID -eq 0 ]] || die "run this with sudo (it changes the host's firewall)"
command -v iptables >/dev/null 2>&1 || die "iptables not found"

case "${1:---install}" in
  --apply | --unapply)
    # --unapply exists so the unit's ExecStop can undo it. Both spellings apply or
    # remove the rules; --unapply is the one systemd calls on stop.
    if [ "$1" = "--unapply" ]; then remove_rules; else apply_rules; fi
    ;;
  --check)
    check_rules
    ;;
  --remove)
    remove_rules
    remove_unit
    ;;
  --install)
    apply_rules
    install_unit
    ;;
  -h | --help)
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "unknown option '$1' (see --help)"
    ;;
esac
