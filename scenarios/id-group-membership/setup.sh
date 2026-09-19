#!/usr/bin/env bash
# Starting state: a mover who still holds their old team's access, and a contractor whose
# agreement ended but whose account is still live.
#
# Both faults are *excess* access rather than a lockout, which is the more common — and
# the more dangerous — shape of identity drift.

set -uo pipefail

. "$(cd "$(dirname "$0")/../.." && pwd)/lib/ontrak-common.sh"

IDP_STATE=/var/lib/ontrak-idp/directory.json
IDP_PY="$(cd "$(dirname "$0")/../.." && pwd)/lib/idp.py"
IDP="python3 $IDP_PY --state $IDP_STATE"

if ! command -v python3 >/dev/null 2>&1; then
    # Debian's minimal container image ships without python3 (Ubuntu's has it), and
    # the directory service is a Python script — so the template build installs it
    # here, once, before the snapshot. Stdlib only; no pip.
    (apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 >/dev/null 2>&1) || true
fi
ontrak_require "python3 is available to run the directory service" command -v python3

mkdir -p /var/lib/ontrak-idp
rm -f "$IDP_STATE"
$IDP seed-preset team-move

# Make the drift explicit in the audit log, the way a real directory would have recorded
# the moves that caused it — so the student can see how the state came about.
$IDP --actor hr --reason "team change: warehouse -> finance (access not yet updated)" \
    add-member marco.silva finance-reporting
$IDP --actor hr --reason "contractor onboarded for warehouse cover" \
    add-member c.nguyen warehouse

if command -v setsid >/dev/null 2>&1; then
    setsid nohup $IDP serve --host 127.0.0.1 --port 8081 >/var/log/ontrak-idp.log 2>&1 &
else
    nohup $IDP serve --host 127.0.0.1 --port 8081 >/var/log/ontrak-idp.log 2>&1 &
fi

ontrak_step "marco.silva: $($IDP show-user marco.silva | tr '\n' ' ') (expected warehouse + finance-reporting)"
ontrak_step "c.nguyen status: $($IDP status c.nguyen) (expected active; contract ended 2026-08-31)"

ontrak_require "the mover still has their old team's access" $IDP in-group marco.silva warehouse
ontrak_require "the mover does not have the new team's access yet" \
    bash -c "! $IDP in-group marco.silva finance >/dev/null 2>&1"
ontrak_require "the mover keeps the group they still need" $IDP in-group marco.silva finance-reporting
ontrak_require "the contractor's account is still active" test "$($IDP status c.nguyen)" = "active"

ontrak_setup_ok "directory seeded at $IDP_STATE; access drift injected"
