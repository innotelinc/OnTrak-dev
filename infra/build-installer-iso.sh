#!/usr/bin/env bash
#
# Build the OnTrak range-host installer ISO.
#
#   infra/build-installer-iso.sh
#
# The result installs Ubuntu Server 24.04 LTS on a bare machine — or on a disk you
# choose, which is how a USB stick becomes a portable range — and then turns that
# machine into a range host on first boot: Incus, the ontrak0 lab bridge, the OnTrak
# checkout and the portal stack. The operator answers identity (username, hostname,
# password, SSH key), and the target disk if they asked for that; nothing is baked in.
#
# It is a remaster of the official Ubuntu Server live ISO:
#
#   * /nocloud/{user-data,meta-data}          the `machine` autoinstall: this
#                                             machine's disk, identity the only
#                                             screen (see autoinstall/)
#   * /nocloud-choose-disk/{user-data,meta-data}  the `choose-disk` autoinstall: the
#                                             same install with the storage screen
#                                             up, so a USB stick can be the target
#   * /ontrak/…                               the first-boot payload (see firstboot/)
#   * the boot entries get `autoinstall ds=nocloud;s=/cdrom/<dir>/` — one entry per
#     profile, so the menu offers both (see infra/installer/patch-grub.py)
#
# The two autoinstalls are rendered from one template
# (infra/installer/render-autoinstall.py), because everything about the install
# except which screens stay up has to stay identical.
#
# xorriso is used to extract and repack, and to recreate the original boot
# equipment (BIOS El Torito, UEFI, isohybrid MBR/GPT) so the image boots from a
# USB stick, a DVD and virtual media alike. If xorriso is not installed on this
# machine, it runs from a container instead — nothing is installed globally.
#
# Environment:
#   ONTRAK_ISO_TIERS       build one image per sizing tier, space- or
#                          comma-separated (dev, class, full — see
#                          infra/installer/tiers/ and docs/installer.md,
#                          "Sizing tiers"). Each tier is its own image, built and
#                          verified from its own extraction of the base ISO, and
#                          named dist/ontrak-installer-<tier>-<release>-amd64.iso.
#                          Unset means one untiered image, exactly as before.
#   ONTRAK_ISO_TIER        build just this one tier (what the fan-out above runs;
#                          set it by hand to build one tier the same way)
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

# ---------------------------------------------------------------------- tiers --
# A *sizing tier* is one image built for one class of machine: `dev` for the small
# host or nested VM a checkout is developed on, `class` for a class of 8-12, `full`
# for a cohort (infra/installer/tiers/, and docs/installer.md). What a tier changes
# is not the install — both entries are identical whatever the tier — but what the
# image bakes into the installed host (its settings), what it says about the machine
# it is for (its floors), and what its menu and its name are labelled with.
#
# ONTRAK_ISO_TIERS builds several: this script re-runs itself once per tier rather
# than growing a second copy of the build inside itself. That is the point of the
# fan-out — every tier gets its own extraction, rendering, verification and output,
# so an image is never assembled from a tree another tier has been through.
TIER="${ONTRAK_ISO_TIER:-}"
if [[ -z "$TIER" && -n "${ONTRAK_ISO_TIERS:-}" ]]; then
  TIERS="${ONTRAK_ISO_TIERS//,/ }"
  COUNT="$(wc -w <<<"$TIERS")"
  if [[ -n "${ONTRAK_ISO_OUT:-}" && "$COUNT" -gt 1 ]]; then
    die "ONTRAK_ISO_OUT names one file, and ONTRAK_ISO_TIERS names $COUNT images.
    Set ONTRAK_ISO_OUT and one tier, or drop ONTRAK_ISO_OUT and take the default
    names (dist/ontrak-installer-<tier>-<release>-amd64.iso)."
  fi
  for ONE in $TIERS; do
    log "building the $ONE tier"
    ONTRAK_ISO_TIER="$ONE" bash "${BASH_SOURCE[0]}" || exit $?
  done
  exit 0
