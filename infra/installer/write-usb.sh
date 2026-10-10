#!/usr/bin/env bash
#
# Write an OnTrak installer ISO to a USB stick, and prove the bytes on the stick.
#
#   infra/installer/write-usb.sh <iso> [device]
#
# With no device it picks the one whole USB disk that is removable, has nothing
# mounted and is not the disk the running system is on — and refuses rather than
# guessing if that is not exactly one device. With a device it still refuses the
# system disk, and asks for --force before writing to something that is not a
# removable USB disk.
#
# Why this rather than the one-line `dd if=<iso> of=/dev/sdX bs=4M oflag=sync`:
# measured on the host this was written on, a single 3.8 GB write killed the device
# twice — `sd … Device offlined - not ready after error recovery` at 3.5 GB and at
# 3.75 GB of 4.07 GB, with `xhci_hcd … AMD-Vi: Event logged [IO_PAGE_FAULT]` beside
# it. A one-shot dd reports none of that until the whole image has been handed to the
# page cache, and a stick that error-recovered mid-write holds a plausible-looking
# image with a hole in it — which boots to a grub prompt on the machine in front of
# the operator, not to an error here. So:
#
#   * the write is in 64 MiB chunks, `oflag=direct`, one fsync per chunk, and a chunk
#     that fails is retried on its own after the device has been given a moment. The
#     failure names the offset it died at. (The run that flashed the reference stick
#     hit `I/O error` on its first chunk and then wrote the other sixty clean, which
#     is exactly what this is for.)
#   * the stick is read back afterwards and hashed against the ISO, so "the write
#     returned 0" is never the last word.
#
# Exit: 0 the stick holds the ISO, 1 a write or a verification failed, 2 a usage or
# safety refusal.
set -euo pipefail

CHUNK_MIB=64
RETRIES=3

die() { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit "${2:-2}"; }
log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }

ISO=""
DEV=""
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -*) die "unknown option: $arg" ;;
    *) if [[ -z "$ISO" ]]; then ISO="$arg"
       elif [[ -z "$DEV" ]]; then DEV="$arg"
       else die "too many arguments"; fi ;;
  esac
done
[[ -n "$ISO" ]] || die "usage: $0 <iso> [device]"
[[ -f "$ISO" ]] || die "no such ISO: $ISO"
ISO="$(readlink -f "$ISO")"

# --------------------------------------------------------------- the device ---
system_disks() {
  # The whole disks carrying the running system's mounts. Never a target: this is the
  # check that keeps a typo from taking the machine's disk with it.
  lsblk -nro NAME,MOUNTPOINTS | awk '$2 ~ /^\/($|boot)/ {print $1}' | sed 's/[0-9]*$//' | sort -u
}

is_removable_usb() {
  local base
  base="$(basename "$1")"
  [[ "$(cat "/sys/block/$base/removable" 2>/dev/null)" == 1 ]] || return 1
  lsblk -nro TRAN "/dev/$base" | head -1 | grep -qx usb
}

