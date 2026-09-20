#!/usr/bin/env bash
#
# Build the golden Windows image that every scenario template is cloned from.
#
#   infra/build-golden-image.sh
#
# This is the long, unattended part (30-60 minutes). It glues together:
#
#   1. antifob/incus-windows  — downloads a Microsoft evaluation ISO and produces
#      an Incus image with VirtIO drivers, WinRM and the Incus agent already in it
#   2. infra/windows/post-install.ps1 — the training account, RDP, power settings,
#      Defender exclusions, agent start-up
#   3. incus publish — the result becomes the ontrak-win-base image
#
# Environment:
#   ONTRAK_WINDOWS_TARGET   incus-windows build target (default 11e = Windows 11
#                          Enterprise evaluation). Run `sh build.sh` with no
#                          arguments in the checkout to list the targets your
#                          pinned version supports (10e, 11e, 2019, 2022, ...).
#   ONTRAK_INCUS_WINDOWS_REF  git ref of incus-windows to use. PIN THIS: it is a
#                          third-party build tool and its interface changes.
#   ONTRAK_VIRTIO_VERSION   override the virtio-win version (0.1.285-1 or newer is
#                          needed for the vsock driver that incus-exec uses)
#   ONTRAK_IMAGE_ALIAS      published alias (default ontrak-win-base)
#   ONTRAK_GOLDEN_CPUS      vCPU for the build VM (default 2)
#   ONTRAK_GOLDEN_MEMORY    RAM for the build VM (default 6GB)
#   ONTRAK_GOLDEN_DISK      disk for the build VM, and so the published image
#                           (default 32GiB)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/build"
CHECKOUT="${BUILD_DIR}/incus-windows"
TARGET="${ONTRAK_WINDOWS_TARGET:-11e}"
REF="${ONTRAK_INCUS_WINDOWS_REF:-main}"
IMAGE_ALIAS="${ONTRAK_IMAGE_ALIAS:-ontrak-win-base}"
PROJECT="${ONTRAK_INCUS_PROJECT:-ontrak}"
NETWORK="${ONTRAK_NETWORK:-ontrak0}"
BUILD_VM="${ONTRAK_BUILD_VM:-ontrak-golden-build}"
PY="${PROJECT_ROOT}/.venv/bin/python"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "${ONTRAK_GUEST_PASSWORD:-}" ]] || die "set ONTRAK_GUEST__PASSWORD (the training account password) first"
command -v incus >/dev/null || die "incus not found; run infra/bootstrap-host.sh first"
command -v xorriso >/dev/null || die "xorriso is required to repack the Windows ISO"
command -v genisoimage >/dev/null || die "genisoimage is required by incus for the agent config ISO"
[[ -x "$PY" ]] || die "python venv missing; run 'make venv' first"

if [[ "$REF" == "main" ]]; then
  warn "ONTRAK_INCUS_WINDOWS_REF is 'main'. Pin a tag or commit for reproducible builds:"
  warn "  ONTRAK_INCUS_WINDOWS_REF=<tag-or-sha> infra/build-golden-image.sh"
fi

# ------------------------------------------------------------------ checkout --
mkdir -p "$BUILD_DIR"
if [[ -d "$CHECKOUT/.git" ]]; then
  log "updating incus-windows checkout"
  git -C "$CHECKOUT" fetch --tags --quiet
  git -C "$CHECKOUT" checkout --quiet "$REF"
else
  log "cloning antifob/incus-windows (${REF})"
  git clone --quiet https://github.com/antifob/incus-windows.git "$CHECKOUT"
  git -C "$CHECKOUT" checkout --quiet "$REF"
fi

# ------------------------------------------------------------ build VM sizing --
# The builder sizes its own build VM at 4 vCPU / 8 GB (tools/pack.sh). On a range
# host with four cores that is *every* core it has, and Windows Setup uses them: the
# host keeps answering ping and accepting TCP while nothing in user space is
# scheduled, so sshd and the portal never reply and the lab is down for the length
# of the build. That is not a guess — it is what this script did to a 4 vCPU host,
# which was unreachable for hours with the build still running. The sizing is
# rewritten here so the host stays usable. Raise it where there are cores to spare.
BUILD_CPUS="${ONTRAK_GOLDEN_CPUS:-2}"
BUILD_MEMORY="${ONTRAK_GOLDEN_MEMORY:-6GB}"
if sed -i -E "s/-c limits\.cpu=[0-9]+ -c limits\.memory=[0-9]+G?B?/-c limits.cpu=${BUILD_CPUS} -c limits.memory=${BUILD_MEMORY}/" "$CHECKOUT/tools/pack.sh" \
   && grep -q -- "-c limits.cpu=${BUILD_CPUS} -c limits.memory=${BUILD_MEMORY}" "$CHECKOUT/tools/pack.sh"; then
  log "build VM sized at ${BUILD_CPUS} vCPU / ${BUILD_MEMORY} (ONTRAK_GOLDEN_CPUS / ONTRAK_GOLDEN_MEMORY)"
