#!/usr/bin/env bash
#
# install-test.sh — install a machine from the installer ISO, then check it.
#
# The build's checks are structural: the payload is on the image, the boot
# entries ask for the autoinstall, the kernel survived the repack. This is the
# only test that answers the question an operator actually has — does booting
# this image end in a range host?
#
#   infra/installer/install-test.sh dist/ontrak-installer-24.04.5-amd64.iso
#
# It does four things:
#
#   1. boots the image in QEMU with KVM, on a blank target disk
#   2. answers the one interactive screen — identity — over the QEMU monitor,
#      the way an operator at a console would
#   3. waits for the install to finish and the machine to reboot
#   4. boots the installed disk and checks, over SSH, that the payload really
#      landed: the first-boot script, its unit enabled, the operator's account,
#      and the install's own marker file
#
# It needs KVM. Without it an Ubuntu install takes hours, so the test refuses to
# pretend: `ONTRAK_INSTALL_TEST_ALLOW_TCG=1` accepts the slow path deliberately.
#
# Environment:
#   ONTRAK_INSTALL_TEST_WORK        scratch dir (default /tmp/ontrak-install-test)
#   ONTRAK_INSTALL_TEST_MINUTES     per-phase deadline (default 45)
#   ONTRAK_INSTALL_TEST_DISK_GB     target disk (default 40)
#   ONTRAK_INSTALL_TEST_USER        identity username to sign in as (default: from
#                                   the -creds.txt beside the ISO, else ontrak)
#   ONTRAK_INSTALL_TEST_PASSWORD    same (default: from the -creds.txt)
#   ONTRAK_INSTALL_TEST_HOSTNAME    the hostname to expect (default ontrak-range)
#   ONTRAK_INSTALL_TEST_SSH_PORT    host port forwarded to the guest's 22 (default 2222)
#   ONTRAK_INSTALL_TEST_KEEP=1      keep the disk and logs when it passes
#
set -euo pipefail

ISO="${1:-}"
[[ -n "$ISO" ]] || { echo "usage: $0 <installer.iso>" >&2; exit 2; }
[[ -f "$ISO" ]] || { echo "no such ISO: $ISO" >&2; exit 2; }
ISO="$(cd "$(dirname "$ISO")" && pwd)/$(basename "$ISO")"

WORK="${ONTRAK_INSTALL_TEST_WORK:-/tmp/ontrak-install-test}"
MINUTES="${ONTRAK_INSTALL_TEST_MINUTES:-45}"
DISK_GB="${ONTRAK_INSTALL_TEST_DISK_GB:-40}"
SSH_PORT="${ONTRAK_INSTALL_TEST_SSH_PORT:-2222}"
HOSTNAME_EXPECTED="${ONTRAK_INSTALL_TEST_HOSTNAME:-ontrak-range}"
KEEP="${ONTRAK_INSTALL_TEST_KEEP:-0}"

# The build writes the fallback identity beside the image; use it unless the
# operator says otherwise.
CREDS="${ISO%.iso}-creds.txt"
if [[ -f "$CREDS" ]]; then
  USERNAME="${ONTRAK_INSTALL_TEST_USER:-$(awk '/^  username/{print $2}' "$CREDS")}"
  PASSWORD="${ONTRAK_INSTALL_TEST_PASSWORD:-$(awk '/^  password/{print $2}' "$CREDS")}"
else
  USERNAME="${ONTRAK_INSTALL_TEST_USER:-ontrak}"
  PASSWORD="${ONTRAK_INSTALL_TEST_PASSWORD:-}"
fi

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; [ -f "$WORK/serial-1.log" ] && { echo "--- installer console (last 40 lines) ---"; tail -40 "$WORK/serial-1.log" | tr -d '\r'; }; exit 1; }

for t in qemu-system-x86_64 sshpass; do
  command -v "$t" >/dev/null 2>&1 || { echo "$t is required" >&2; exit 2; }
done

