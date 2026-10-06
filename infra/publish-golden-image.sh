#!/usr/bin/env bash
#
# Publish a golden image to a registry, so another host can pull it.
#
#   infra/publish-golden-image.sh [export-directory]
#
# `infra/import-golden-image.sh` brings a golden image in from another host's
# filesystem. This is the same move over a network: the image goes to a registry
# once, and every range that cannot build it — a host whose KVM is nested on an AMD
# CPU, a classroom of thin hosts, a colleague's laptop — pulls it instead. See
# docs/operations.md, "Publishing the golden image to a registry".
#
# What to publish is the split export the build writes (`incus.tar.xz` plus
# `disk.qcow2`), which is also what `make golden-import` consumes, so a pulled image
# needs no conversion. Point at the directory, or leave the argument off and it
# looks where the build puts it.
#
# The disk is verified before it is published (`scripts/verify-golden-image.py`), for
# the same reason the import verifies it: a half-applied image loads and runs
# perfectly well everywhere it is stored, and then hangs every template built from
# it. Publishing one to a registry hands that to everyone at once.
#
# Environment:
#   ONTRAK_GOLDEN_REPOSITORY   target repository
#                              (default: ghcr.io/innotelinc/ontrak-golden)
#   ONTRAK_GOLDEN_TAG          tag (default: win11e-<YYYY-MM-DD>)
#   ONTRAK_GOLDEN_TOKEN        registry token to log in with. Default: `gh auth
#                              token`, which needs `gh` and a signed-in account.
#   ONTRAK_GOLDEN_USER         username for that token. Default: `gh api user`,
#                              falling back to "token" — GHCR reads the PAT, not
#                              the name. Leave both unset to use an existing
#                              `oras login`.
#   ONTRAK_ORAS                oras binary to use (default: `oras` on PATH)
#   ONTRAK_SKIP_VERIFY         set to 1 to publish without checking the disk
#
# A new GHCR package is **private** by default, which is the right default for
# media built from a Windows evaluation ISO: a pull then needs a token. Make it
# public in the package settings only if that is a deliberate call.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PROJECT_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
REPOSITORY="${ONTRAK_GOLDEN_REPOSITORY:-ghcr.io/innotelinc/ontrak-golden}"
TAG="${ONTRAK_GOLDEN_TAG:-win11e-$(date +%F)}"
ORAS="${ONTRAK_ORAS:-oras}"

# The artifact's shape. These media types are this project's own, so a pull can
# tell the two layers apart without unpacking them.
ARTIFACT_TYPE="application/vnd.ontrak.golden-image.v1"
META_TYPE="application/vnd.ontrak.golden-image.metadata.v1.tar.xz"
DISK_TYPE="application/vnd.ontrak.golden-image.disk.v1.qcow2"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$PY" ]] || die "python3 not found (needed to verify the disk)"
command -v "$ORAS" >/dev/null || die "oras not found — set ONTRAK_ORAS to its path, or install it (https://oras.land)"

# ------------------------------------------------------------------ the export --
DIR="${1:-}"
if [[ -z "$DIR" ]]; then
  for candidate in \
      "${PROJECT_ROOT}/build/incus-windows/output/win11e" \
      "${PROJECT_ROOT}/build/incus-windows/output/11e" \
      "${PROJECT_ROOT}/build/incus-windows/output" \
      "${PROJECT_ROOT}/build/golden-image"; do
    if [[ -d "$candidate" ]]; then DIR="$candidate"; break; fi
  done
fi
[[ -n "$DIR" ]] || die "no export directory given and none found; pass the directory the build exported to"
[[ -d "$DIR" ]] || die "$DIR is not a directory"
DIR="$(cd "$DIR" && pwd)"

META=""
for candidate in incus.tar.xz lxd.tar.xz; do
  [[ -f "$DIR/$candidate" ]] && { META="$DIR/$candidate"; break; }
done
DISK=""
for candidate in disk.qcow2 rootfs.img; do
  [[ -f "$DIR/$candidate" ]] && { DISK="$DIR/$candidate"; break; }
done
[[ -n "$META" ]] || die "$DIR holds no incus.tar.xz — is this a split image export? (a single <fingerprint>.tar has to be split first; see docs/operations.md, 'Recovering a build VM that was kept')"
[[ -n "$DISK" ]] || die "$DIR holds no disk.qcow2 — is this a split image export?"

