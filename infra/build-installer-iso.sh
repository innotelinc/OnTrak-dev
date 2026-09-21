#!/usr/bin/env bash
#
# Build the OnTrak range-host installer ISO.
#
#   infra/build-installer-iso.sh
#
# The result installs Ubuntu Server 24.04 LTS on a bare machine, then turns it
# into a range host on first boot: Incus, the ontrak0 lab bridge, the OnTrak
# checkout and the portal stack. The operator answers exactly one screen —
# identity (username, hostname, password, SSH key) — and nothing is baked in.
#
# It is a remaster of the official Ubuntu Server live ISO:
#
#   * /nocloud/{user-data,meta-data}   the autoinstall (see autoinstall/)
#   * /ontrak/…                        the first-boot payload (see firstboot/)
#   * the boot entries get `autoinstall ds=nocloud;s=/cdrom/nocloud/`
#
# xorriso is used to extract and repack, and to recreate the original boot
# equipment (BIOS El Torito, UEFI, isohybrid MBR/GPT) so the image boots from a
# USB stick, a DVD and virtual media alike. If xorriso is not installed on this
# machine, it runs from a container instead — nothing is installed globally.
#
# Environment:
#   ONTRAK_UBUNTU_RELEASE  base release under releases.ubuntu.com/24.04
#                          (default: 24.04.5)
#   ONTRAK_BASE_ISO        use an ISO already on disk instead of downloading
#   ONTRAK_ISO_OUT         output path (default: dist/ontrak-installer-<rel>-amd64.iso)
#   ONTRAK_ISO_CACHE       download and work directory
#                          (default: ${XDG_CACHE_HOME:-$HOME/.cache}/ontrak-installer)
#   ONTRAK_INSTALLER_USERNAME / ONTRAK_INSTALLER_HOSTNAME
#                          defaults for the identity screen (default: ontrak /
#                          ontrak-range)
#   ONTRAK_INSTALLER_PASSWORD
#                          default password, instead of a random one
#   ONTRAK_ISO_SMOKE=1     also boot the ISO in QEMU and check the installer starts
#                          (ONTRAK_ISO_SMOKE_SECONDS, default 600, is how long it
#                          watches the serial console for)
#
# The generated default password is printed at the end and written next to the
# ISO (…-creds.txt); the identity screen lets the operator choose their own, so
# treat it as a fallback, not a secret to keep.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER_DIR="$PROJECT_ROOT/infra/installer"

RELEASE="${ONTRAK_UBUNTU_RELEASE:-24.04.5}"
BASE_NAME="ubuntu-${RELEASE}-live-server-amd64.iso"
BASE_URL="https://releases.ubuntu.com/24.04/${BASE_NAME}"
CACHE="${ONTRAK_ISO_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/ontrak-installer}"
WORK="$CACHE/work"
EXTRACT="$WORK/extract"
OUT="${ONTRAK_ISO_OUT:-$PROJECT_ROOT/dist/ontrak-installer-${RELEASE}-amd64.iso}"
CREDS="${OUT%.iso}-creds.txt"

USERNAME="${ONTRAK_INSTALLER_USERNAME:-ontrak}"
HOSTNAME_DEFAULT="${ONTRAK_INSTALLER_HOSTNAME:-ontrak-range}"
VOLID="OnTrak ${RELEASE} amd64"

