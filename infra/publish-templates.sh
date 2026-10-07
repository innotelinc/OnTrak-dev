#!/usr/bin/env bash
#
# Publish built scenario templates to a registry, so a host that cannot build them
# can still run them.
#
#   infra/publish-templates.sh                        # every built template
#   infra/publish-templates.sh tpl-net-dns-failure     # named templates
#
# A scenario template is an Incus instance named ``tpl-<scenario>[-<workload>]``
# carrying a ``clean`` snapshot: the guest booted once, the fault injected, the
# guest powered off. Building one means booting the platform once per scenario —
# minutes on a host with KVM, and much longer where a Windows guest has to be
# emulated — so a classroom of hosts builds them on one machine and pulls the rest.
#
# What goes into the registry is the instance's *backup* (``incus export``), not an
# image (``incus publish``). That is the only Incus artifact that carries the
# instance as built: a template built from a catalog workload image and one built
# from the golden Windows image are both of them instances, and the extra hardware
# a scenario asks for (``hw-driver-device``'s second NIC, for instance) lives on the
# instance and in no image. ``infra/import-templates.sh`` restores it here.
#
# ``--instance-only`` is deliberate. The ``clean`` snapshot is the same disk as the
# instance it was taken from — the guest is not booted in between — so exporting the
# snapshot as well would store every template twice. The importing host takes the
# snapshot again, where it is a cheap copy-on-write snapshot of the restored disk.
#
# Environment:
#   ONTRAK_TEMPLATE_REPOSITORY  target repository
#                               (default: ghcr.io/innotelinc/ontrak-template)
#   ONTRAK_TEMPLATE_TAG_PREFIX  prefix for each tag (default: empty — the tag is
#                               the instance name, so `oras pull ...:tpl-<scenario>`)
#   ONTRAK_INCUS_PROJECT        Incus project the templates live in (default: ontrak)
#   ONTRAK_TEMPLATE_SNAPSHOT    the snapshot a template must carry (default: clean)
#   ONTRAK_TEMPLATE_WORKDIR     where the export is written. Default: a directory on
#                               the project's own storage pool, because a Windows
#                               template is a 12 GiB disk and the host's root
#                               filesystem is usually the smallest thing it has.
#   ONTRAK_TEMPLATE_KEEP        set to 1 to keep the exported tarballs after the push
#   ONTRAK_GOLDEN_TOKEN         registry token to log in with, as for the golden
#                               image. Default: `gh auth token`.
#   ONTRAK_GOLDEN_USER          username for that token. Default: `gh api user`.
#   ONTRAK_ORAS                 oras binary to use (default: `oras` on PATH)
#
# A published template is a snapshot of the fault at the moment it was built, so it
# pins the scenario as much as the media: re-run this after editing a scenario, and
# give the run a new tag if an older one has to stay pullable.

set -euo pipefail

REPOSITORY="${ONTRAK_TEMPLATE_REPOSITORY:-ghcr.io/innotelinc/ontrak-template}"
TAG_PREFIX="${ONTRAK_TEMPLATE_TAG_PREFIX:-}"
PROJECT="${ONTRAK_INCUS_PROJECT:-ontrak}"
SNAPSHOT="${ONTRAK_TEMPLATE_SNAPSHOT:-clean}"
ORAS="${ONTRAK_ORAS:-oras}"

# The artifact's shape. These media types are this project's own, so a pull can
# tell a template backup from a golden image without unpacking either.
ARTIFACT_TYPE="application/vnd.ontrak.template.v1"
LAYER_TYPE="application/vnd.ontrak.template.backup.v1.tar.gz"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

command -v incus >/dev/null || die "incus not found; run infra/bootstrap-host.sh first"
command -v "$ORAS" >/dev/null || die "oras not found — set ONTRAK_ORAS to its path, or install it (https://oras.land)"

