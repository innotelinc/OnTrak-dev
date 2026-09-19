#!/usr/bin/env bash
# OnTrak — fill in the secrets a local stack needs, without ever inventing a
# value that is already there.
#
#   bash scripts/secrets.sh           # ./.env from ./.env.example, blanks filled
#   bash scripts/secrets.sh .env.ci   # any other file
#
# Rules:
#   * a key that is missing          → appended, generated
#   * a key that is present and empty → filled
#   * a key that already has a value  → left alone (a `vault://` reference counts
#     as a value: this script must never quietly replace one with a local secret)
#   * a key .env.example no longer lists → reported at the end, never touched: a
#     spent setting breaks every command that loads config, and the stack cannot
#     see it (compose reads the file as its own variables, not as settings)
#
# The generated values are local-development values. Production secrets come
# from Cerulean Vault, by reference (see .env.example).
set -euo pipefail

ENV_FILE="${1:-.env}"
EXAMPLE=".env.example"

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$EXAMPLE" ]; then
    cp "$EXAMPLE" "$ENV_FILE"
    echo "==> ${ENV_FILE} created from ${EXAMPLE}"
  else
    : >"$ENV_FILE"
    echo "==> ${ENV_FILE} created (no ${EXAMPLE} to copy)"
  fi
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "!! openssl not found: install it, or fill in the blanks in ${ENV_FILE} by hand" >&2
  exit 1
fi

# fill <KEY> <bytes> <note>
fill() {
  local key="$1" bytes="$2" note="${3:-}" value
  if grep -qE "^${key}=" "$ENV_FILE"; then
    if grep -qE "^${key}=[^[:space:]]" "$ENV_FILE"; then
      printf '   %-32s already set, left alone\n' "$key"
      return 0
    fi
    value="$(openssl rand -hex "$bytes")"
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
  else
    value="$(openssl rand -hex "$bytes")"
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
  printf '   %-32s generated%s\n' "$key" "${note:+ (${note})}"
}

echo "==> generating local secrets in ${ENV_FILE}"
# Signs portal session cookies. Rotating it logs every student out; nothing else
# breaks, which is why this one is safe to regenerate.
fill ONTRAK_PORTAL__SECRET 32
# Exactly 32 hex characters: Guacamole's JSON auth rejects anything else, and it
# must be identical in the portal and the gateway.
fill ONTRAK_GUAC__SECRET_KEY 16
# Local admin password inside the training machines (WinRM/SSH). Baked into the
# images at build time, so change it before you build them, not after.
fill ONTRAK_GUEST__PASSWORD 12

# ── stale keys ──────────────────────────────────────────────────────────────
# A key ${ENV_FILE} holds that ${EXAMPLE} no longer lists is usually one this
# checkout has stopped reading: the section was renamed, or a setting moved. That
# is worth saying out loud here, because the failure it causes is a bad one —
# `load_settings` rejects an unknown `ONTRAK_<SECTION>__<KEY>` outright, so a
# leftover line breaks *every* command that reads config (`make check`, `ontrak
# doctor`, the CLI) while the container stack stays green: compose reads its own
# variables and never parses the file as settings. An upgraded checkout then
# looks broken for a reason nothing points at.
#
# Report, never rewrite. The value may be an operator's own, it may be read by
# compose or the gateway rather than the app, and this script's contract is that
# it fills blanks and nothing else. A bare `ONTRAK_...` with no `__` is left out
# of the report for the same reason: it was never addressed to the app at all.
report_stale_keys() {
  [ -f "$EXAMPLE" ] || return 0
  local stale
  stale="$(comm -23 \
    <(grep -oE '^ONTRAK_[A-Z0-9_]*__[A-Z0-9_]+' "$ENV_FILE" | sort -u) \
    <(grep -oE '^ONTRAK_[A-Z0-9_]*__[A-Z0-9_]+' "$EXAMPLE" | sort -u) || true)"
  [ -n "$stale" ] || return 0

  echo
  echo "!! ${ENV_FILE} sets settings ${EXAMPLE} does not list:"
  while IFS= read -r key; do
    printf '   %-36s not in %s\n' "$key" "$EXAMPLE"
  done <<<"$stale"
  echo "   If it was renamed or removed, delete the line: an unknown"
  echo "   ONTRAK_<SECTION>__<KEY> makes every command that loads config fail."
}
report_stale_keys

echo
echo "==> done. ${ENV_FILE} is gitignored; never commit it."
echo "    next: make up      (portal on http://localhost:8080, console on :8080/guacamole/)"