else
  warn "could not size the build VM: tools/pack.sh no longer matches (it is a pinned third-party checkout, so this is a change to re-read) — it will take the host's defaults"
fi

# ------------------------------------------------- build disk write cache --
# Incus hands a VM disk to qemu with the "writeback" cache by default, so every
# block the guest writes is charged to the host page cache *and* to the
# container's memory cgroup. Applying the ~7 GiB Windows image that way is what
# made the first three builds fail: the cgroup reached its limit and the kernel
# OOM-killed qemu in the middle of the apply.
#
# The failure then hides itself. tools/click.py (a pinned third-party tool) waits
# for `incus ls` to report STOPPED and treats that as "the installer finished" —
# it cannot tell a clean sysprep shutdown from a killed process — so pack.sh goes
# on to publish and export the half-applied disk as if it were a golden image.
# A qemu kill is therefore silent: you get an image whose ESP has no Windows boot
# files, and every Windows template built from it fails much later.
#
# Bypassing the host write cache for the build disk removes the memory pressure
# that caused the kill, which is the fix that actually matters. (ONTRAK_GOLDEN_CPUS
# / ONTRAK_GOLDEN_MEMORY above bound the guest's own RAM for the same reason.)
if sed -i '/^incus config device set "${name}" root io.bus=virtio-blk$/a incus config device set "${name}" root io.cache=none' "$CHECKOUT/tools/pack.sh" \
   && grep -q 'root io.cache=none' "$CHECKOUT/tools/pack.sh"; then
  log "build disk set to io.cache=none (keeps the Windows image apply out of the host page cache)"
else
  warn "could not set io.cache=none on the build disk: tools/pack.sh no longer matches — on a 16 GiB range host, expect the OOM kill described above"
fi

# ---------------------------------------------------- build disk (and image) ----
# tools/pack.sh creates its build VM at 30 GiB and then grows the disk to 60 GiB
# before the install. That size is not just the build VM's: `incus publish` tars
# the *apparent* disk into the image — it does not skip holes, and on this host it
# is not even gzip's input size that matters, because a Linux guest's partition
# table is the whole disk by construction.
#
# What that costs was measured, not guessed. Publishing a 60 GiB build disk with
# --compression none wrote for fourteen minutes and then failed with "Failed to
# begin transaction: context deadline exceeded" — the image store and Incus's own
# cowsql database share one dataset, and the copy starves the database's leader
# election. The daemon's DB is left unusable, every `incus` command times out, and
# the fully-installed Windows disk has to be recovered by hand. It happened twice.
#
# A Windows 11 install needs ~20 GiB, so 32 GiB is plenty of headroom and the
# published image is half the size. Raise it only if a scenario's media needs it.
BUILD_DISK="${ONTRAK_GOLDEN_DISK:-32GiB}"
if sed -i -E "/^\s*incus config device set .*root size=[0-9]+GiB/s/root size=[0-9]+GiB/root size=${BUILD_DISK}/" "$CHECKOUT/tools/pack.sh" \
   && grep -q -- "root size=${BUILD_DISK}" "$CHECKOUT/tools/pack.sh"; then
  log "build disk sized at ${BUILD_DISK} (ONTRAK_GOLDEN_DISK) — this is also the published image's disk"
else
  warn "could not size the build disk: tools/pack.sh no longer matches — expect a 60 GiB image, a long publish, and possibly a wedged Incus database"
fi

# ------------------------------------------------------------------- build ----
log "building the Windows ${TARGET} image (this takes 30-60 minutes)"
pushd "$CHECKOUT" >/dev/null
# virtio-win >= 0.1.285-1 ships the viosock driver that Incus 6.22+ needs for the
# Windows agent (guest.driver=incus-exec). Harmless if you stay on WinRM.
if [[ -n "${ONTRAK_VIRTIO_VERSION:-}" ]]; then
  export VIRTIO_VERSION="$ONTRAK_VIRTIO_VERSION"
  [[ -n "${ONTRAK_VIRTIO_SHA256:-}" ]] && export VIRTIO_SHA256="$ONTRAK_VIRTIO_SHA256"
fi
sh build.sh "$TARGET"

