#!/usr/bin/env bash
# Pull a published golden image, so this host can import one it cannot build.
#
#   infra/pull-golden-image.sh                     # the newest published tag
#   infra/pull-golden-image.sh win11e-2026-10-06    # a named one
#   infra/pull-golden-image.sh --list               # what there is to pull
#
# ``infra/publish-golden-image.sh`` puts the image in a registry. This is the other
# end of that, and it exists because the other end was missing: templates have had a
# puller of their own for as long as they could be moved at all
# (``infra/import-templates.sh``), while the golden image had a documented two-step in
# docs/operations.md -- ``oras login``, ``oras pull`` -- that every host had to retype
# from a page. That is how an export directory ends up holding yesterday's
# ``disk.qcow2``: the pull is the step people improvise, and the import then verifies
# the *disk it finds*, which passes, because the disk is a complete Windows install.
# It is simply not the one that was just fetched. So a destination that already has
# anything in it is refused rather than written over.
#
# This pulls and stops. The disk check and the import are ``make golden-import``,
# which verifies the disk before it publishes anything, and is the step worth
# keeping separate: one pulls from a network, the other changes what this range
# clones every template and session from.
#
# The tag defaults to the newest ``win11e-*`` one rather than to today's date, which
# is what the publish side defaults to. A host set up on any other day would ask for
# a tag that was never pushed, and the failure reads like a credential problem
# (``unauthorized``) rather than like a wrong guess. ``--list`` prints what the
# repository actually holds, oldest first in the order the registry returns them.
#
# Environment:
#   ONTRAK_GOLDEN_REPOSITORY  source repository
#                             (default: ghcr.io/innotelinc/ontrak-golden)
#   ONTRAK_GOLDEN_TAG         tag to pull. Default: the newest ``win11e-*`` tag
#   ONTRAK_GOLDEN_PULL_DIR    where the export lands (default: ./golden-export)
#   ONTRAK_GOLDEN_FORCE       set to 1 to pull into a destination that is not empty
#   ONTRAK_GOLDEN_TOKEN       registry token to log in with. Default: `gh auth
#                             token`, which needs `gh` and a signed-in account.
#   ONTRAK_GOLDEN_USER        username for that token. Default: `gh api user`,
#                             falling back to "token" -- GHCR reads the PAT, not
#                             the name. Leave both unset to use an existing
#                             `oras login`.
#   ONTRAK_ORAS               oras binary to use (default: `oras` on PATH)
#
# A new GHCR package is **private** by default, which is the right default for media
# built from a Windows evaluation ISO: the pull then needs a token, and without one
# `oras` answers `unauthorized` for a tag that is plainly there.

set -euo pipefail

# No PROJECT_ROOT here, unlike its siblings: this script needs no path from the
# checkout — it pulls into a directory, and the import that reads the project's
# verifier is the next step, not this one. One assigned and never used would be an
# unused-variable warning (SC2034), and CI runs shellcheck at warning severity.
REPOSITORY="${ONTRAK_GOLDEN_REPOSITORY:-ghcr.io/innotelinc/ontrak-golden}"
TAG="${ONTRAK_GOLDEN_TAG:-}"
DIR="${ONTRAK_GOLDEN_PULL_DIR:-golden-export}"
ORAS="${ONTRAK_ORAS:-oras}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

LIST=0
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --list)    LIST=1 ;;
  -*)        die "unknown option: $1 (try --help)" ;;
  "")        : ;;
  *)         TAG="$1" ;;
esac

command -v "$ORAS" >/dev/null || die "oras not found — set ONTRAK_ORAS to its path, or install it (https://oras.land)"

# ------------------------------------------------------------------- log in ----
# Only when there is a token to log in *with*: an operator who has already run
# `oras login` should not be asked for one again, and a token in the environment is
# what a CI job has. Same fallbacks as the publish side, so one machine needs one
# `gh` sign-in for both directions.
log_in() {
  TOKEN="${ONTRAK_GOLDEN_TOKEN:-}"
  USERNAME="${ONTRAK_GOLDEN_USER:-}"
  if command -v gh >/dev/null 2>&1; then
    [[ -z "$TOKEN" ]] && TOKEN="$(gh auth token 2>/dev/null || true)"
    [[ -z "$USERNAME" ]] && USERNAME="$(gh api user --jq .login 2>/dev/null || true)"
  fi
  if [[ -n "$TOKEN" ]]; then
    [[ -n "$USERNAME" ]] || USERNAME="token"
    registry="${REPOSITORY%%/*}"
    log "authenticating to $registry as $USERNAME"
    printf '%s' "$TOKEN" | "$ORAS" login "$registry" -u "$USERNAME" --password-stdin >/dev/null \
      || die "oras login to $registry failed — check the token's read:packages scope"
  fi
}
log_in