# ---------------------------------------------------------------- the host ----
KVM=0
if python3 - <<'PY' 2>/dev/null
import fcntl, os
fd = os.open("/dev/kvm", os.O_RDWR)
fcntl.ioctl(fd, 0xAE00, 0)   # KVM_GET_API_VERSION
os.close(fd)
PY
then KVM=1; fi
if [[ $KVM -eq 0 && "${ONTRAK_INSTALL_TEST_ALLOW_TCG:-0}" != "1" ]]; then
  die "no usable /dev/kvm: an Ubuntu install under software emulation takes hours.
    Run this on a machine with virtualisation, or set ONTRAK_INSTALL_TEST_ALLOW_TCG=1
    to accept the slow path."
fi
ACCEL=(); [[ $KVM -eq 1 ]] && ACCEL=(-enable-kvm -cpu host)
log "QEMU $(qemu-system-x86_64 --version | head -1 | awk '{print $4}')${ACCEL:+ with KVM}"

rm -rf "$WORK"; mkdir -p "$WORK"
truncate -s "${DISK_GB}G" "$WORK/disk.img"
log "ISO     $ISO"
log "target  $WORK/disk.img (${DISK_GB}G)"
log "console $WORK/serial-1.log"
log "sign in $USERNAME@$HOSTNAME_EXPECTED on 127.0.0.1:$SSH_PORT (after the install)"

MONITOR="$WORK/monitor.sock"
monitor() {
  python3 - "$MONITOR" "$1" <<'PY'
import socket, sys, time
path, command = sys.argv[1], sys.argv[2]
for _ in range(20):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(path)
        break
    except OSError:
        time.sleep(1)
else:
    sys.exit("no QEMU monitor at " + path)
s.sendall((command + "\n").encode())
time.sleep(0.2)
s.close()
PY
}

QEMU_PID=""
stop_qemu() {
  [[ -n "$QEMU_PID" ]] || return 0
  kill "$QEMU_PID" 2>/dev/null || true
  wait "$QEMU_PID" 2>/dev/null || true
  QEMU_PID=""
}
trap 'stop_qemu' EXIT

boot_iso() {
  qemu-system-x86_64 -m 4096 -smp 2 "${ACCEL[@]}" \
    -drive file="$WORK/disk.img",if=virtio,format=raw \
    -cdrom "$ISO" -boot d -no-reboot \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
    -device virtio-net-pci,netdev=net0 \
    -monitor unix:"$MONITOR",server,nowait \
    -display none -serial "file:$WORK/serial-1.log" &
  QEMU_PID=$!
}

boot_disk() {
  qemu-system-x86_64 -m 4096 -smp 2 "${ACCEL[@]}" \
    -drive file="$WORK/disk.img",if=virtio,format=raw \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
    -device virtio-net-pci,netdev=net0 \
    -display none -serial "file:$WORK/serial-2.log" &
  QEMU_PID=$!
}

# ---------------------------------------------------- 1. install, unattended ---
log "1/4 booting the installer"
boot_iso
DEADLINE=$(( $(date +%s) + MINUTES * 60 ))
# Grub waits 30s, then the live system boots to the installer's first screen. Keys
# before that reach grub (which just boots its default entry) or the kernel, so
# the wait is about keeping the log readable rather than avoiding damage.
START_PRESSING=$(( $(date +%s) + 120 ))

# The identity screen is the only prompt, and the ISO carries defaults for it, so
# an Enter at each field is all an operator would do. Keys go in through the
# monitor rather than the console: where subiquity draws its UI (tty1, the serial
# console, or both) makes no difference to a keystroke.
log "2/4 answering the identity screen until the machine reboots"
PRESSES=0
while kill -0 "$QEMU_PID" 2>/dev/null; do
  if (( $(date +%s) > DEADLINE )); then
    monitor "screendump $WORK/installer-screen.ppm" 2>/dev/null || true
    die "the install did not finish within ${MINUTES} minutes (a screenshot and the
    console log are in $WORK)"
  fi
  # Give the live system time to reach the installer before touching the keyboard.
  if (( $(date +%s) > START_PRESSING )) && (( PRESSES < 40 )); then
    monitor "sendkey ret" 2>/dev/null || true
    PRESSES=$((PRESSES + 1))
  fi
  sleep 10