# Two things about incus-windows' import step are worth knowing, because both of
# them made this script fail *after* a 30-60 minute build:
#
#   * build.sh writes its image to ./output/win<target> (OUTDIR=${OUTDIR:-
#     ./output/win${VERSION}}), not ./output/<target>. tools/import.sh takes that
#     directory as its argument, so passing ./output/${TARGET} pointed at a path
#     that never exists.
#   * tools/import.sh runs a bare `incus image import`, which means the default
#     project. The templates this image exists for live in $PROJECT, and an image
#     alias is project-scoped, so "incus --project $PROJECT init <alias>" could not
#     see it. Import into the project the templates are built in, using the same
#     requirements flag upstream uses.
IMPORT_ALIAS="win${TARGET}"
OUTDIR="./output/${IMPORT_ALIAS}"
[[ -f "$OUTDIR/incus.tar.xz" ]] || OUTDIR="./output/${TARGET}"
[[ -f "$OUTDIR/incus.tar.xz" && -f "$OUTDIR/disk.qcow2" ]] \
  || die "the build produced no image: expected incus.tar.xz and disk.qcow2 under ./output/${IMPORT_ALIAS} (a stale directory there makes build.sh bail out early -- remove it and retry)"
log "importing the built image into Incus (project $PROJECT)"
incus --project "$PROJECT" image import "$OUTDIR/incus.tar.xz" "$OUTDIR/disk.qcow2" \
  requirements.cdrom_agent=true --alias "$IMPORT_ALIAS"
popd >/dev/null

# The imported image keeps whatever alias incus-windows chose; find the newest one
# that looks like a Windows image and work with it explicitly.
mapfile -t CANDIDATES < <(incus --project "$PROJECT" image list --format=csv -c L,f 2>/dev/null | grep -i -E 'win' | awk -F, '{print $1}' | tail -n 5)
[[ ${#CANDIDATES[@]} -gt 0 ]] || die "no Windows image found after import; check the build output above"
SOURCE_IMAGE="${ONTRAK_SOURCE_IMAGE:-${CANDIDATES[${#CANDIDATES[@]}-1]}}"
log "using imported image: $SOURCE_IMAGE"

# ------------------------------------------------------- customise and publish -
if incus --project "$PROJECT" info "$BUILD_VM" >/dev/null 2>&1; then
  warn "removing the leftover build VM $BUILD_VM"
  incus --project "$PROJECT" delete "$BUILD_VM" --force
fi

log "creating the build VM from $SOURCE_IMAGE"
incus --project "$PROJECT" init "$SOURCE_IMAGE" "$BUILD_VM" -p default -p ontrak-student
incus --project "$PROJECT" config device add "$BUILD_VM" eth0 nic network="$NETWORK" 2>/dev/null || true
incus --project "$PROJECT" start "$BUILD_VM"

log "waiting for an address"
IP=""
for _ in $(seq 1 60); do
  IP="$(incus --project "$PROJECT" list "$BUILD_VM" --format=csv -c 4 | cut -d' ' -f1)"
  [[ -n "$IP" ]] && break
  sleep 5
done
[[ -n "$IP" ]] || { incus --project "$PROJECT" list "$BUILD_VM"; die "the build VM never got an address"; }
log "build VM address: $IP"

log "applying post-install.ps1 over WinRM"
"$PY" "$PROJECT_ROOT/infra/windows/apply-postinstall.py" "$BUILD_VM" "$IP" \
  || die "post-install failed; the VM '$BUILD_VM' is left running so you can inspect it"

log "shutting the build VM down cleanly"
# --force as a fallback: a failed clean shutdown must not block publishing, and a
# published image from a hard power-off is still usable for the next clone.
incus --project "$PROJECT" stop "$BUILD_VM" --timeout 120 || incus --project "$PROJECT" stop "$BUILD_VM" --force

if incus --project "$PROJECT" image info "$IMAGE_ALIAS" >/dev/null 2>&1; then
  warn "replacing the existing image alias $IMAGE_ALIAS"
  incus --project "$PROJECT" image delete "$IMAGE_ALIAS"
fi

log "publishing as $IMAGE_ALIAS"
incus --project "$PROJECT" publish "$BUILD_VM" --alias "$IMAGE_ALIAS"
incus --project "$PROJECT" delete "$BUILD_VM" --force

cat <<EOF

$(log "golden image ready: $IMAGE_ALIAS")

Next: infra/build-templates.sh   (boot each scenario, inject its fault, snapshot "clean")

Notes
  * Evaluation media expires (90 days for Windows 11 Enterprise eval, 180 for
    Server). Rebuild before a course, or switch to volume licensing for permanent
    infrastructure.
  * Clones share the same machine SID because the image is not sysprep'd. That is
    fine for standalone training VMs and breaks domain joins, which is why
    scenarios never assume a domain.
  * To rebuild only the customisation step against an existing image:
      ONTRAK_SOURCE_IMAGE=<image> ONTRAK_IMAGE_ALIAS=ontrak-win-base infra/build-golden-image.sh
    (it will still rebuild the ISO unless you remove the checkout's output/)
EOF