# ------------------------------------------------------------------- the tag ----
# With no tag named, ask the repository what it holds rather than assuming today's
# date: `oras repo tags` is how a fresh host learns what there is to pull.
if [[ "$LIST" = 1 || -z "$TAG" ]]; then
  log "asking $REPOSITORY what it holds"
  mapfile -t TAGS < <("$ORAS" repo tags "$REPOSITORY" 2>/dev/null || true)
  if [[ ${#TAGS[@]} -eq 0 ]]; then
    die "no tags in $REPOSITORY (or it is unreachable) — if the package is private, check that the token can read it (read:packages)"
  fi
  if [[ "$LIST" = 1 ]]; then
    printf '%s\n' "${TAGS[@]}"
    exit 0
  fi
  # The published tags are dated (`win11e-<YYYY-MM-DD>`), so the greatest in plain
  # string order is the most recent build. Anything else in the repository is not a
  # golden image and is ignored rather than guessed at.
  NEWEST=""
  for tag in "${TAGS[@]}"; do
    case "$tag" in
      win11e-*)
        # An `if`, not `[[ ... ]] && NEWEST=...`: the tags this skips are the normal
        # case on a repository that holds anything else, and a list whose last
        # command failed is a script under `set -e` deciding whether to stop.
        if [[ -z "$NEWEST" || "$tag" > "$NEWEST" ]]; then
          NEWEST="$tag"
        fi
        ;;
    esac
  done
  [[ -n "$NEWEST" ]] || die "$REPOSITORY holds no win11e-* tag — name the tag to pull, or run --list to see what is there"
  TAG="$NEWEST"
  log "newest published tag: $TAG"
fi

# ------------------------------------------------------------- the destination ---
mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
if [[ -n "$(ls -A "$DIR" 2>/dev/null)" ]]; then
  if [[ "${ONTRAK_GOLDEN_FORCE:-}" =~ ^(1|true|yes|on)$ ]]; then
    warn "$DIR is not empty — pulling over what is there (ONTRAK_GOLDEN_FORCE)"
  else
    die "$DIR is not empty. An export that mixes last week's layers with this pull's is not something the import can catch: it verifies the disk it finds, and an older disk.qcow2 is a complete Windows install too. Pull into an empty directory (ONTRAK_GOLDEN_PULL_DIR=...), or set ONTRAK_GOLDEN_FORCE=1 to overwrite what is there."
  fi
fi

log "target: $REPOSITORY:$TAG"
log "destination: $DIR"

# ------------------------------------------------------------------- the pull ----
log "pulling (this is the whole disk: expect minutes, not seconds)"
"$ORAS" pull "$REPOSITORY:$TAG" -o "$DIR" \
  || die "oras pull of $REPOSITORY:$TAG failed — see the note above if it says unauthorized"

# A pull that returned success but wrote half the artifact is the case the import
# cannot see: it wants the metadata layer *and* the disk, and it will take one of
# them from an older pull if that is what is lying in the directory. Check both
# landed, by the names the publish side pushes.
META=""
for candidate in incus.tar.xz lxd.tar.xz; do
  [[ -f "$DIR/$candidate" ]] && { META="$candidate"; break; }
done
DISK=""
for candidate in disk.qcow2 rootfs.img; do
  [[ -f "$DIR/$candidate" ]] && { DISK="$candidate"; break; }
done
if [[ -z "$META" || -z "$DISK" ]]; then
  die "$DIR is not a complete export after the pull: it holds no ${META:-incus.tar.xz} or no ${DISK:-disk.qcow2}. The published artifact is two layers (metadata plus disk); check that the tag names a golden image, then pull again into an empty directory."
fi

log "export: $DIR"
log "  metadata: $META"
log "  disk:     $DISK ($(du -h --apparent-size "$DIR/$DISK" 2>/dev/null | cut -f1 || echo '?') apparent)"

cat <<EOF

$(log "pulled $REPOSITORY:$TAG")

Next
  * Import it, which verifies the disk first:  make golden-import ARGS=$DIR
  * Then build the scenario templates:         make templates
  * And confirm the range is happy:            make check

Notes
  * \`make golden-import\` reads the disk to check it is a finished install, and that
    check converts it to a raw copy inside TMPDIR — about 17 GiB for the 32 GiB
    Windows image. If /tmp is a tmpfs, point TMPDIR at a filesystem with room first.
  * \`make templates\` is still required on this host: templates are snapshots built
    where they will run, not files that can be shipped.
EOF
