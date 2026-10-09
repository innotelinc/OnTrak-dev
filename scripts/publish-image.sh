#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# OnTrak — build and publish the portal image.
#
#   make publish-image                          # build, tag, push
#   make publish-image PUSH=0                   # build and tag, push nothing
#   make publish-image VERSION=0.2.0            # override the version
#   make publish-image REGISTRY=ghcr.io/someone # publish somewhere else
#
# The local twin of `.github/workflows/publish.yml`, and deliberately the same
# shape: the same image, the same serving stage (`runtime`) and the same tags
# (`<version>`, `<major>.<minor>`, `latest`, `sha-<commit>`). CI publishes when a
# release is cut; this publishes when somebody has a tree they want an image of —
# which is what a range host that *pulls* rather than builds needs:
#
#   ONTRAK_IMAGE=ghcr.io/innotelinc/ontrak:0.1.0
#   ONTRAK_PULL_POLICY=missing     # the stack declares pull_policy: never by default
#
# **The version comes from the product's own source** (`ontrak/__init__.py`),
# never from an argument an operator has to remember: one place is what stops
# this script disagreeing with what the product reports about itself. `VERSION`
# overrides it for a rebuild that must not renumber anything.
#
# **The serving stage is named, not inherited.** The Dockerfile's `builder` stage
# resolves the dependencies and its `runtime` stage is the one that ships; tagging
# whichever `FROM` happens to be last is how a publish starts shipping a build
# tree after someone reorders the file.
#
# Publishing to GHCR needs `packages: write` on the repository, exactly as the
# workflow's token has. A by-hand push uses `gh auth token`, so one `gh` sign-in
# covers both directions.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REGISTRY="${REGISTRY:-ghcr.io/innotelinc}"
IMAGE="${IMAGE:-ontrak}"
PUSH="${PUSH:-1}"
VERSION_OVERRIDE="${VERSION:-}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" = "--help" || "${1:-}" = "-h" ]]; then
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker not found"

# The version lives in one place, and this is it.
version_of() {
  sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "${ROOT}/ontrak/__init__.py" | head -1
}

VERSION="${VERSION_OVERRIDE:-$(version_of)}"
[[ -n "$VERSION" ]] || die "could not read __version__ from ontrak/__init__.py (override with VERSION=x.y.z)"
MAJOR_MINOR="${VERSION%.*}"

# The commit tag is the full SHA, matching the workflow's `format=long`: it is the
# one tag that cannot be overwritten by a later build.
SHA="$(git rev-parse HEAD 2>/dev/null || true)"
if [[ -z "$SHA" ]]; then
  # A tree without git history can still be built, just not commit-tagged.
  warn "not a git checkout: publishing without the sha- tag"
  SHORT=""
else
  SHORT="${SHA:0:12}"
fi

REF="${REGISTRY}/${IMAGE}"
REFS=("${REF}:${VERSION}" "${REF}:${MAJOR_MINOR}" "${REF}:latest")
[[ -n "$SHORT" ]] && REFS+=("${REF}:sha-${SHORT}")

log "publishing ${REF} (version ${VERSION}, push=${PUSH}${SHA:+", commit ${SHORT}"})"

# ---------------------------------------------------------------------- push --
if [[ "$PUSH" = "1" ]]; then
  case "$REGISTRY" in
    ghcr.io/*|ghcr.io)
      if command -v gh >/dev/null 2>&1; then
        TOKEN="$(gh auth token 2>/dev/null || true)"
        USERNAME="$(gh api user --jq .login 2>/dev/null || true)"
        if [[ -n "$TOKEN" && -n "$USERNAME" ]]; then
          log "authenticating to ${REGISTRY%%/*} as $USERNAME"
          printf '%s' "$TOKEN" | docker login "${REGISTRY%%/*}" -u "$USERNAME" --password-stdin >/dev/null \
            || die "docker login to ${REGISTRY%%/*} failed — check the token's write:packages scope"
        else
          warn "no gh token: relying on an existing docker login for ${REGISTRY%%/*}"
        fi
      else
        warn "gh not installed: relying on an existing docker login for ${REGISTRY%%/*}"
      fi
      ;;
  esac
fi

# --------------------------------------------------------------------- build --
log "building the runtime stage"
docker build --target runtime -t "$REF:$VERSION" .
for ref in "${REFS[@]}"; do
  [[ "$ref" = "$REF:$VERSION" ]] || docker tag "$REF:$VERSION" "$ref"
done
log "tagged: ${VERSION}, ${MAJOR_MINOR}, latest${SHORT:+", sha-${SHORT}"}"

if [[ "$PUSH" != "1" ]]; then
  warn "PUSH=0: nothing was pushed"
fi

if [[ "$PUSH" = "1" ]]; then
  for ref in "${REFS[@]}"; do
    log "pushing $ref"
    docker push "$ref" || die "push failed for $ref — if this is an auth failure: gh auth token | docker login ${REGISTRY%%/*} -u <user> --password-stdin"
  done
fi

cat <<DONE

==> done
Run it on a deployment by pointing the stack at this image and letting it pull:

  ONTRAK_IMAGE=${REF}:${VERSION}
  ONTRAK_PULL_POLICY=missing

DONE