pick_device() {
  local found=() d name
  for d in /sys/block/sd*; do
    name="/dev/$(basename "$d")"
    is_removable_usb "$name" || continue
    system_disks | grep -qx "$(basename "$name")" && continue
    # A mounted partition would be written out from under its filesystem.
    if [[ -n "$(lsblk -nro MOUNTPOINTS "$name" | tr -d ' \n')" ]]; then
      warn "$name has a mounted partition; not it"
      continue
    fi
    found+=("$name")
  done
  [[ ${#found[@]} -eq 1 ]] || return 1
  printf '%s\n' "${found[0]}"
}

if [[ -z "$DEV" ]]; then
  DEV="$(pick_device)" || die "$(printf 'no single candidate USB disk.\n    Attach the stick to this machine and re-run, or name the device:\n    %s %s /dev/sdX' "$0" "$ISO")"
else
  [[ -b "$DEV" ]] || die "$DEV is not a block device"
  DEV="$(readlink -f "$DEV")"
  system_disks | grep -qx "$(basename "$DEV")" && die "$DEV carries the running system — refusing"
  if ! is_removable_usb "$DEV"; then
    [[ $FORCE -eq 1 ]] || die "$DEV is not a removable USB disk. Re-run with --force if you mean it."
    warn "$DEV is not a removable USB disk, and --force was given"
  fi
fi

if [[ -n "$(lsblk -nro MOUNTPOINTS "$DEV" | tr -d ' \n')" ]]; then
  log "unmounting anything mounted from $DEV"
  while read -r part; do [[ -n "$part" ]] && umount "$part" 2>/dev/null || true; done \
    < <(lsblk -nro NAME "$DEV" | tail -n +2 | sed 's|^|/dev/|')
fi

SIZE="$(stat -c %s "$ISO")"
DEV_SIZE="$(blockdev --getsize64 "$DEV")"
[[ "$DEV_SIZE" -ge "$SIZE" ]] || die "$DEV holds $((DEV_SIZE / 1024 / 1024)) MiB and the ISO is $((SIZE / 1024 / 1024)) MiB"
log "ISO    : $ISO ($((SIZE / 1024 / 1024)) MiB, sha256 $(sha256sum "$ISO" | cut -c1-16)…)"
log "target : $DEV — $(lsblk -ndo VENDOR,MODEL "$DEV" 2>/dev/null | tr -s ' ')"

# ---------------------------------------------------------------- the write ---
CHUNK_BYTES=$((CHUNK_MIB * 1024 * 1024))
CHUNKS=$(( (SIZE + CHUNK_BYTES - 1) / CHUNK_BYTES ))
TOTAL_MIB=$(( (SIZE + 1024 * 1024 - 1) / (1024 * 1024) ))
log "writing in $CHUNKS chunk(s) of ${CHUNK_MIB} MiB, direct I/O, one fsync each"

for ((i = 0; i < CHUNKS; i++)); do
  at=$((i * CHUNK_MIB))                     # MiB into the ISO
  left=$((SIZE - i * CHUNK_BYTES))
  this=$CHUNK_MIB
  if [[ $left -lt $CHUNK_BYTES ]]; then
    this=$(( (left + 1024 * 1024 - 1) / (1024 * 1024) ))
  fi

  error_log="$(mktemp)"
  ok=0
  for attempt in $(seq 1 "$RETRIES"); do
    if dd if="$ISO" of="$DEV" bs=1M skip="$at" seek="$at" count="$this" \
          oflag=direct conv=fsync status=none 2>"$error_log"; then
      ok=1; break
    fi
    warn "chunk at ${at} MiB failed (attempt $attempt/$RETRIES): $(tr -d '\n' <"$error_log" | tail -c 160)"
    state="/sys/block/$(basename "$DEV")/device/state"
    [[ -e "$state" ]] && warn "device state: $(cat "$state" 2>/dev/null)"
    sleep 5
  done
  rm -f "$error_log"
  [[ $ok -eq 1 ]] || die "write failed at ${at} MiB after $RETRIES attempts — the stick or its port is not taking the image" 1
  printf '\r\033[36m==>\033[0m %3d%% (%d/%d MiB)' \
    "$(( (at + this) * 100 / TOTAL_MIB ))" "$((at + this))" "$TOTAL_MIB"
done
echo
sync
log "write complete; reading the stick back"

# ---------------------------------------------------------- verify the stick ---
# The first $SIZE bytes of the device, hashed against the ISO.
#
# `iflag=count_bytes` so dd stops exactly at the ISO's length — it is not a multiple
# of the block size — and the read's stderr is reported rather than hidden: a failed
# read that `set -e` turns into a silent exit is indistinguishable from a stick that
# did not take the write, which is the one thing this script exists to tell apart.
# (It did exactly that, once, here.)
#
# The read is buffered on purpose: every write above was `oflag=direct`, so nothing
# of ours is in the page cache, and `iflag=direct` is not something every device
# takes anyway — a loop device refuses it with `dd: IO error: Invalid input`.
read_err="$(mktemp)"
if ! GOT="$(dd if="$DEV" bs=1M iflag=count_bytes count="$SIZE" 2>"$read_err" \
            | sha256sum | awk '{print $1}')"; then
  die "could not read $DEV back: $(tail -c 300 "$read_err" | tr '\n' ' ')" 1
fi
rm -f "$read_err"
WANT="$(sha256sum "$ISO" | awk '{print $1}')"
if [[ "$GOT" == "$WANT" ]]; then
  log "verified: the stick holds $ISO byte for byte"
  log "boot the machine from it: BIOS and UEFI both work, and the menu offers the"
  log "unattended install or the install onto a disk you choose (a USB stick)."
  exit 0
fi
die "the stick does NOT match the ISO
    expected $WANT
    read     $GOT
  A stick that error-recovered mid-write holds a plausible image with a hole in it.
  Re-run this; if it fails in the same place, the stick is the problem rather than
  the image." 1