log "export: $DIR"
log "  metadata: $(basename "$META")"
log "  disk:     $(basename "$DISK") ($(du -h --apparent-size "$DISK" 2>/dev/null | cut -f1 || echo '?') apparent)"
log "target: $REPOSITORY:$TAG"

# ---------------------------------------------------------------- verify first --
if [[ "${ONTRAK_SKIP_VERIFY:-}" =~ ^(1|true|yes|on)$ ]]; then
  warn "ONTRAK_SKIP_VERIFY set: publishing without checking the disk is a finished install"
else
  log "checking the disk is a complete Windows install"
  if ! "$PY" "${PROJECT_ROOT}/scripts/verify-golden-image.py" "$DISK"; then
    die "refusing to publish $DISK — it is not a complete Windows install, and every host that pulled it would build templates that hang at the firmware boot prompt. Re-run the build, or set ONTRAK_SKIP_VERIFY=1 to publish it anyway."
  fi
fi

# ------------------------------------------------------------------- log in ----
# Only when there is a token to log in *with*: an operator who has already run
# `oras login` should not be asked for one again, and a token in the environment is
# what a CI job has.
TOKEN="${ONTRAK_GOLDEN_TOKEN:-}"
USERNAME="${ONTRAK_GOLDEN_USER:-}"
if command -v gh >/dev/null 2>&1; then
  [[ -z "$TOKEN" ]] && TOKEN="$(gh auth token 2>/dev/null || true)"
  [[ -z "$USERNAME" ]] && USERNAME="$(gh api user --jq .login 2>/dev/null || true)"
fi
if [[ -n "$TOKEN" ]]; then
  # -u is not optional: without it oras stops on a "Username:" prompt, which in a
  # script is a hang rather than an error. GHCR authenticates on the token, so a
  # name is only needed to fill the field.
  [[ -n "$USERNAME" ]] || USERNAME="token"
  registry="${REPOSITORY%%/*}"
  log "authenticating to $registry as $USERNAME"
  printf '%s' "$TOKEN" | "$ORAS" login "$registry" -u "$USERNAME" --password-stdin >/dev/null \
    || die "oras login to $registry failed — check the token's write:packages scope"
fi

# -------------------------------------------------------------------- publish --
# The tag is not `latest` on purpose: a golden image is pinned to the Windows media
# and the training account baked into it, so an operator pulling one wants to know
# which. The annotations are what `oras discover` and the package page show.
log "publishing (this is the whole disk: expect minutes, not seconds)"
# Published from inside the export directory, by file name, for two reasons. oras
# refuses an absolute path outright (it reads one as a path-traversal attempt), and
# the name it is given becomes the layer's org.opencontainers.image.title, which is
# the name `oras pull` writes on the other side — publishing /srv/build/disk.qcow2
# would have every puller recreate that directory. `oras push` also takes the layers
# in the order given, and the metadata layer is first so that a pull lists it first.
(
  cd "$DIR"
  "$ORAS" push "$REPOSITORY:$TAG" \
    --artifact-type "$ARTIFACT_TYPE" \
    -a "org.opencontainers.image.source=https://github.com/innotelinc/OnTrak-dev" \
    -a "org.opencontainers.image.title=OnTrak golden Windows 11 Enterprise image" \
    -a "org.opencontainers.image.description=Windows 11 Enterprise (11e) golden image for OnTrak scenario templates. Pull, then: make golden-import ARGS=<dir>" \
    "$(basename "$META"):$META_TYPE" \
    "$(basename "$DISK"):$DISK_TYPE"
)

cat <<EOF

$(log "published $REPOSITORY:$TAG")

Another host fetches it with

  oras login ${REPOSITORY%%/*} -u <user>       # the package is private by default
  oras pull $REPOSITORY:$TAG -o ./golden-export
  make golden-import ARGS=./golden-export      # verifies the disk again, then imports

Notes
  * A GHCR package starts private. Its visibility is a setting on the package page,
    and it is worth leaving private for media built from a Windows evaluation ISO.
  * \`make templates\` on the pulling host is still required: templates are snapshots
    built on the host that will run them, not files that can be shipped.
  * Re-publishing an existing tag overwrites it. Give a rebuild its own tag — the
    date default does that for rebuilds on different days, and ONTRAK_GOLDEN_TAG
    does it for several on the same one.
EOF
