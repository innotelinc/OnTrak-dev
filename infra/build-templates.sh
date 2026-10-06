#!/usr/bin/env bash
#
# Build a template for every scenario: boot the golden image, let setup.ps1 inject
# the fault, verify the fault was applied, then power off and snapshot as "clean".
#
#   infra/build-templates.sh                 # all scenarios
#   infra/build-templates.sh net-dns-failure # one scenario
#
# Re-run after editing a scenario. Use --force (below or via the CLI) to rebuild a
# template whose snapshot already exists.

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRAINLAB="${PROJECT_ROOT}/.venv/bin/ontrak"

[[ -x "$TRAINLAB" ]] || { echo "run 'make setup' first" >&2; exit 1; }

if [[ $# -gt 0 ]]; then
  exec "$TRAINLAB" template build "$@"
fi

echo "==> validating every scenario first"
"$TRAINLAB" scenario validate

echo "==> building templates (each one boots Windows once)"
"$TRAINLAB" template build --all

echo
echo "Templates are idle (powered off) between classes; only pool VMs consume RAM."
echo "Inspect with:  .venv/bin/ontrak pool status"
