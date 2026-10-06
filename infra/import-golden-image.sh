#!/usr/bin/env bash
#
# Publish a golden Windows image that was built on another host.
#
#   infra/import-golden-image.sh [export-directory]
#
# `infra/build-golden-image.sh` cannot run everywhere. A host whose KVM is nested
# on an AMD CPU cannot virtualise SMM, so the Windows installer dies before Setup
# starts (docs/operations.md, "Building the golden image on a nested host"). Build
# the image where that works -- bare metal, or a VM with nested virtualisation --
# and bring the result here.
#
# What to bring is the directory incus-windows writes its export to. Point at it
# with the argument, or leave the argument off and it looks where the build puts
# it:
#
#   <dir>/incus.tar.xz   image metadata
#   <dir>/disk.qcow2     the disk
#
# Environment:
#   ONTRAK_IMAGE_ALIAS     alias to publish under (default ontrak-win-base)
#   ONTRAK_INCUS_PROJECT   Incus project to publish into (default ontrak)
#   ONTRAK_SKIP_VERIFY     set to 1 to import without the disk check
#
# The disk is verified first (scripts/verify-golden-image.py). An image whose
# Windows apply was killed part-way publishes silently and then hangs every
# template built from it, which is a much worse afternoon than a failed import --
# so a disk without \EFI\Microsoft\Boot\bootmgfw.efi stops the import unless you
# really do want it (ONTRAK_SKIP_VERIFY=1).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALIAS="${ONTRAK_IMAGE_ALIAS:-ontrak-win-base}"
PROJECT="${ONTRAK_INCUS_PROJECT:-ontrak}"
PY="${PROJECT_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

command -v incus >/dev/null || die "incus not found; run infra/bootstrap-host.sh first"
[[ -n "$PY" ]] || die "python3 not found (needed to verify the disk before importing)"

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
for candidate in disk.qcow2 rootfs.img disk.raw; do
  [[ -f "$DIR/$candidate" ]] && { DISK="$DIR/$candidate"; break; }
done
[[ -n "$META" ]] || die "$DIR holds no incus.tar.xz (or lxd.tar.xz) — is this an image export?"
[[ -n "$DISK" ]] || die "$DIR holds no disk.qcow2 (or rootfs.img) — is this an image export?"

log "export: $DIR"
log "  metadata: $(basename "$META")"
log "  disk:     $(basename "$DISK") ($(du -h --apparent-size "$DISK" 2>/dev/null | cut -f1 || echo '?') apparent)"

# ---------------------------------------------------------------- verify first --
# The check reads the partition table and the ESP out of the file, so it needs no
# root and no loop device -- it can run before anything is published.
if [[ "${ONTRAK_SKIP_VERIFY:-}" =~ ^(1|true|yes|on)$ ]]; then
  warn "ONTRAK_SKIP_VERIFY set: importing without checking the disk is a finished install"
else
  log "checking the disk is a complete Windows install"
  if ! "$PY" "${PROJECT_ROOT}/scripts/verify-golden-image.py" "$DISK"; then
    die "refusing to import $DISK — it is not a complete Windows install. Re-run the build on the host that produced it, or set ONTRAK_SKIP_VERIFY=1 to import it anyway (templates built from it will not boot)."
  fi
fi

# ----------------------------------------------------------------- and publish --
if incus --project "$PROJECT" image info "$ALIAS" >/dev/null 2>&1; then
  warn "replacing the existing image alias $ALIAS in project $PROJECT"
  incus --project "$PROJECT" image delete "$ALIAS"
fi

log "importing into Incus (project $PROJECT, alias $ALIAS)"
incus --project "$PROJECT" image import "$META" "$DISK" --alias "$ALIAS"

# Adopt the image: clear the one property upstream stamps on everything it builds,
# and which nothing in this range can satisfy.
#
# incus-windows' tools/pack.sh publishes its build VM with
# `requirements.cdrom_agent=true`, `incus publish` copies image properties, and the
# property travels in the export's metadata.yaml as well. So every image built this
# way carries it, and importing *without* the flag — which a reader would reasonably
# expect to drop it — does not: the metadata declares it, so it is still there
# afterwards. It has to be cleared explicitly.
#
# What it means is "every instance made from this image must have an `agent:config`
# disk" (the config CD-ROM the Incus agent reads), and Incus enforces it at *start*,
# not at create. Nothing in `ontrak/` attaches one — OnTrak drives Windows over
# WinRM (`guest.driver`), which needs no agent config — so the failure lands late and
# reads strangely:
#
#     Error: This virtual machine image requires an agent:config disk be added
#
# which is what the first `ontrak template build` on a freshly imported image
# returned; every Windows template would have hit it. OnTrak's *own* golden build
# hits it too, in the build VM it clones to apply post-install.ps1 — see the same
# edit in infra/build-golden-image.sh.
#
# An operator who wants the opt-in `incus-exec` driver puts the requirement back and
# attaches the disk, which is all upstream was asking for:
#
#     incus --project <project> config device add <vm> incusagent disk source=agent:config
#     incus --project <project> image set-property ontrak-win-base requirements.cdrom_agent=true
#
# `incus image unset-property` is not the tool here: it panics with a nil pointer
# dereference in Incus 7.5.1. An empty value is how the property is removed.
incus --project "$PROJECT" image set-property "$ALIAS" requirements.cdrom_agent=""

incus --project "$PROJECT" image list "$ALIAS" --format csv -c l,d,s >/dev/null 2>&1 || true

cat <<EOF

$(log "golden image ready: $ALIAS (project $PROJECT)")

Next
  * Build the scenario templates:  make templates
  * Confirm the range is happy:    make check   (or: make exec ARGS=doctor)
EOF
