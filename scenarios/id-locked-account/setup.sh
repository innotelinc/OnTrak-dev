#!/usr/bin/env bash
# Starting state: an analyst's account locked by failed sign-ins, and a session from a
# laptop she no longer has.
#
# The directory service is the shared lab service from scenarios/_lib/idp.py, deployed
# here so the scenario is self-contained: any Linux container can host it, and grading
# never depends on a background daemon surviving.

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

# Seed the directory from scratch so re-running setup is deterministic.
rm -f "$IDP_STATE"
$IDP seed-preset finance-close

# The stale session: this is the part of the ticket students forget, and it is why the
# objective exists separately from the unlock.
$IDP add-session aisha.khan --id aisha.khan-laptop --device HOME-LAPTOP-01

# The fault: locked after repeated bad passwords. Recorded as a system action, because
# that is what a directory does — the ticket is not "who locked it".
$IDP --actor system lock aisha.khan --reason "lockout: 5 failed sign-ins"

# Best effort: run the HTTP API so `curl localhost:8081/users` works for the student.
# Grading uses the CLI and does not care whether this daemon is alive.
if command -v setsid >/dev/null 2>&1; then
    setsid nohup $IDP serve --host 127.0.0.1 --port 8081 >/var/log/ontrak-idp.log 2>&1 &
else
    nohup $IDP serve --host 127.0.0.1 --port 8081 >/var/log/ontrak-idp.log 2>&1 &
fi

ontrak_step "account status: $($IDP status aisha.khan) (expected locked)"
ontrak_step "stale session for aisha.khan: $($IDP has-active-session aisha.khan) (expected yes)"
ontrak_step "memberships: $($IDP show-user aisha.khan | tr '\n' ' ')"

ontrak_require "the directory has the seeded accounts" \
    test "$($IDP --json list-users | grep -c '"id"')" -ge 3
ontrak_require "the account is locked" test "$($IDP status aisha.khan)" = "locked"
ontrak_require "the account has a failed-attempt count to clear" \
    bash -c "python3 -c \"import json;d=json.load(open('$IDP_STATE'));u=[x for x in d['users'] if x['id']=='aisha.khan'][0];print(0 if u.get('failed_attempts',0)>=3 else 1)\""
ontrak_require "a stale session is live for the account" $IDP has-active-session aisha.khan
ontrak_require "the account still has its memberships" $IDP in-group aisha.khan finance

ontrak_setup_ok "directory seeded at $IDP_STATE; aisha.khan locked with a live laptop session"