BUILDER_IMAGE="ontrak-iso-builder:24.04"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m--- %s\033[0m\n' "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required ($2)"; }

mkdir -p "$CACHE" "$WORK" "$(dirname "$OUT")"

# --------------------------------------------------------------- one build --
# Two builds must not share a work tree. The second one starts with `rm -rf` and
# re-extracts, so the first repacks a tree with holes in it: xorriso says "File …
# can't be opened. Filling with 0s" for whichever files it has not rewritten yet,
# and the result is an image that boots, installs, and cannot fetch its own pool.
# That is a real way to lose an hour, and it is not hypothetical: a launch script
# that looked failed (a flaky host, a dropped connection) was retried while the
# first build was still running, and the only sign of it was a corrupt image.
# An exclusive lock turns that into a clear message for the second run.
LOCK="$CACHE/.build.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || die "another build is already using $CACHE.
    Wait for it, or build somewhere else: ONTRAK_ISO_CACHE=/some/other/dir"
fi

# ------------------------------------------------------------------ xorriso --
# xorriso is the one tool that has to be recent: Ubuntu's own ISO is a
# BIOS+UEFI+isohybrid image, and `-report_el_torito as_mkisofs` is what makes the
# repack keep all three ways of booting. Rather than require it on the operator's
# machine (or install it globally), fall back to a throwaway container.
XORRISO_MODE="host"
if command -v xorriso >/dev/null 2>&1; then
  log "using the host's xorriso ($(xorriso --version 2>&1 | head -1 | awk '{print $2}'))"
else
  need docker "used to run xorriso without installing it on this machine"
  XORRISO_MODE="docker"
  if ! docker image inspect "$BUILDER_IMAGE" >/dev/null 2>&1; then
    log "building the xorriso container image ($BUILDER_IMAGE)"
    docker build -q -t "$BUILDER_IMAGE" - <<'DOCKERFILE' >/dev/null
FROM ubuntu:24.04
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends xorriso \
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
  fi
  log "using xorriso from the $BUILDER_IMAGE container"
fi

# The cache is mounted at the *identical* path inside the container, so every
# path xorriso reports, and every path we hand it, means the same thing on both
# sides — and `-report_el_torito` output can be replayed verbatim.
xorriso_run() {
  if [[ "$XORRISO_MODE" == "docker" ]]; then
    docker run --rm -v "$CACHE:$CACHE" "$BUILDER_IMAGE" xorriso "$@"
  else
    xorriso "$@"
  fi
}

# ------------------------------------------------------------------- base ISO -
step "base image: Ubuntu $RELEASE"
BASE_ISO="${ONTRAK_BASE_ISO:-$CACHE/$BASE_NAME}"
if [[ -n "${ONTRAK_BASE_ISO:-}" && ! -f "$BASE_ISO" ]]; then
  die "ONTRAK_BASE_ISO=$ONTRAK_BASE_ISO does not exist"
fi

if [[ -f "$BASE_ISO" ]]; then
  log "already downloaded: $BASE_ISO"
else
  need curl "downloads the base image"
  log "downloading $BASE_URL"
  curl -fL --retry 3 --retry-delay 5 --progress-bar -o "$BASE_ISO.part" "$BASE_URL"
  mv "$BASE_ISO.part" "$BASE_ISO"
fi

# Verify the download against Canonical's own checksums: a truncated ISO produces
# an unreadable image, and that failure is far cheaper to catch here.
if command -v sha256sum >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  if curl -fsSL -o "$CACHE/SHA256SUMS" "https://releases.ubuntu.com/24.04/SHA256SUMS"; then
    EXPECTED="$(awk -v n="*$BASE_NAME" '$2 == n {print $1}' "$CACHE/SHA256SUMS")"
    if [[ -n "$EXPECTED" ]]; then
      ACTUAL="$(sha256sum "$BASE_ISO" | awk '{print $1}')"
      if [[ "$EXPECTED" == "$ACTUAL" ]]; then
        log "sha256 verified"
      else
        die "sha256 mismatch for $BASE_ISO
    expected $EXPECTED
    actual   $ACTUAL
    Delete the file and re-run to download it again."
      fi
    else
      warn "no checksum listed for $BASE_NAME; skipping verification"
    fi
  else
    warn "could not fetch SHA256SUMS; skipping verification"
  fi
fi

# --------------------------------------------------------------- credentials -
step "identity defaults"
need openssl "generates the default password hash"
if [[ -n "${ONTRAK_INSTALLER_PASSWORD:-}" ]]; then
  DEFAULT_PASSWORD="$ONTRAK_INSTALLER_PASSWORD"
  PASSWORD_NOTE="from ONTRAK_INSTALLER_PASSWORD"
else
  DEFAULT_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  PASSWORD_NOTE="generated for this build"
fi
PASSWORD_HASH="$(openssl passwd -6 -salt "$(openssl rand -hex 8)" "$DEFAULT_PASSWORD")"
log "identity defaults: $USERNAME@$HOSTNAME_DEFAULT  (password ${PASSWORD_NOTE})"

# ------------------------------------------------------------------ render ---
step "assembling the ISO tree"
rm -rf "$EXTRACT"
mkdir -p "$EXTRACT"

log "extracting $BASE_ISO"
xorriso_run -osirrox on -indev "$BASE_ISO" -extract / "$EXTRACT" >/dev/null
chmod -R u+w "$EXTRACT"

# autoinstall, the way the nocloud datasource expects it.
mkdir -p "$EXTRACT/nocloud"
cp "$INSTALLER_DIR/autoinstall/meta-data" "$EXTRACT/nocloud/meta-data"

need python3 "renders the autoinstall template"
python3 - "$INSTALLER_DIR/autoinstall/user-data.dist" "$EXTRACT/nocloud/user-data" \
  "$USERNAME" "$HOSTNAME_DEFAULT" "$PASSWORD_HASH" "$RELEASE" <<'PY'
import pathlib, sys
template, out, username, hostname, pw_hash, release = sys.argv[1:7]
text = pathlib.Path(template).read_text()
for token, value in (("@USERNAME@", username), ("@HOSTNAME@", hostname),
                     ("@PASSWORD_HASH@", pw_hash), ("@RELEASE@", release)):
    text = text.replace(token, value)
left = [t for t in ("@USERNAME@", "@HOSTNAME@", "@PASSWORD_HASH@", "@RELEASE@") if t in text]
if left:
    sys.exit(f"unsubstituted tokens in {template}: {left}")
pathlib.Path(out).write_text(text)
PY
log "wrote /nocloud/user-data and /nocloud/meta-data"

# The first-boot payload, straight from the repository so the ISO and the repo
# cannot drift apart.
mkdir -p "$EXTRACT/ontrak"
cp "$INSTALLER_DIR/firstboot/ontrak-firstboot.sh"        "$EXTRACT/ontrak/firstboot.sh"
cp "$INSTALLER_DIR/firstboot/ontrak-firstboot.service"   "$EXTRACT/ontrak/ontrak-firstboot.service"
cp "$INSTALLER_DIR/firstboot/firstboot.env.example"      "$EXTRACT/ontrak/firstboot.env.example"
cp "$INSTALLER_DIR/README.txt"                           "$EXTRACT/ontrak/README.txt"
chmod 0755 "$EXTRACT/nocloud" "$EXTRACT/ontrak" "$EXTRACT/ontrak/firstboot.sh"
log "wrote /ontrak (first-boot payload)"

# ------------------------------------------------------------------- boot -----
step "pointing the boot entries at the autoinstall"
need python3 "patches the boot menu"
GRUB_FILES="$(cd "$EXTRACT" && find . -name 'grub.cfg' | sort)"
[[ -n "$GRUB_FILES" ]] || die "no grub.cfg in the extracted ISO — is $BASE_ISO really the Ubuntu Server live image?"
PATCHED=0
while IFS= read -r rel; do
  f="$EXTRACT/${rel#./}"
  if grep -q '/casper/vmlinuz' "$f"; then
    python3 - "$f" <<'PY'
import re, sys

path = sys.argv[1]
# console=ttyS0 as well as the default VGA console: a headless machine with a
# serial console can then be installed and watched over it, and the smoke test
# below has something to read.
ARGS = "autoinstall ds=nocloud;s=/cdrom/nocloud/ console=ttyS0"
text = open(path).read()
added = [0]


def patch(m):
    # Kernel arguments go before the `---` that separates subiquity's own ones.
    added[0] += 1
    return f"{m.group(1)} {ARGS}"


# The live-server menu uses `linux` (BIOS and UEFI); `linuxefi` on older media.
# Both the standard and the HWE kernel entries are patched, so either menu choice
# installs unattended. (`linux16 /boot/memtest…` has no /casper path, so it is
# left alone.)
text = re.sub(r"^(\s*linux(?:efi)?\s+/casper/[^\s]*vmlinuz)\b", patch, text, flags=re.M)
open(path, "w").write(text)
if not added[0]:
    sys.exit(f"{path}: no /casper/vmlinuz boot line found")
print(f"    patched {added[0]} boot entr{'y' if added[0] == 1 else 'ies'} in {path}")
PY
    PATCHED=$((PATCHED + 1))
  fi
done <<< "$GRUB_FILES"
[[ $PATCHED -gt 0 ]] || die "no boot entries were patched"
log "$PATCHED boot configuration(s) patched"

# Keep a copy of what we shipped, so a finished ISO can be accounted for.
cp "$EXTRACT/nocloud/user-data" "$WORK/user-data.rendered"
chmod 0600 "$WORK/user-data.rendered"

# ------------------------------------------------------------------- repack ---
step "repacking (BIOS + UEFI + isohybrid)"
MKISOFS_ARGS_FILE="$WORK/mkisofs.args"
log "asking xorriso to describe the original boot equipment"
xorriso_run -indev "$BASE_ISO" -report_el_torito as_mkisofs >"$MKISOFS_ARGS_FILE"
[[ -s "$MKISOFS_ARGS_FILE" ]] || die "xorriso could not describe $BASE_ISO's boot equipment"

# Replay those arguments against the modified tree. The report is shell-quoted
# (some lines carry two options, e.g. `--grub2-mbr --interval:…:'…iso'`), so it is
# split with shlex rather than by whitespace, and handed over NUL-separated to
# keep any spaces in a path intact.
python3 - "$MKISOFS_ARGS_FILE" >"$WORK/mkisofs.argv" <<'PY'
import shlex, sys

text = open(sys.argv[1]).read()
args = shlex.split(text)
if not args:
    sys.exit(f"no xorriso arguments in {sys.argv[1]}")
sys.stdout.write("\0".join(args))
PY
mapfile -d '' -t ARGS <"$WORK/mkisofs.argv"
# -V is included by the report; add it only if it is missing, since mkisofs
# refuses a duplicate volume id.
if ! printf '%s\0' "${ARGS[@]}" | grep -qz '^-V$'; then
  ARGS+=(-V "$VOLID")
fi
log "xorriso arguments: ${#ARGS[@]} entries"

BUILT="$WORK/ontrak-installer.iso"
rm -f "$BUILT"
xorriso_run -as mkisofs "${ARGS[@]}" -o "$BUILT" "$EXTRACT" >/dev/null

# ------------------------------------------------------------------- verify ---
step "verifying the image"
[[ -s "$BUILT" ]] || die "the repack produced no image"
SIZE_H="$(du -h "$BUILT" | awk '{print $1}')"
log "built $BUILT ($SIZE_H)"

# 1. the payload is really in there — read it back out of the finished image, so
#    this tests the artefact and not the tree it was made from.
rm -rf "$WORK/verify"
mkdir -p "$WORK/verify"
for f in nocloud/user-data nocloud/meta-data \
         ontrak/firstboot.sh ontrak/ontrak-firstboot.service \
         ontrak/firstboot.env.example ontrak/README.txt; do
  out="$WORK/verify/$(basename "$f")"
  xorriso_run -osirrox on -indev "$BUILT" -extract "/$f" "$out" >/dev/null 2>&1 || true
  [[ -s "$out" ]] || die "the built ISO has no readable /$f"
done
log "payload present: the autoinstall and the first-boot unit came back out of the image"

# The autoinstall on the image must be the one rendered from the template — not
# an older copy, and not with a token left in it.
diff -q "$WORK/user-data.rendered" "$WORK/verify/user-data" >/dev/null \
  || die "the autoinstall on the ISO differs from the rendered template"
if grep -qE '@(USERNAME|HOSTNAME|PASSWORD_HASH|RELEASE)@' "$WORK/verify/user-data"; then
  die "the autoinstall on the ISO still contains template tokens"
fi
log "the autoinstall on the ISO is byte-identical to the rendered template"

# 2. the boot entries carry the autoinstall
xorriso_run -osirrox on -indev "$BUILT" \
  -extract /boot/grub/grub.cfg "$WORK/verify/grub.cfg" >/dev/null 2>&1 || true
[[ -s "$WORK/verify/grub.cfg" ]] \
  || die "the built ISO has no readable /boot/grub/grub.cfg"
grep -q 'ds=nocloud' "$WORK/verify/grub.cfg" \
  || die "the boot entries on the built ISO do not request the autoinstall"
log "boot entries request the autoinstall ($(grep -c 'ds=nocloud' "$WORK/verify/grub.cfg") of them)"

# 3. the repack left the installer's own files alone. A remaster rewrites the
#    whole tree, so the kernel and initrd that grub loads are compared against the
#    image they came from — a corrupted one would fail on the target machine and
#    nowhere else.
rm -rf "$WORK/intcheck"; mkdir -p "$WORK/intcheck"
for f in casper/vmlinuz casper/initrd; do
  n="$(basename "$f")"
  xorriso_run -osirrox on -indev "$BASE_ISO" -extract "/$f" "$WORK/intcheck/base-$n" >/dev/null 2>&1 || true
  xorriso_run -osirrox on -indev "$BUILT" -extract "/$f" "$WORK/intcheck/built-$n" >/dev/null 2>&1 || true
  if [[ -s "$WORK/intcheck/base-$n" && -s "$WORK/intcheck/built-$n" ]]; then
    if [[ "$(sha256sum "$WORK/intcheck/base-$n" | awk '{print $1}')" \
          != "$(sha256sum "$WORK/intcheck/built-$n" | awk '{print $1}')" ]]; then
      die "the repack changed /$f — the image would not boot"
    fi
  else
    warn "could not compare /$f between the base and the built image"
  fi
done
log "the installer's own kernel and initrd are byte-identical to the base image"

# 4. and it is bootable: a BIOS *and* a UEFI El Torito entry, over an isohybrid
#    MBR/GPT layout that a USB stick and virtual media both accept.
TORITO="$(xorriso_run -indev "$BUILT" -report_el_torito plain 2>/dev/null || true)"
printf '%s' "$TORITO" | grep -q 'El Torito boot img :   1  BIOS' \
  || die "the built ISO has no BIOS boot entry"
printf '%s' "$TORITO" | grep -q 'El Torito boot img :   2  UEFI' \
  || die "the built ISO has no UEFI boot entry"
log "bootable: an El Torito BIOS entry and an El Torito UEFI entry"
AREA="$(xorriso_run -indev "$BUILT" -report_system_area plain 2>/dev/null || true)"
if printf '%s' "$AREA" | grep -q 'MBR protective-msdos-label' \
   && printf '%s' "$AREA" | grep -q 'GPT'; then
  log "isohybrid layout present (a USB stick and virtual media both boot it)"
else
  die "the built ISO has no isohybrid MBR/GPT area; a plain USB-stick write would not boot"
fi

# 5. optional: actually boot it. The boot is the part a structural check cannot
#    prove — whether firmware finds the image, whether grub passes the autoinstall
#    on, and whether the installer reads /nocloud off the media. The test watches
#    the serial console (which the build added console=ttyS0 for) and stops there:
#    the operator's identity screen is the installer behaving as designed.
if [[ "${ONTRAK_ISO_SMOKE:-0}" == "1" ]]; then
  step "booting the image in QEMU"
  SECONDS_TO_WATCH="${ONTRAK_ISO_SMOKE_SECONDS:-600}"
  if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    warn "qemu-system-x86_64 is not installed; skipping the boot test"
    warn "install qemu-system-x86 (apt) to have this ISO boot-tested"
  else
    SMOKE_DIR="$WORK/smoke"
    SMOKE_LOG="$SMOKE_DIR/serial.log"
    SMOKE_DISK="$SMOKE_DIR/disk.img"
    rm -rf "$SMOKE_DIR"; mkdir -p "$SMOKE_DIR"; : >"$SMOKE_LOG"
    # A real target disk, sparse: the installer must find something to install
    # to, or it would stop at storage rather than at identity.
    truncate -s 40G "$SMOKE_DISK"
    ACCEL=(); [[ -e /dev/kvm ]] && ACCEL=(-enable-kvm -cpu host)
    log "booting for ${SECONDS_TO_WATCH}s (${ACCEL[*]:-no KVM: slow})"
    log "serial console: $SMOKE_LOG   disk: $SMOKE_DISK"
    timeout "$SECONDS_TO_WATCH" qemu-system-x86_64 \
      -m 4096 -smp 2 "${ACCEL[@]}" \
      -drive file="$SMOKE_DISK",if=virtio,format=raw \
      -cdrom "$BUILT" -boot d -no-reboot \
      -netdev user,id=net0,hostfwd=tcp::2222-:22 \
      -device virtio-net-pci,netdev=net0 \
      -nographic -serial "file:$SMOKE_LOG" || true

    # What the log has to show: the installer itself, and the autoinstall being
    # read off the media (the nocloud datasource or the identity screen).
    if grep -qiE 'ubuntu|casper|subiquity' "$SMOKE_LOG"; then
      log "the boot reaches the installer"
    else
      warn "no sign of the installer in $SMOKE_LOG — this image may not boot"
    fi
    if grep -qiE 'nocloud|autoinstall|profile setup' "$SMOKE_LOG"; then
      log "the installer reads the autoinstall from the media"
    else
      warn "no sign of the autoinstall being read; check /nocloud and the boot entries"
    fi
    log "the installer's own view is in $SMOKE_LOG (kept for inspection)"
  fi
fi

# --------------------------------------------------------------------- out ----
step "writing $OUT"
install -m 0644 "$BUILT" "$OUT"

cat >"$CREDS" <<EOF
OnTrak installer $RELEASE — default credentials
Built $(date -u +%Y-%m-%dT%H:%M:%SZ) from $(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown revision')

The identity screen asks for these; the values below are only the defaults a
boot nobody answers will use. Change them at the console, and delete this file
once the machine is built.

  username  $USERNAME
  password  $DEFAULT_PASSWORD
  hostname  $HOSTNAME_DEFAULT
EOF
chmod 0600 "$CREDS"

cat <<EOF

$(log "installer image ready")
  ISO         $OUT  ($SIZE_H)
  defaults    $CREDS
  base        Ubuntu $RELEASE LTS live-server, remastered
  first boot  Ubuntu, then Incus + the OnTrak checkout + the portal stack

Write it to a USB stick (it boots from BIOS and UEFI):

  sudo dd if=$OUT of=/dev/sdX bs=4M status=progress oflag=sync

Then boot the target machine from it. The installer stops once, on identity;
after the reboot the host provisions itself — journalctl -fu ontrak-firstboot.
EOF
