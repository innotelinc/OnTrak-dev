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
#   ONTRAK_GOLDEN_NO_SECUREBOOT  build with Secure Boot (and TPM) OFF and satisfy
#                           the Windows 11 Setup gate with the standard LabConfig
#                           bypasses. For a lab that wants a no-Secure-Boot image
#                           (range VMs clone with secureboot=false anyway) or a host
#                           that cannot offer a TPM. This does NOT rescue a host
#                           that cannot virtualise SMM -- OVMF uses SMM for its
#                           runtime services either way -- and the image is not a
#                           production one.
#   ONTRAK_QEMU_ACCEL       kvm or tcg, overruling the host detection below. tcg
#                           runs the build VM on QEMU's software emulator, which is
#                           what a host whose KVM cannot virtualise SMM needs.
#   ONTRAK_GOLDEN_REQUIRE_KVM  refuse to build on such a host, instead of emulating
#                           it. ONTRAK_QEMU_ACCEL=kvm is the same answer, and is
#                           what ONTRAK_GOLDEN_ALLOW_NESTED=1 used to mean.
#   ONTRAK_GOLDEN_ADDRESS_ATTEMPTS  how many times to look for the build VM's
#                           address, 5 seconds apart (default 60). Software
#                           emulation raises it.
#   ONTRAK_GOLDEN_READY_TIMEOUT  seconds to wait for the cloned image's first boot
#                           to put WinRM up before applying post-install.ps1
#                           (default 1200). It is longer than a session's
#                           guest.ready_timeout_seconds because a generalized
#                           Windows image answers nothing until its first
#                           specialize pass has run.
#   ONTRAK_PACK_VM          name for incus-windows' own build VM
#                           (default ontrak-winpack-build). It is pinned so a failed
#                           build leaves something the operator can name.
#   ONTRAK_DISCARD_BUILD_VM  delete a leftover build VM at the start instead of
#                           stopping. A failed build keeps its VM on purpose.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_ROOT}/build"
CHECKOUT="${BUILD_DIR}/incus-windows"
TARGET="${ONTRAK_WINDOWS_TARGET:-11e}"
REF="${ONTRAK_INCUS_WINDOWS_REF:-main}"
# The unattend this range edits: the file pack.sh stages onto the virtio ISO and
# incus-windows installs Windows from. Both edits below use the same file, so the
# path is named once.
UNATTEND="${CHECKOUT}/unattend/${TARGET}/Autounattend.xml"
# The first-logon script that goes onto the unattended ISO as local/main.ps1, where
# upstream's OEM/main.ps1 finds and dot-sources it. It is what configures the WinRM
# listener; the autounattend can only open the firewall for it. See
# infra/windows/golden-local/main.ps1 and infra/incus-windows-pack.sh.
LOCAL="${PROJECT_ROOT}/infra/windows/golden-local"
IMAGE_ALIAS="${ONTRAK_IMAGE_ALIAS:-ontrak-win-base}"
PROJECT="${ONTRAK_INCUS_PROJECT:-ontrak}"
NETWORK="${ONTRAK_NETWORK:-ontrak0}"
BUILD_VM="${ONTRAK_BUILD_VM:-ontrak-golden-build}"
# incus-windows' *own* build VM (tools/pack.sh), which is not the same thing as
# $BUILD_VM above: that one is this script's, cloned later from the published image
# to apply post-install.ps1. This one is created and named inside the third-party
# checkout, so this script pins its name there and keeps it when a build fails.
PACK_VM="${ONTRAK_PACK_VM:-ontrak-winpack-build}"
PY="${PROJECT_ROOT}/.venv/bin/python"