fi
TIERS_DIR="$INSTALLER_DIR/tiers"
if [[ -n "$TIER" ]]; then
  TIER_FILE="$TIERS_DIR/$TIER.env"
  if [[ ! -f "$TIER_FILE" ]]; then
    die "no tier '$TIER' in $TIERS_DIR
    there is: $(cd "$TIERS_DIR" 2>/dev/null && ls *.env 2>/dev/null | sed 's/\.env$//' | tr '\n' ' ')"
  fi
  # Tier files are **plain KEY=value**, and deliberately not shell: a title with
  # spaces in it is not a shell assignment (`TIER_TITLE=OnTrak class range` runs
  # `class`), and quoting the values would make the two readers disagree about what
  # the file says. Everything after the first `=` is the value, for this reader and
  # for render-tier.py's; render-tier.py is also the one that validates the file.
  tier_value() { # tier_value <key> — the value of one plain KEY=value line
    sed -n "s/^$1=//p" "$TIER_FILE" | head -1
  }
  TIER_LABEL="${ONTRAK_TIER_LABEL:-$(tier_value TIER_LABEL)}"
  TIER_LABEL="${TIER_LABEL:-$TIER}"
  TIER_HOSTNAME="$(tier_value TIER_HOSTNAME)"
  TIER_TITLE="$(tier_value TIER_TITLE)"
  # The tier names the machine image, its volume id and its menu — the three places
  # an operator with two sticks in front of them can tell which one they hold. Its
  # hostname is only a *default*: ONTRAK_INSTALLER_HOSTNAME still wins.
  HOSTNAME_DEFAULT="${ONTRAK_INSTALLER_HOSTNAME:-${TIER_HOSTNAME:-ontrak-range}}"  # the tier only defaults it
  # The ISO's volume label is left as Canonical's, tiered image or not: it is what
  # casper and subiquity look the live medium up by, and a label that no longer says
  # `Ubuntu-Server …` is a risk with no upside — the tier is on the file name, in the
  # menu and in the README on the installed machine, which is where a person looks.
  if [[ -z "${ONTRAK_ISO_OUT:-}" ]]; then
    OUT="$PROJECT_ROOT/dist/ontrak-installer-${TIER_LABEL}-${RELEASE}-amd64.iso"
    CREDS="${OUT%.iso}-creds.txt"
  fi
  log "tier: $TIER_LABEL — $TIER_TITLE"
  log "       $TIER_FILE → $OUT"
fi

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
    # The output directory is mounted as well as the cache: the built image is
    # written straight to where it is published (see $BUILT), which is not
    # necessarily the cache.
    docker run --rm -v "$CACHE:$CACHE" -v "$(dirname "$OUT"):$(dirname "$OUT")" \
      "$BUILDER_IMAGE" xorriso "$@"
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
  # --continue-at - so an interrupted download resumes instead of starting the 3.8
  # GiB again: on a slow link this is the difference between a build and an evening.
  # The sha256 check below is what makes a resumed file trustworthy — a truncated
  # one fails it, and the message says to delete the file and re-run.
  curl -fL --retry 5 --retry-delay 5 --progress-bar --continue-at - \
    -o "$BASE_ISO.part" "$BASE_URL"
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

# The autoinstalls, the way the nocloud datasource expects each one: one
# user-data/meta-data pair per profile, in the directory its boot entry will name.
# The renderer prints the directory it wrote, and the boot entries are patched with
# that value rather than with a second copy of the paths — a boot entry pointing at
# a directory nothing wrote is the one way this can fail quietly.
need python3 "renders the autoinstall template"
render_autoinstall() { # render_autoinstall <profile>
  python3 "$INSTALLER_DIR/render-autoinstall.py" \
    --profile "$1" --iso-tree "$EXTRACT" \
    --template "$INSTALLER_DIR/autoinstall/user-data.dist" \
    --meta-data "$INSTALLER_DIR/autoinstall/meta-data" \
    --username "$USERNAME" --hostname "$HOSTNAME_DEFAULT" \
    --password-hash "$PASSWORD_HASH" --release "$RELEASE" | tail -n 1
}
MACHINE_DIR="$(render_autoinstall machine)"
CHOOSE_DIR="$(render_autoinstall choose-disk)"
log "wrote $MACHINE_DIR/user-data (identity is the only screen)"
log "wrote $CHOOSE_DIR/user-data (identity, then pick the target disk)"