done
wait "$QEMU_PID" 2>/dev/null || true
QEMU_PID=""
log "   the machine rebooted after $PRESSES keystroke(s) — the install is done"

# -no-reboot means QEMU exits when the guest reboots, which is the signal the
# install finished — but it is also what a triple fault looks like, so the disk is
# checked before believing it.
if command -v fdisk >/dev/null 2>&1; then
  timeout 60 fdisk -l "$WORK/disk.img" 2>/dev/null | grep -q 'EFI System' \
    || die "QEMU exited but the target disk has no installed system on it — the install
    did not complete. The console log is $WORK/serial-1.log"
  log "   the target disk now holds an installed system"
else
  warn "fdisk is not installed; not checking the target disk's partition table here"
  warn "the SSH checks below are the real test either way"
fi

# --------------------------------------------------- 2. the installed system ---
log "3/4 booting the installed machine"
boot_disk

log "   waiting for sshd"
SSH_READY=0
for _ in $(seq 1 60); do
  if (( $(date +%s) > DEADLINE )); then break; fi
  if sshpass -p "$PASSWORD" ssh -p "$SSH_PORT" \
       -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
       -o PreferredAuthentications=password -o PubkeyAuthentication=no \
       -o ConnectTimeout=5 -o LogLevel=ERROR \
       "$USERNAME@127.0.0.1" true 2>/dev/null; then
    SSH_READY=1; break
  fi
  sleep 5
done
[[ $SSH_READY -eq 1 ]] || die "no SSH on 127.0.0.1:$SSH_PORT within the deadline: the
    installed machine either did not come up or did not accept the default
    credentials. Its console log is $WORK/serial-2.log"

log "4/4 checking the installed machine"
guest() {
  sshpass -p "$PASSWORD" ssh -p "$SSH_PORT" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o ConnectTimeout=10 -o LogLevel=ERROR \
    "$USERNAME@127.0.0.1" "$1"
}

FAILED=0
check() { # check <description> <shell test>
  if guest "$2" >/dev/null 2>&1; then
    printf '  \033[32m[ ok ]\033[0m %s\n' "$1"
  else
    printf '  \033[31m[FAIL]\033[0m %s\n' "$1"
    FAILED=$((FAILED + 1))
  fi
  return 0
}

echo
check "the identity screen's defaults were applied (hostname)" \
      "[ \"\$(hostname)\" = '$HOSTNAME_EXPECTED' ]"
check "the operator signed in is $USERNAME and can sudo" \
      "id -nG | grep -qw sudo"
check "the first-boot script is installed and executable" \
      "test -x /usr/local/sbin/ontrak-firstboot.sh"
check "its unit is installed" \
      "test -f /etc/systemd/system/ontrak-firstboot.service"
check "its unit is enabled for the first boot" \
      "systemctl is-enabled ontrak-firstboot.service"
check "the settings example is in place" \
      "test -f /etc/ontrak/firstboot.env.example"
check "the on-media readme was installed" \
      "test -f /opt/ONTRAK-INSTALL.txt"
check "the SSH server is running (the range is reachable)" \
      "systemctl is-active ssh"

echo
FIRSTBOOT_STATE="$(guest 'systemctl is-active ontrak-firstboot.service 2>/dev/null || true')"
echo "  first-boot unit: ${FIRSTBOOT_STATE:-unknown}"
echo "  --- its log so far ---"
guest 'tail -25 /var/log/ontrak-firstboot.log 2>/dev/null || echo "(no log yet)"' || true

if [[ $FAILED -gt 0 ]]; then
  warn "$FAILED check(s) failed — the machine installed, but not as the image intends"
  warn "disk and logs kept in $WORK"
  exit 1
fi

echo
log "the image installs a machine that provisions itself"
if [[ "$KEEP" != "1" && ${ONTRAK_INSTALL_TEST_KEEP:-0} != "1" ]]; then
  stop_qemu
  rm -f "$WORK/disk.img"
  log "kept the logs: $(ls "$WORK" | tr '\n' ' ')"
else
  warn "keeping everything in $WORK (QEMU still running: kill $QEMU_PID)"
fi