# The rewrites this range makes to incus-windows' tools/pack.sh. They live in
# their own file so a test can run them without a host (see
# scripts/tests/test_pack_sh_rewrites.py), and each one is idempotent, because a
# failed build is re-run against the same checkout.
# shellcheck source=incus-windows-pack.sh
. "${PROJECT_ROOT}/infra/incus-windows-pack.sh"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# The name is ONTRAK_GUEST__PASSWORD — `ONTRAK_<SECTION>__<KEY>`, the same one the
# config, the portal and scripts/secrets.sh use. A single underscore here asked
# for a variable nothing else sets, so an operator who did exactly what the error
# message said got the same error again on the next run.
[[ -n "${ONTRAK_GUEST__PASSWORD:-}" ]] \
  || die "set ONTRAK_GUEST__PASSWORD (the training account password) first — 'make golden' exports it from .env"
command -v incus >/dev/null || die "incus not found; run infra/bootstrap-host.sh first"
command -v xorriso >/dev/null || die "xorriso is required to repack the Windows ISO"
command -v genisoimage >/dev/null || die "genisoimage is required by incus for the agent config ISO"
[[ -x "$PY" ]] || die "python venv missing; run 'make setup' first"

# -------------------------------------------------------- host accelerator --
# Windows 11 Setup needs Secure Boot, and in OVMF Secure Boot means SMM. A host
# whose own virtualisation is *nested* may be unable to virtualise SMM at all. It
# was measured on nested AMD SVM (WSL2/Hyper-V on a Ryzen): the build VM enters
# ERROR seconds after launch and its qemu log ends with
#
#     KVM: entry failed, hardware error 0xffffffff
#     ... EIP=00008000 ... SMM=1 HLT=0
#
# Turning Secure Boot and the TPM off does not help -- OVMF uses SMM for its
# runtime services regardless -- and `-machine smm=off` does not fix it either: it
# trades the crash for a guest that spins at 100% CPU and never writes a sector. A
# legacy-BIOS build (CSM/SeaBIOS, no SMM at all) hangs the same way.
#
# A host like that can still run the guest: QEMU can emulate the CPU. That is the
# fallback here, and it is why this block no longer refuses. The decision lives in
# ontrak/qemu.py -- the same one the range's own templates and student sessions ask
# for -- and infra/qemu-accel.sh is how a shell script asks it. The rewrite further
# down puts the answer on the build VM, which pack.sh creates, not this script.
#
# It is worth deciding up front because the build costs a 5 GiB ISO download and an
# hour before it fails, and worth being honest about because software emulation
# makes that hour several: the guest runs on an emulated CPU, and that is the only
# thing about it that changes. Q35, UEFI, SMM, the writable per-VM VARS store,
# Secure Boot and the TPM all stay exactly as they were.
HELPER="${PROJECT_ROOT}/infra/qemu-accel.sh"
ACCEL="$("$HELPER" --accel)"
export ONTRAK_QEMU_ACCEL="$ACCEL"
ADDRESS_ATTEMPTS="${ONTRAK_GOLDEN_ADDRESS_ATTEMPTS:-60}"
# How long to let the cloned image's first boot take before giving up on WinRM. It is
# not the session default: see the comment at the apply-postinstall.py call.
GOLDEN_READY_TIMEOUT="${ONTRAK_GOLDEN_READY_TIMEOUT:-1200}"
if [[ "$ACCEL" == tcg ]]; then
  if [[ "${ONTRAK_GOLDEN_REQUIRE_KVM:-}" == "1" ]]; then
    die "ONTRAK_GOLDEN_REQUIRE_KVM=1, and this host cannot give the guest KVM: $("$HELPER" --reason). Build the image where KVM is not nested -- bare-metal Linux, or a cloud VM with nested virtualisation -- then copy it in (see docs/operations.md, 'Building the golden image on a nested host'), or leave ONTRAK_GOLDEN_REQUIRE_KVM unset and let QEMU emulate the CPU."
  fi
  log "$("$HELPER" --reason)"
  warn "the build VM is emulated: the guest runs several times slower than it would on a host with KVM"
  warn "  This is the host, not the image. Nothing about the guest is reduced for it."
  warn "  Expect the install to take hours, and leave the host to it."
  # The preflight is a run rather than a package list because the software path
  # needs two things Incus does not always pull in: OVMF for the guest's firmware
  # and swtpm for its TPM 2.0. Both are *detected*, not named -- Incus ships its own
  # copies under /opt/incus and the ovmf package keeps the distribution's under
  # /usr/share, so hard-coding either path is a host that works here and not there.
  "$HELPER" --check >/dev/null \
    || die "this host is missing something the software-emulation path needs (named above), or set ONTRAK_QEMU_ACCEL=kvm to try KVM anyway"
  # The build VM is created inside the checkout's pack.sh, which this range
  # rewrites; exporting the helper is what makes that rewrite do anything at all.
  export ONTRAK_QEMU_ACCEL_HELPER="$HELPER"
  # pack.sh never passes --project, so its build VM lands in the default project.
  export ONTRAK_QEMU_ACCEL_PROJECT="default"
  # A boot that took three minutes under KVM can take much longer when the CPU is
  # emulated, and this loop is the first place the build would otherwise give up.
  ADDRESS_ATTEMPTS="${ONTRAK_GOLDEN_ADDRESS_ATTEMPTS:-240}"