# The first-boot payload, straight from the repository so the ISO and the repo
# cannot drift apart.
mkdir -p "$EXTRACT/ontrak"
cp "$INSTALLER_DIR/firstboot/ontrak-firstboot.sh"        "$EXTRACT/ontrak/firstboot.sh"
cp "$INSTALLER_DIR/firstboot/ontrak-firstboot.service"   "$EXTRACT/ontrak/ontrak-firstboot.service"
cp "$INSTALLER_DIR/firstboot/firstboot.env.example"      "$EXTRACT/ontrak/firstboot.env.example"
cp "$INSTALLER_DIR/tier-check.py"                        "$EXTRACT/ontrak/tier-check.py"
cp "$INSTALLER_DIR/README.txt"                           "$EXTRACT/ontrak/README.txt"
chmod 0755 "$EXTRACT/nocloud" "$EXTRACT/ontrak" "$EXTRACT/ontrak/firstboot.sh"
chmod 0755 "$EXTRACT/ontrak/tier-check.py"
log "wrote /ontrak (first-boot payload)"

# The tier's own files, when this image is one: /ontrak/tier.env (what machine it
# is for) and /ontrak/firstboot.env (the settings that go with it), plus a line in
# the README saying which tier this is. The script renders those, and it runs after
# the payload is copied because it annotates the README that was just written.
if [[ -n "$TIER" ]]; then
  python3 "$INSTALLER_DIR/render-tier.py" \
    --tier "$TIER" --tiers-dir "$TIERS_DIR" --iso-tree "$EXTRACT"
  # Keep what we shipped, as with the autoinstalls above, so a finished image can be
  # accounted for against the tier file it came from rather than against a memory of
  # what the file said.
  cp "$EXTRACT/ontrak/tier.env"      "$WORK/tier.env.rendered"
  cp "$EXTRACT/ontrak/firstboot.env" "$WORK/firstboot.env.rendered"
fi

# ------------------------------------------------------------------- boot -----
step "pointing the boot entries at the autoinstalls"
# Every entry that loads /casper/vmlinuz is retitled to say what it does, pointed
# at the `machine` autoinstall, and duplicated for `choose-disk`. The duplicate
# boots `toram` too, because installing onto the stick you booted from is the
# point of it and the medium has to be free to be erased. The original stays
# first, so grub's default — an unattended install on this machine's disk — is
# what an unanswered boot still does. (patch-grub.py is its own file so it can be
# tested against a fixture; see scripts/tests/test_patch_grub.py.)
need python3 "patches the boot menu"
GRUB_FILES="$(cd "$EXTRACT" && find . -name 'grub.cfg' | sort)"
[[ -n "$GRUB_FILES" ]] || die "no grub.cfg in the extracted ISO — is $BASE_ISO really the Ubuntu Server live image?"
PATCHED=0
while IFS= read -r rel; do
  f="$EXTRACT/${rel#./}"
  if grep -q '/casper/vmlinuz' "$f"; then
    python3 "$INSTALLER_DIR/patch-grub.py" "$f" \
      --machine-dir "$MACHINE_DIR" --choose-dir "$CHOOSE_DIR" --tier "$TIER_LABEL"
    PATCHED=$((PATCHED + 1))
  fi
done <<< "$GRUB_FILES"
[[ $PATCHED -gt 0 ]] || die "no boot entries were patched"
log "$PATCHED boot configuration(s) patched"

