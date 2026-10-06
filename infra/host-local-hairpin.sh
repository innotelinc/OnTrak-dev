#!/usr/bin/env bash
#
# Make this host able to reach the ports it publishes itself.
#
#   sudo infra/host-local-hairpin.sh              # apply now, and install the unit
#   sudo infra/host-local-hairpin.sh --apply      # apply now, once
#   sudo infra/host-local-hairpin.sh --check      # exit 0 if the rules are in place
#   sudo infra/host-local-hairpin.sh --remove     # remove the rules and the unit
#
# The problem this fixes, because it does not look like a networking problem when
# you meet it: the stack publishes its one port on 0.0.0.0, so "it is on 8080"
# reads as "anything can reach 8080". Docker implements that 0.0.0.0 with a DNAT
# rule, and the rule lives in the *nat OUTPUT* chain, which is the chain that
# handles connections **from this host to this host**. So:
#
#   * a browser on another machine           -> PREROUTING -> DNAT -> the container   ✓
#   * a container on this host, to the LAN IP -> PREROUTING -> DNAT -> the container  ✓
#   * this host, to its own LAN address       -> OUTPUT     -> DNAT -> rewritten to
#     the container's address, from a source address that is not the one the socket
#     was opened on, and the reply never matches. The connection hangs until it
#     times out; nginx logs it as a 499 and every other log says nothing happened.
#
# Adding a RETURN for the host's own addresses before that DNAT puts the packet
# back on the ordinary local path, where the published port has a listener of its
# own (docker-proxy) waiting for it. Nothing about how other machines reach the
# range changes — only the host talking to itself.
#
# This is the difference between `ontrak doctor`, `curl` and a student's browser
# agreeing about whether the range is up. On a WSL host the rules do not survive
# a restart by themselves, which is what `--install` is for.
#
# IPv4 only: that is where Docker's DNAT rule lives on these hosts, and it is
# what the range answers on.

set -euo pipefail

UNIT_NAME=ontrak-local-hairpin.service
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# Every IPv4 address this machine answers on, from anywhere but itself. `scope
# global` is what leaves out loopback and link-local; the ranges and the Docker
# bridge gateways are both "global" here, and both want the rule — the bridge
# addresses are local addresses too, and the RETURN is a no-op for traffic that
# no published port was going to answer anyway.
host_addresses() {
  ip -4 -o addr show up scope global 2>/dev/null \
    | awk '{ split($4, parts, "/"); print parts[1] }' \
    | sort -u
}

rule_present() {
  iptables -t nat -C OUTPUT -d "$1" -j RETURN >/dev/null 2>&1
}

apply_rules() {
  local address found=0
  while read -r address; do
    [ -n "$address" ] || continue
    found=1
    if rule_present "$address"; then
      ok "already reachable from this host: ${address}"
    else
      # Position 1: last, so it is ahead of the DOCKER jump that would otherwise
      # rewrite the packet first.
      iptables -t nat -I OUTPUT 1 -d "$address" -j RETURN
      ok "this host can now reach ${address}:* directly"
    fi
  done < <(host_addresses)
  [ "$found" -eq 1 ] || warn "no global IPv4 address found — nothing to do"
}

check_rules() {
  local address missing=0
  while read -r address; do
    [ -n "$address" ] || continue
    if rule_present "$address"; then
      ok "present: ${address}"
    else
      warn "missing: ${address}"
      missing=1
    fi
  done < <(host_addresses)
  return "$missing"
}

remove_rules() {
  local address
  while read -r address; do
    [ -n "$address" ] || continue
    while rule_present "$address"; do
      iptables -t nat -D OUTPUT -d "$address" -j RETURN
      ok "removed the rule for ${address}"
    done
  done < <(host_addresses)
}

install_unit() {
  command -v systemctl >/dev/null 2>&1 || { warn "no systemd — the rules are applied but will not survive a restart"; return 0; }
  if [ ! -d /run/systemd/system ]; then
    warn "systemd is not running this boot — applied now, but this host will forget at restart"
    return 0
  fi
  cat >"$UNIT_PATH" <<EOF
# Written by infra/host-local-hairpin.sh. The four lines below it are the whole
# unit: it re-applies the nat OUTPUT RETURNs for this host's own addresses, which
# Docker's DNAT would otherwise rewrite before a published port's own listener
# ever sees them. Idempotent, so it is safe on every boot.
[Unit]
Description=OnTrak: let this host reach the ports it publishes
Documentation=https://github.com/Innotel/OnTrak (infra/host-local-hairpin.sh)
After=network-online.target
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
  # --now as well as enable: the rules are already applied above, but a unit that
  # is enabled and inactive reports as though it were not doing anything.
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
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "unknown option '$1' (see --help)"
    ;;
esac