fi

# ------------------------------------------------ automatic updates preflight --
# The build takes hours, and it is not the only thing the host is doing. On stock
# Ubuntu, unattended-upgrades is armed and apt-daily.timer /
# apt-daily-upgrade.timer are enabled; an upgrade that touches incus, qemu or
# libvirt restarts the daemon underneath the build.
#
# Not hypothetical: it is what ended this project's first build on i2. The VM had
# applied Windows and booted it through three setup passes when an unattended
# `apt-get --only-upgrade` of qemu and ovmf landed at 00:57. `systemctl stop
# incus` followed four seconds later, click.py's next poll of `incus ls` failed,
# and pack.sh's EXIT trap deleted the instance. Nothing had gone wrong with
# Windows.
#
# The disk no longer dies with the daemon — that trap is removed below, and a
# failed build keeps its VM — so this is a warning rather than a refusal: the run
# can still be rescued by publishing what was kept. But a restart costs the hours
# already spent, so name the timers and let the operator stop them.
if command -v systemctl >/dev/null; then
  armed=()
  for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null || systemctl is-active --quiet "$unit" 2>/dev/null; then
      armed+=("$unit")
    fi
  done
  if [[ ${#armed[@]} -gt 0 ]]; then
    warn "this host has automatic updates armed: ${armed[*]}"
    warn "  An upgrade that touches incus, qemu or libvirt restarts them and ends this run."
    warn "  Stop them for the length of the build (they come back at the next boot):"
    warn "    systemctl stop ${armed[*]}"
    warn "  A restart no longer destroys the installed disk — see docs/operations.md,"
    warn "  'Recovering a build VM that was kept' — but it does cost the hours so far."
  fi
fi

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
# The builder sizes its own build VM at 4 vCPU / 8 GB (tools/pack.sh), which on a
# four-core range host is *every* core it has: Windows Setup then starves sshd and
# the portal, and the host is unreachable for hours with the build still running.
# That is measured, not feared. The rewrite, and the rest of the reasoning, is in
# infra/incus-windows-pack.sh.
BUILD_CPUS="${ONTRAK_GOLDEN_CPUS:-2}"
BUILD_MEMORY="${ONTRAK_GOLDEN_MEMORY:-6GB}"
if pack_vm_size "$CHECKOUT/tools/pack.sh" "$BUILD_CPUS" "$BUILD_MEMORY"; then
  log "build VM sized at ${BUILD_CPUS} vCPU / ${BUILD_MEMORY} (ONTRAK_GOLDEN_CPUS / ONTRAK_GOLDEN_MEMORY)"
else
  warn "could not size the build VM: tools/pack.sh no longer matches (it is a pinned third-party checkout, so this is a change to re-read) — it will take the host's defaults"
fi

# ------------------------------------------------- build disk write cache --
# qemu's writeback default charges every block the guest writes to the host page
# cache *and* to the container's memory cgroup. Applying the ~7 GiB Windows image
# that way had the kernel OOM-kill qemu mid-apply, and it was silent: click.py
# reads the kill as a finished install, and pack.sh publishes the half-applied
# disk as if it were a golden image. infra/incus-windows-pack.sh has the rest, and
# the test that keeps the rewrite applying exactly once.
if pack_disk_cache_none "$CHECKOUT/tools/pack.sh"; then
  log "build disk set to io.cache=none (keeps the Windows image apply out of the host page cache)"
else
  warn "could not set io.cache=none on the build disk: tools/pack.sh no longer matches — on a 16 GiB range host, expect the OOM kill described above"
fi

# ---------------------------------------------------- build disk (and image) ----
# pack.sh creates the build disk at 30 GiB and grows it to 60 GiB before the
# install, and `incus publish` tars the *apparent* disk into the image — 60 GiB
# costs fourteen minutes of publishing and then wedges Incus's own database. A
# Windows 11 install needs ~20 GiB, so 32 GiB of headroom halves the image. The
# size is applied where the disk is created and the later resize is deleted rather
# than called, because growing a volume is the operation that blocks forever on a
# pool with inconsistent qgroups. The measurements are in infra/incus-windows-pack.sh.
BUILD_DISK="${ONTRAK_GOLDEN_DISK:-32GiB}"
if pack_disk_size "$CHECKOUT/tools/pack.sh" "$BUILD_DISK"; then
  log "build disk sized at ${BUILD_DISK} when it is created (ONTRAK_GOLDEN_DISK) — this is also the published image's disk"
else
  warn "could not size the build disk: tools/pack.sh no longer matches — expect a 60 GiB image, a long publish, and possibly a wedged Incus database"
fi

# --------------------------------------------- what keeps the installed disk --
# pack.sh names its build VM with six random bytes and deletes it from an
# unconditional EXIT trap, so *any* exit — including an unrelated `apt` upgrade
# restarting incus — throws away a disk that may hold three hours of Windows.
# That is measured: it is what ended this project's first build on i2. So the name
# is pinned, the delete is removed, and the VM is left in place for an operator to
# publish by hand; this script removes it once the image has really been imported.
# infra/incus-windows-pack.sh carries the account of the incident.
if pack_qemu_accel "$CHECKOUT/tools/pack.sh"; then
  log "build VM will ask for this host's accelerator (KVM, or QEMU TCG where KVM cannot run it)"
else
  warn "could not add the accelerator hook to tools/pack.sh: it no longer matches — on a host that cannot virtualise SMM the build VM will die at 'KVM: entry failed' seconds after it starts"
fi

if pack_keep_vm "$CHECKOUT/tools/pack.sh" "$PACK_VM"; then
  log "build VM pinned to ${PACK_VM}, and kept until the image is imported (ONTRAK_PACK_VM)"
else
  warn "could not pin the build VM in tools/pack.sh: it no longer matches — a failed build will delete the installed disk after all, and any leftover VM will have a random name"
fi

# ------------------------------------- the guest must be reachable at all ---- #
# A Windows image nobody can reach cannot be provisioned, and this range
# provisions over WinRM. The image incus-windows builds does have WinRM enabled —
# but only for the Domain and Private firewall profiles, and a clone lands on
# whatever profile Windows decides for the lab bridge, which on every host measured
# here is **Public**. Every inbound packet is then dropped before anything can
# connect: the guest has an address, it answers ARP, and every port times out with
# no RST — 445 and 135 included, which Windows always answers when a SYN gets
# through. Only the empty qemu log distinguishes it from the SMM fault further up.
#
# This is the lock on the door with the key behind it. `post-install.ps1` (applied
# after the image is imported, below) opens WinRM for every profile — but it is run
# *over WinRM, against a clone of the image*, so it can only repair a guest that is
# already reachable. The fix has to be in the install itself, where no network is
# involved: a declarative firewall group in the autounattend, plus
# infra/windows/golden-local/main.ps1, which runs from the unattended ISO at first
# logon and configures the listener. infra/incus-windows-pack.sh has the account of
# it — including why the listener half is a file on the ISO rather than a command in
# the answer file — and scripts/tests/test_unattend_winrm.py keeps it applying.
if pack_unattend_winrm "$UNATTEND"; then
  log "autounattend: Windows Remote Management opened on every firewall profile (the image is reachable on the lab bridge)"
else
  warn "could not open WinRM on every firewall profile in $UNATTEND: the unattend no longer matches."
  warn "  The image will still build, and then be unreachable — the build stops at"
  warn "  'applying post-install.ps1 over WinRM', with every port timing out and no RST."
  warn "  Re-read infra/incus-windows-pack.sh against the pinned checkout."
fi

# --------------------------------------- no-Secure-Boot golden images -------- #
# Windows 11 Setup refuses to install unless Secure Boot (and a TPM) are present.
# ONTRAK_GOLDEN_NO_SECUREBOOT=1 lifts that: the build VM gets no TPM and no Secure
# Boot, and the Setup gate is satisfied with the standard LabConfig bypasses, so the
# image installs on a host that cannot offer a TPM. Range VMs clone with
# secureboot=false anyway, so they are unaffected -- it is opt-in because the image
# is installed past the gate Windows 11 normally insists on.
#
# Note this does NOT rescue a host that cannot virtualise SMM: with Secure Boot off,
# OVMF still uses SMM for its runtime services, so the nested-AMD host the preflight
# above refuses still fails. That is checked before the build, not here.
if [[ "${ONTRAK_GOLDEN_NO_SECUREBOOT:-}" =~ ^(1|true|yes|on)$ ]]; then
  warn "ONTRAK_GOLDEN_NO_SECUREBOOT set: building with Secure Boot and TPM OFF."
  warn "  The image installs past the Windows 11 TPM/Secure Boot gate, so it is not"
  warn "  a production golden image."

  # 1. Keep pack.sh from giving the build VM a TPM and Secure Boot. Off means off:
  #    no TPM device, no later security.secureboot=true. The secureboot=false that
  #    upstream puts on `incus init` is its own default and is left alone;
  #    infra/incus-windows-pack.sh has both edits.
  if pack_no_secureboot "$CHECKOUT/tools/pack.sh"; then
    log "build VM set to run without TPM and without Secure Boot"
  else
    warn "could not disable TPM/Secure Boot in tools/pack.sh: it no longer matches — the build will still need SMM"
  fi

  # 2. Let Windows Setup install past the checks it can no longer satisfy.
  if [[ -f "$UNATTEND" ]] && ! grep -q 'ONTRAK_NO_SECUREBOOT_BEGIN' "$UNATTEND"; then
    "$PY" - "$UNATTEND" <<'PYEOF'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    s = fh.read()

# The end of the Microsoft-Windows-Setup component in the windowsPE pass -- the
# only place a RunSynchronous runs early enough to beat the TPM/Secure Boot check.
anchor = (
    "        <Organization>Vagrant</Organization>\n"
    "      </UserData>\n"
    "    </component>"
)
if anchor not in s:
    sys.exit("autounattend does not match: the Microsoft-Windows-Setup component moved")

checks = [
    ("TPM", "BypassTPMCheck"),
    ("Secure Boot", "BypassSecureBootCheck"),
    ("RAM", "BypassRAMCheck"),
    ("CPU", "BypassCPUCheck"),
    ("storage", "BypassStorageCheck"),
]
lines = [
    "        <Organization>Vagrant</Organization>",
    "      </UserData>",
    "      <!-- ONTRAK_NO_SECUREBOOT_BEGIN -->",
    "      <RunSynchronous>",
]
for order, (label, key) in enumerate(checks, start=1):
    lines += [
        '        <RunSynchronousCommand wcm:action="add">',
        f"          <Order>{order}</Order>",
        f"          <Description>Bypass {label} check</Description>",
        f'          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {key} /t REG_DWORD /d 1 /f</Path>',
        "        </RunSynchronousCommand>",
    ]
lines += [
    "      </RunSynchronous>",
    "      <!-- ONTRAK_NO_SECUREBOOT_END -->",
    "    </component>",
]

with open(path, "w", encoding="utf-8") as fh:
    fh.write(s.replace(anchor, "\n".join(lines), 1))
PYEOF
    if grep -q 'ONTRAK_NO_SECUREBOOT_BEGIN' "$UNATTEND"; then
      log "Windows Setup TPM/Secure Boot checks bypassed in $(basename "$UNATTEND")"
    else
      warn "could not inject the Setup bypass: the installer may still stop on the TPM/Secure Boot check"
    fi
  fi
fi

# ------------------------------------------------------- the last build VM ----
# A build VM from an earlier failed run can hold a fully installed Windows disk.
# Do not quietly throw that away -- say so, and make the operator choose.
# pack.sh publishes under the same name it gives the VM, so a leftover image alias
# of that name breaks a re-run just as a leftover VM does.
if incus info "$PACK_VM" >/dev/null 2>&1 || incus image info "$PACK_VM" >/dev/null 2>&1; then
  if [[ "${ONTRAK_DISCARD_BUILD_VM:-}" == "1" ]]; then
    warn "discarding the leftover build VM and image $PACK_VM (ONTRAK_DISCARD_BUILD_VM=1)"
    incus delete "$PACK_VM" --force 2>/dev/null || :
    incus image rm "$PACK_VM" 2>/dev/null || :
  else
    die "there is already a build VM or image named $PACK_VM. A failed build keeps its VM on purpose, because it may hold an installed Windows disk -- see docs/operations.md, 'Recovering a build VM that was kept'. Remove it (incus delete $PACK_VM --force; incus image rm $PACK_VM) or re-run with ONTRAK_DISCARD_BUILD_VM=1 to discard it."
  fi
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
# The second argument is pack.sh's optional `local/` directory: build.sh forwards
# any argument after the target straight to it ("${@:+"${@}"}"), and pack.sh copies
# it onto the unattended ISO as local/, where upstream's OEM/main.ps1 dot-sources
# local/main.ps1 at first logon. Nothing else in this repository can put a file
# inside the guest before the image is sealed, and Windows Setup will not accept the
# work as an autounattend value -- see infra/incus-windows-pack.sh.
sh build.sh "$TARGET" "$LOCAL"

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
#     see it. Import into the project the templates are built in.
IMPORT_ALIAS="win${TARGET}"
OUTDIR="./output/${IMPORT_ALIAS}"
[[ -f "${LOCAL}/main.ps1" ]] \
  || die "the first-logon script is missing: expected ${LOCAL}/main.ps1, which tools/pack.sh puts on the unattended ISO as local/. Without it the built image has no WinRM listener and the range cannot provision it."
[[ -f "$OUTDIR/incus.tar.xz" ]] || OUTDIR="./output/${TARGET}"
[[ -f "$OUTDIR/incus.tar.xz" && -f "$OUTDIR/disk.qcow2" ]] \
  || die "the build produced no image: expected incus.tar.xz and disk.qcow2 under ./output/${IMPORT_ALIAS} (a stale directory there makes build.sh bail out early -- remove it and retry). The build VM ${PACK_VM} has been kept -- see docs/operations.md, 'Recovering a build VM that was kept'."
log "importing the built image into Incus (project $PROJECT)"
incus --project "$PROJECT" image import "$OUTDIR/incus.tar.xz" "$OUTDIR/disk.qcow2" \
  --alias "$IMPORT_ALIAS"

# Adopt it: clear the one property upstream stamps on every image it builds, and
# which nothing in this range can satisfy.
#
# tools/pack.sh publishes its build VM with `requirements.cdrom_agent=true`,
# `incus publish` copies image properties, and the property also travels in the
# export's metadata.yaml — so it is on the image import just brought in, and
# importing *without* the flag does not remove it (the metadata declares it, so the
# flag was never what put it there).
#
# It means "every instance made from this image must have an `agent:config` disk",
# and Incus enforces it at *start* rather than at create. The very next step here
# creates a VM from this image to apply post-install.ps1 — so without this the
# golden build dies, after the whole two-hour install, with
#
#     Error: This virtual machine image requires an agent:config disk be added
#
# It is also what stopped every Windows template build; infra/import-golden-image.sh
# carries the fuller account, including why the opt-in `incus-exec` driver wants the
# requirement back, and why `incus image unset-property` (which panics in Incus 7.5.1)
# is not the tool for this.
incus --project "$PROJECT" image set-property "$IMPORT_ALIAS" requirements.cdrom_agent=""

# The image is safely imported, so the build VM has done its job. It was kept until
# exactly this point on purpose: pack.sh used to delete it on any failure, which is
# how an installed Windows disk gets thrown away for no reason.
if incus info "$PACK_VM" >/dev/null 2>&1; then
  log "removing the build VM $PACK_VM now that its image has been imported"
  incus delete "$PACK_VM" --force
fi
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

log "waiting for an address (up to $((ADDRESS_ATTEMPTS * 5))s)"
IP=""
for _ in $(seq 1 "$ADDRESS_ATTEMPTS"); do
  IP="$(incus --project "$PROJECT" list "$BUILD_VM" --format=csv -c 4 | cut -d' ' -f1)"
  [[ -n "$IP" ]] && break
  sleep 5
done
[[ -n "$IP" ]] || { incus --project "$PROJECT" list "$BUILD_VM"; die "the build VM never got an address"; }
log "build VM address: $IP"

# This step needs a longer wait than a session does, and the difference is measured
# rather than defensive. A generalized Windows image answers nothing until its first
# boot has finished the specialize pass: SetupComplete.cmd -- which re-enables the
# WinRM rules sysprep.bat blocked before the image was sealed -- is the last thing
# that pass runs. `guest.ready_timeout_seconds` (420s) is sized for a session joining
# a guest that is already up, and here it expired on a clone whose address appeared
# at 20:26 and whose 5985 opened at 20:38: the image was correct, the budget was not,
# and the build failed one step from the end. ONTRAK_GOLDEN_READY_TIMEOUT changes it;
# ONTRAK_GUEST__READY_TIMEOUT_SECONDS, the setting itself, still wins over both.
log "applying post-install.ps1 over WinRM (allowing $((GOLDEN_READY_TIMEOUT / 60))m for the first boot to finish)"
ONTRAK_GUEST__READY_TIMEOUT_SECONDS="${ONTRAK_GUEST__READY_TIMEOUT_SECONDS:-$GOLDEN_READY_TIMEOUT}" \
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

# Clear the property again, on *this* alias, because this is the one the range
# clones from and `incus publish` has just put it back.
#
# The clear above is on the image that was imported, and on its own it is not
# enough. Measured on a host that had just built this image: create a build VM from
# an image whose `requirements.cdrom_agent` is cleared, publish that VM, and the
# resulting image carries `requirements.cdrom_agent: true` again. Nothing in this
# range attaches an `agent:config` disk -- OnTrak drives Windows over WinRM -- and
# Incus enforces the requirement at *start*, so without this line the build reports
# success and every clone of it fails with
#
#     Error: This virtual machine image requires an agent:config disk be added
#
# which is every Windows template and every Windows session. Same command and same
# reason as the clear above; infra/import-golden-image.sh does the same for an image
# it imports from somewhere else.
incus --project "$PROJECT" image set-property "$IMAGE_ALIAS" requirements.cdrom_agent=""

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