# Keep a copy of what we shipped, so a finished ISO can be accounted for.
cp "$EXTRACT${MACHINE_DIR}/user-data" "$WORK/user-data.machine.rendered"
cp "$EXTRACT${CHOOSE_DIR}/user-data"  "$WORK/user-data.choose-disk.rendered"
chmod 0600 "$WORK"/user-data.*.rendered

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

# The image is written where it will be published, under a `.partial` name, and
# renamed into place once every check below has passed. Two reasons, and the second
# is the one that matters: a 3.8 GiB image copied afterwards needs the space twice,
# and the image that is *verified* is then the image that ships rather than a copy
# of it. A build that fails leaves something clearly marked as unfinished.
BUILT="$OUT.partial"
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
extract_from_image() { # extract_from_image <path-on-iso> — flattened into $WORK/verify
  local rel="${1#/}" name="${1#/}"
  # `nocloud/user-data` becomes `nocloud.user-data`: one flat name per path, so the
  # two datasources' user-data files do not land on top of each other.
  name="${name//\//.}"
  local out="$WORK/verify/$name"
  xorriso_run -osirrox on -indev "$BUILT" -extract "/$rel" "$out" >/dev/null 2>&1 || true
  [[ -s "$out" ]] || die "the built ISO has no readable /$rel"
}
PAYLOAD=("$MACHINE_DIR/user-data" "$MACHINE_DIR/meta-data" \
         "$CHOOSE_DIR/user-data" "$CHOOSE_DIR/meta-data" \
         ontrak/firstboot.sh ontrak/ontrak-firstboot.service \
         ontrak/firstboot.env.example ontrak/tier-check.py ontrak/README.txt)
# A tiered image carries two more files, and they are the ones that decide what the
# installed host does: its settings, and what machine it believes it is on.
if [[ -n "$TIER" ]]; then
  PAYLOAD+=(ontrak/tier.env ontrak/firstboot.env)
fi
for f in "${PAYLOAD[@]}"; do
  extract_from_image "$f"
done
log "payload present: both autoinstalls and the first-boot unit came back out of the image"

# Both autoinstalls on the image must be the ones rendered from the template — not
# an older copy, and not with a token left in it. The `choose-disk` one is checked
# as hard as the default: it is the profile a USB install uses, and it is the one an
# operator cannot fall back to a different entry for.
check_autoinstall() { # check_autoinstall <profile> <datasource dir>
  local profile="$1" dir="$2"
  local rendered="$WORK/user-data.$profile.rendered"
  local got="$WORK/verify/${dir#/}.user-data"
  diff -q "$rendered" "$got" >/dev/null \
    || die "the $profile autoinstall on the ISO differs from the rendered template"
  if grep -qE '@(USERNAME|HOSTNAME|PASSWORD_HASH|RELEASE|INTERACTIVE_SECTIONS)@' "$got"; then
    die "the $profile autoinstall on the ISO still contains template tokens"
  fi
  log "the $profile autoinstall on the ISO is byte-identical to the rendered template"
}
check_autoinstall machine "$MACHINE_DIR"
check_autoinstall choose-disk "$CHOOSE_DIR"

# The tier's own files, when this image has a tier. Both are read back off the image
# and compared with the tier file they came from: a tier that says 64 GiB in its
# README while baking the dev tier's pool sizing into the installed host is exactly
# the mistake worth failing a build over, because nothing else would notice it.
if [[ -n "$TIER" ]]; then
  diff -q "$WORK/tier.env.rendered" "$WORK/verify/ontrak.tier.env" >/dev/null \
    || die "the tier record on the ISO differs from $TIER_FILE"
  diff -q "$WORK/firstboot.env.rendered" "$WORK/verify/ontrak.firstboot.env" >/dev/null \
    || die "the first-boot settings on the ISO differ from $TIER_FILE"
  grep -q "SIZING TIER: $TIER_LABEL" "$WORK/verify/ontrak.README.txt" \
    || die "the README on the ISO does not name the $TIER_LABEL tier"
  log "tier $TIER_LABEL: the tier record and the settings on the ISO are $TIER_FILE's,\n    and the README names the tier"