# Pin a path-shaped oras to an absolute one before anything changes directory: the
# push runs from inside the export directory, and a relative ONTRAK_ORAS would then
# resolve against that directory instead. This is the same trap the golden publish
# documents, and scripts/tests/test_publish_templates.py drives it with a stub.
if [[ "$ORAS" == */* ]]; then
  ORAS="$(cd "$(dirname "$ORAS")" && pwd)/$(basename "$ORAS")"
fi

# ------------------------------------------------------------------ templates --
# Named templates are used as given; with no arguments, everything `incus list`
# reports under this project whose name looks like a template is published. A name
# that is not there at all is an error rather than a skip, because a typo otherwise
# reads exactly like "nothing to do".
mapfile -t FOUND < <(incus --project "$PROJECT" list --format=csv -c n 2>/dev/null | grep '^tpl-' | sort)
if [[ $# -gt 0 ]]; then
  NAMES=("$@")
else
  NAMES=("${FOUND[@]}")
fi
[[ ${#NAMES[@]} -gt 0 ]] || die "no templates found in project $PROJECT; build them first (ontrak template build --all)"

for name in "${NAMES[@]}"; do
  incus --project "$PROJECT" info "$name" >/dev/null 2>&1 \
    || die "no instance named $name in project $PROJECT"
  incus --project "$PROJECT" snapshot list "$name" --format=csv -c n 2>/dev/null | grep -qx "$SNAPSHOT" \
    || die "$name has no '$SNAPSHOT' snapshot — it is not a built template. Run \`ontrak template build\` for it first."
done

# ------------------------------------------------------------------- workdir ----
dir_is_big_enough() {
  local free
  free="$(df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}')" || return 1
  [[ -n "$free" && "$free" =~ ^[0-9]+$ && "$free" -ge 16777216 ]]  # 16 GiB, a Windows template and room
}

WORKDIR="${ONTRAK_TEMPLATE_WORKDIR:-}"
if [[ -z "$WORKDIR" ]]; then
  # Default to the project's own storage pool: a Windows template is a 12 GiB disk,
  # and on a range host the pool is the large filesystem while / is not.
  pool="$(incus --project "$PROJECT" profile device get default root pool 2>/dev/null || true)"
  if [[ -n "$pool" && -d "/var/lib/incus/storage-pools/$pool" ]]; then
    WORKDIR="/var/lib/incus/storage-pools/$pool/ontrak-template-export"
  else
    WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ontrak-template-export.XXXXXX")"
  fi
fi
mkdir -p "$WORKDIR"
dir_is_big_enough "$WORKDIR" || warn "$WORKDIR looks small for a Windows template (12 GiB each); set ONTRAK_TEMPLATE_WORKDIR to a roomier filesystem"

# -------------------------------------------------------------------- log in ----
TOKEN="${ONTRAK_GOLDEN_TOKEN:-}"
USERNAME="${ONTRAK_GOLDEN_USER:-}"
if command -v gh >/dev/null 2>&1; then
  [[ -z "$TOKEN" ]] && TOKEN="$(gh auth token 2>/dev/null || true)"
  [[ -z "$USERNAME" ]] && USERNAME="$(gh api user --jq .login 2>/dev/null || true)"
fi
if [[ -n "$TOKEN" ]]; then
  # -u is not optional: without it oras stops on a "Username:" prompt, which in a
  # script is a hang rather than an error.
  [[ -n "$USERNAME" ]] || USERNAME="token"
  registry="${REPOSITORY%%/*}"
  log "authenticating to $registry as $USERNAME"
  printf '%s' "$TOKEN" | "$ORAS" login "$registry" -u "$USERNAME" --password-stdin >/dev/null \
    || die "oras login to $registry failed — check the token's write:packages scope"
fi

# -------------------------------------------------------------------- publish ---
log "publishing ${#NAMES[@]} template(s) to $REPOSITORY"
published=0
for name in "${NAMES[@]}"; do
  tag="${TAG_PREFIX}${name}"
  tarball="$WORKDIR/$name.tar.gz"
  rm -f "$tarball"

  status="$(incus --project "$PROJECT" list "$name" --format=csv -c s 2>/dev/null | head -1)"
  [[ "$status" == "RUNNING" ]] && warn "$name is RUNNING — exporting it snapshot-consistent, but a template is meant to be idle"

  log "$name: exporting (this is the whole disk: expect minutes, not seconds)"
  incus --project "$PROJECT" export "$name" "$tarball" --instance-only --compression gzip --force

  size="$(du -h "$tarball" | cut -f1)"
  log "$name: pushing $size as $REPOSITORY:$tag"
  # Published from inside the export directory, by file name: oras refuses an
  # absolute layer path, and the name it is given is what `oras pull` writes on the
  # other side, which has to be `<name>.tar.gz` and not a path from this host.
  (
    cd "$WORKDIR"
    "$ORAS" push "$REPOSITORY:$tag" \
      --artifact-type "$ARTIFACT_TYPE" \
      -a "org.opencontainers.image.source=https://github.com/innotelinc/OnTrak-dev" \
      -a "org.opencontainers.image.title=OnTrak scenario template $name" \
      -a "org.opencontainers.image.description=$name — a booted scenario template (fault injected, 'clean' snapshot). Pull, then: infra/import-templates.sh $name" \
      "$name.tar.gz:$LAYER_TYPE"
  )

  if [[ "${ONTRAK_TEMPLATE_KEEP:-}" =~ ^(1|true|yes|on)$ ]]; then
    log "$name: kept $tarball (ONTRAK_TEMPLATE_KEEP)"
  else
    rm -f "$tarball"
  fi
  published=$((published + 1))
done

cat <<EOF

$(log "published $published template(s) to $REPOSITORY")

Another host fetches them with

  oras login ${REPOSITORY%%/*} -u <user>          # the package is private by default
  infra/import-templates.sh                       # every tag in the repository
  infra/import-templates.sh tpl-net-dns-failure   # or one at a time

Notes
  * A GHCR package starts private. Its visibility is a setting on the package page;
    leave it private for templates built on media from a Windows evaluation ISO.
  * The importing host re-takes the 'clean' snapshot, and points each Windows guest
    at its own accelerator (infra/qemu-accel.sh), because which accelerator a guest
    runs on is a property of the host, not of the scenario.
EOF