fi

# 2. the boot entries carry the autoinstalls, and the menu offers the choice
xorriso_run -osirrox on -indev "$BUILT" \
  -extract /boot/grub/grub.cfg "$WORK/verify/grub.cfg" >/dev/null 2>&1 || true
[[ -s "$WORK/verify/grub.cfg" ]] \
  || die "the built ISO has no readable /boot/grub/grub.cfg"
for dir in "$MACHINE_DIR" "$CHOOSE_DIR"; do
  entries="$(grep -c "ds=nocloud;s=/cdrom$dir/" "$WORK/verify/grub.cfg" || true)"
  [[ "$entries" -ge 1 ]] \
    || die "no boot entry on the built ISO asks for the $dir autoinstall"
  # Quoted, because grub ends an argument at `;`: unquoted, the kernel is handed
  # `autoinstall ds=nocloud` alone — no seed directory, no console=, and no error
  # anywhere to say so. patch-grub.py has the measurement.
  grep -q "autoinstall \"ds=nocloud;s=/cdrom$dir/\"" "$WORK/verify/grub.cfg" \
    || die "the boot entry for $dir does not quote the datasource argument, so grub\n    will truncate it at the semicolon and the installer will find no autoinstall"
  log "boot entries: $entries for $dir (datasource argument quoted)"
done
grep -q "Install OnTrak on this machine's disk" "$WORK/verify/grub.cfg" \
  || die "the menu entries on the built ISO do not say they install on this machine's disk"
grep -q 'Install OnTrak on the disk you choose' "$WORK/verify/grub.cfg" \
  || die "the built ISO has no entry for installing onto a disk you choose (a USB stick)"
# A tiered image says so in the menu, because that is what an operator holding two
# OnTrak sticks is looking at when they choose one.
if [[ -n "$TIER" ]]; then
  grep -q "Install OnTrak on this machine's disk (unattended) \[$TIER_LABEL\]" "$WORK/verify/grub.cfg" \
    || die "the menu entries on the built ISO do not carry the $TIER_LABEL tier"
  log "the menu entries are labelled [$TIER_LABEL]"
fi
# And the choose-disk entry has to free the medium, because the medium can be the
# disk being installed to: without `toram` the installer would be erasing the
# filesystem it is running from.
CHOOSE_ENTRIES="$WORK/verify/choose-disk-entries.txt"
grep "ds=nocloud;s=/cdrom$CHOOSE_DIR/" "$WORK/verify/grub.cfg" >"$CHOOSE_ENTRIES"
[[ "$(grep -c 'toram' "$CHOOSE_ENTRIES" || true)" -eq "$(awk 'END {print NR}' "$CHOOSE_ENTRIES")" ]] \
  || die "a choose-disk boot entry does not boot toram: installing onto the stick it
    booted from would pull the ground out from under the installer"
log "the choose-disk entries boot toram (the medium is free to be installed to)"

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
step "publishing $OUT"
mv -f "$BUILT" "$OUT"
chmod 0644 "$OUT"

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
  tier        ${TIER_LABEL:-none — the untiered image}
  first boot  Ubuntu, then Incus + the OnTrak checkout + the portal stack

Write it to a USB stick (it boots from BIOS and UEFI):

  sudo dd if=$OUT of=/dev/sdX bs=4M status=progress oflag=sync

Then boot the target machine from it:

  1. Install OnTrak on this machine's disk (unattended)
       wipes the machine's own disk. The installer stops on identity; after the
       reboot the host provisions itself — journalctl -fu ontrak-firstboot.
  2. Install OnTrak on the disk you choose (USB stick, or another disk)
       the same install, with the storage screen up so you pick the disk. This is
       how a USB stick becomes a portable range host: install onto the stick, then
       boot the stick. It boots \`toram\`, so it can install onto the very stick it
       booted from.

There is no third choice — a server ISO has no live session, so the only thing
that boots off it is the installer. See docs/installer.md.
EOF
