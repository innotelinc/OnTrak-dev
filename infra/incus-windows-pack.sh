#!/usr/bin/env bash
#
# The edits this range makes to the pinned incus-windows checkout: its
# tools/pack.sh, and the autounattend it installs Windows from.
#
# `infra/build-golden-image.sh` sources this file; nothing else should. It is
# separate for one reason: these are the only part of the golden build a test can
# reach. Upstream pack.sh cannot be run without incus, Windows evaluation media
# and two hours, so a rewrite that quietly stops matching — because the pinned
# third-party checkout moved — is otherwise discovered on a host, after the
# build, in the middle of the night. In a file of their own they can be run
# against a fixture: see scripts/tests/test_pack_sh_rewrites.py and
# scripts/tests/test_unattend_winrm.py.
#
# Each function takes the path to a pack.sh as its first argument and returns 0
# when the file is in the shape this range needs, non-zero when its anchor is
# gone (upstream moved and the rewrite did nothing). The caller decides what a
# non-zero means; in practice it is a warning naming what to re-read, because a
# build with upstream's defaults is slow and fragile rather than impossible.
#
# Idempotence is a contract, not a nicety: a build that failed is re-run against
# the same checkout, so "already patched" has to be a success. It was not always
# one — `io.cache=none` was appended on every run at first, and a checkout that
# had been patched four times carried the line five times. So every rewrite here
# either checks before inserting, or substitutes something it can substitute
# again, and the tests apply each one twice and compare.
#
# Line numbers are from tools/pack.sh at upstream ae3b612, the commit the local
# checkout is on; they are a reading aid, nothing depends on them.

# Build VM sizing — upstream line 84.
#
# pack.sh creates its build VM at 4 vCPU / 8 GB. On a four-core range host that
# is *every* core it has, and Windows Setup uses them: the host keeps answering
# ping and accepting TCP while nothing in user space is scheduled, so sshd and
# the portal never reply and the lab is down for the length of the build. That
# was measured, not feared — this script did it to a 4 vCPU host, which was
# unreachable for hours with the build still running.
pack_vm_size() {
  local file="$1" cpus="$2" memory="$3"
  sed -i -E "s/-c limits\\.cpu=[0-9]+ -c limits\\.memory=[0-9]+G?B?/-c limits.cpu=${cpus} -c limits.memory=${memory}/" "$file"
  grep -q -- "-c limits.cpu=${cpus} -c limits.memory=${memory}" "$file"
}

# The build disk's write cache — upstream line 85.
#
# Incus hands a VM disk to qemu with the "writeback" cache by default, so every
# block the guest writes is charged to the host page cache *and* to the
# container's memory cgroup. Applying the ~7 GiB Windows image that way is what
# made the first three builds fail: the cgroup reached its limit and the kernel
# OOM-killed qemu in the middle of the apply.
#
# The failure then hides itself. tools/click.py waits for `incus ls` to report
# STOPPED and reads that as "the installer finished" — it cannot tell a clean
# sysprep shutdown from a killed process — so pack.sh goes on to publish and
# export the half-applied disk as if it were a golden image. A qemu kill is
# therefore silent: you get an image whose ESP has no Windows boot files, and
# every Windows template built from it fails much later. (scripts/verify-golden-image.py
# is the check that catches it at import time.)
pack_disk_cache_none() {
  local file="$1"
  if ! grep -q 'root io.cache=none' "$file"; then
    sed -i '/^incus config device set "${name}" root io.bus=virtio-blk$/a incus config device set "${name}" root io.cache=none' "$file"
  fi
  grep -q 'root io.cache=none' "$file"
}

# The build disk's size — upstream lines 84 and 97.
#
# pack.sh creates the disk at 30 GiB and then grows it to 60 GiB before the
# install. That size is not only the build VM's: `incus publish` tars the
# *apparent* disk into the image — it does not skip holes, and the copy is gzip's
# input in any case.
#
# What 60 GiB costs was measured. Publishing it with --compression none wrote for
# fourteen minutes and then failed with "Failed to begin transaction: context
# deadline exceeded" — the image store and Incus's own cowsql database share one
# dataset, and the copy starves the database's leader election. The daemon's DB is
# left unusable, every `incus` command times out, and the fully-installed Windows
# disk has to be recovered by hand. It happened twice. A Windows 11 install needs
# ~20 GiB, so 32 GiB is plenty of headroom and the published image is half the
# size.
#
# The size is applied where the disk is *created*, and the later resize is
# deleted rather than called. Growing an existing volume is a separate path in
# the daemon with its own way of going wrong: on a btrfs pool whose qgroups are
# flagged inconsistent, `incus config device set <vm> root size=...` blocks inside
# the daemon forever — the operation is not cancelable — and the build parks at
# "Device tpm added" for as long as you leave it. Sizing at creation takes the
# path that has always worked.
pack_disk_size() {
  local file="$1" size="$2"
  sed -i -E "s/-d root,size=[0-9]+GiB/-d root,size=${size}/" "$file"
  sed -i -E "/^[[:space:]]*incus config device set .* root size=[0-9]+GiB[[:space:]]*$/d" "$file"
  grep -q -- "root,size=${size}" "$file"
}

# The build VM's name, and what happens to it on the way out — upstream lines 39-44.
#
# pack.sh names its build VM with six random bytes and deletes it from an EXIT
# trap:
#
#     name=build$(head -c6 /dev/urandom | ...)
#     cleanup() { incus image rm "${name}" || :; incus delete -f "${name}"; }
#     trap cleanup EXIT INT QUIT TERM
#
# Both halves are a problem for a build this long. The random name means that
# after a failure there is no name to hand the operator; the unconditional trap
# means *any* exit deletes the disk, including exits that have nothing to do with
# Windows.
#
# That was measured the expensive way. A build that had applied Windows to disk
# and booted it through three setup passes was destroyed after roughly three
# hours because an unrelated `apt` upgrade on the same host restarted incus;
# click.py's next poll of `incus ls` failed, and pack.sh's trap deleted the
# instance on the way out. Nothing had gone wrong with Windows.
#
# So the name is pinned and the delete is removed. The VM is then left in place
# whenever the build fails, where its disk can be published by hand
# (docs/operations.md, "Recovering a build VM that was kept"), and
# build-golden-image.sh removes it itself once the image has really been imported.
# `incus image rm` is left alone: pack.sh publishes under the same name it gives
# the VM, so the image it removes is its own, and the export it writes is what
# this range imports.
pack_keep_vm() {
  local file="$1" name="$2"
  sed -i -E "s|^name=build.*|name=${name}|" "$file"
  sed -i -E '/^[[:space:]]*incus delete -f /d' "$file"
  grep -q "^name=${name}$" "$file" && ! grep -qE '^[[:space:]]*incus delete -f ' "$file"
}

# Secure Boot and the TPM, off — upstream lines 96 and 101.
#
# Windows 11 Setup refuses to install unless Secure Boot and a TPM are present.
# ONTRAK_GOLDEN_NO_SECUREBOOT lifts that on the build VM only, for a lab that
# wants a no-Secure-Boot image or a host that cannot offer a TPM; range VMs clone
# with secureboot=false anyway, so they are unaffected. The Setup gate is then
# satisfied with the standard LabConfig bypasses, which build-golden-image.sh
# injects into the autounattend.
#
# The line that sets `-c security.secureboot=false` on `incus init` is upstream's
# own default and is deliberately left alone: only the *later* enable is
# commented out. Both edits are substitutes, so applying them twice is a no-op.
pack_no_secureboot() {
  local file="$1"
  sed -i -E 's|^([[:space:]]*)incus config device add .* tpm tpm[[:space:]]*$|\1# ONTRAK_NO_SECUREBOOT: no TPM on the build VM|' "$file"
  sed -i -E 's|^([[:space:]]*)incus config set .* security\.secureboot=true[[:space:]]*$|\1# ONTRAK_NO_SECUREBOOT: no Secure Boot on the build VM|' "$file"
  ! grep -qE '^[[:space:]]*incus config (device add .* tpm tpm|set .* security\.secureboot=true)' "$file"
}

# The accelerator the build VM starts under — upstream line 107, the `click.py`
# call that hands the VM to Windows Setup.
#
# This is the one host difference the build cannot express through a config the
# operator sets once, because the offending config is written by incusd at start
# time and there is no setting that changes it. For a VM, incusd always ends up
# with `-cpu host,hv_passthrough` on the QEMU command line and `[machine] accel =
# "kvm"` in the config file it passes to `-readconfig`. Both are wrong on a host
# whose own virtualisation is nested on AMD, and both fail loudly rather than
# slowly: Windows 11 needs Secure Boot, in OVMF Secure Boot means SMM, and nested
# AMD SVM cannot virtualise SMM. The build VM then goes to ERROR ten to twenty
# seconds after it starts, and its qemu log ends
#
#     KVM: entry failed, hardware error 0xffffffff
#     ... EIP=00008000 ... SMM=1 HLT=0
#
# Turning Secure Boot and the TPM off does not help (OVMF uses SMM for its runtime
# services regardless), `-machine smm=off` only trades the crash for a guest that
# spins without writing a sector, and a legacy-BIOS build hangs the same way — so
# the answer is not a lesser VM but QEMU's software emulator:
#
#     [machine]
#     accel = "tcg"
#
# plus `-cpu max`, because `-cpu host` cannot run without KVM
# ("CPU model 'host' requires KVM or HVF").
#
# The rewrite adds a *hook*, not the settings. The decision — is this host's KVM
# one that can run the guest — and the two values belong to ontrak/qemu.py, because
# the range's own templates and student sessions need exactly the same answer and
# two implementations of it would eventually disagree. All this does is call it,
# once, at the last moment before Windows starts.
#
# Nothing happens unless the build exported the helper, and it only does that when
# it has decided this host needs the fallback. On a host where KVM works, pack.sh
# behaves exactly as it did. If the call does fail, the build should stop: the
# guest is about to be started on an accelerator that cannot run it, and a clear
# error here is worth more than the qemu log twenty seconds later.
pack_qemu_accel() {
  local file="$1"
  if ! grep -q 'ONTRAK_QEMU_ACCEL_BEGIN' "$file" 2>/dev/null; then
    local block
    block="$(pack_qemu_accel_block)"
    # Inserted immediately before `click.py`: by then every device, every raw
    # argument and the Secure Boot/TPM settings above it are in place, and this is
    # the last point at which the VM is still just a stored configuration.
    awk -v block="$block" '
      !inserted && /click\.py/ { print block; inserted = 1 }
      { print }
    ' "$file" > "${file}.ontrak-qemu-accel" && mv "${file}.ontrak-qemu-accel" "$file"
  fi
  grep -q 'ONTRAK_QEMU_ACCEL_BEGIN' "$file" && grep -q 'ONTRAK_QEMU_ACCEL_END' "$file"
}

# WinRM reachable on whatever network the guest lands on — a firewall group in the
# autounattend, and one script on the unattended ISO.
#
# The image incus-windows builds has WinRM installed and enabled, but its firewall
# rules are only *active* on the Domain and Private profiles: `Enable-PSRemoting`
# turns the Windows Remote Management group on for the profile the machine is on
# when it runs, and the install runs on whatever the build host's bridge is.
# A clone that comes up on a network Windows classifies as **Public** then drops
# every inbound packet. Nothing looks broken from either end: the guest has an
# address, it answers ARP, and every port times out *without a RST* — including
# 135 and 445, which Windows always answers when the SYN gets through the firewall
# — while its qemu log stays empty. Only that last detail is different from the
# SMM fault this range also knows about, and confusing the two costs an afternoon.
#
# That is not hypothetical. It is what stopped every Windows template build on
# this range, and it is why the golden image could be built, published and
# imported and still never be usable. `infra/windows/post-install.ps1` fixes
# exactly this — it runs `Enable-PSRemoting -SkipNetworkProfileCheck` — but the
# build applies that script *over WinRM, to a clone of the image*. So it can only
# repair a guest that is already reachable, and nothing in a fresh image is.
# The step that would open the door is locked behind the door.
#
# The fix therefore belongs in the install, where no network is involved, and it is
# two parts:
#
#   * this rewrite adds a declarative firewall group for the Windows Remote
#     Management rules, active on every profile — exactly the mechanism upstream
#     already uses for Remote Desktop a few lines above it. Being declarative, it
#     cannot fail at run time.
#   * `infra/windows/golden-local/main.ps1` runs at first logon, from the ISO, and
#     does what a firewall rule cannot: `Enable-PSRemoting
#     -SkipNetworkProfileCheck` plus explicit 5985/3389 rules for `-Profile Any`,
#     which also guarantees the *listener* exists, in case the image had the
#     service installed but never configured. Upstream's own `OEM/main.ps1`
#     dot-sources `${setupdrive}\local\main.ps1` when the ISO carries it, and
#     tools/pack.sh copies the directory given as its optional seventh argument to
#     `local/` — so that file is upstream's supported extension point, not a patch
#     to upstream. `infra/build-golden-image.sh` passes it.
#
# Why the second part is a file and not a second `RunSynchronousCommand`, which is
# what this started as: the `Path` of a RunSynchronousCommand is limited, and an
# over-long one does not fail loudly — it invalidates the whole answer file. A
# 490-character one-liner was rejected in the specialize pass with
#
#     [setup.exe] SMI data results dump: Source = Name: Microsoft-Windows-Deployment,
#       ... /settings/RunSynchronous/RunSynchronousCommand/[Order="4"]/Path
#     [setup.exe] SMI data results dump: Description = Value is invalid.
#     Error [0x060432] IBS  The provided unattend file is not valid; hrResult = 0x80220005
#     Windows could not parse or process unattend answer file
#       [C:\\WINDOWS\\Panther\\unattend.xml] for pass [specialize]. The answer file is invalid.
#
# and Setup then *blocked the installation*. There is no reboot and no error anyone
# outside the guest can see: `unattendgc` is never written, sysprep never runs, and
# a half-installed Windows sits at 0.6 of a core forever — a disk that has stopped
# changing is the only clue. tools/click.py waits for the VM to reach STOPPED and
# has no wall-clock limit, so the build waits with it. Upstream's own commands on
# that pass are around 100 characters and are fine. A file on the ISO has no such
# limit, which is why the long one lives there.
#
# This rewrite anchors on text this range does not otherwise touch, and is a no-op
# the second time. The image it ships in has to be rebuilt for the fix to reach a
# host; see docs/operations.md.
pack_unattend_winrm() {
  local file="$1"

  # Undo the command an earlier version of this rewrite injected. That is not a
  # nicety: the checkout is *not* reset between builds -- build-golden-image.sh runs
  # `git checkout <ref>`, which keeps local edits to tracked files -- so a checkout
  # that has built once still carries it, and leaving it there blocks every later
  # build in the specialize pass exactly as it blocked that one. It is why this
  # rewrite has to converge on a shape rather than only add to one.
  if grep -q 'ONTRAK_WINRM_COMMAND' "$file" 2>/dev/null; then
    if awk '
      /ONTRAK_WINRM_COMMAND/ { skip = 1 }
      skip { if (/<\/RunSynchronousCommand>/) { skip = 0 }; next }
      { print }
      END { exit (skip ? 2 : 0) }
    ' "$file" > "${file}.ontrak-winrm-unpatch"; then
      mv "${file}.ontrak-winrm-unpatch" "$file"
    else
      # A marker with no closing command after it. Leave the file alone rather than
      # truncate it, and report the anchor as moved.
      rm -f "${file}.ontrak-winrm-unpatch"
      return 1
    fi
  fi

  if ! grep -q 'keyValue="WindowsRemoteManagement"' "$file" 2>/dev/null; then
    awk -v block="$(pack_unattend_firewall_group)" '
      !done && /^[[:space:]]*<\/FirewallGroups>/ { print block; done = 1 }
      { print }
    ' "$file" > "${file}.ontrak-winrm" && mv "${file}.ontrak-winrm" "$file"
  fi
  grep -q 'keyValue="WindowsRemoteManagement"' "$file"
}

# The Windows Remote Management rules, on every profile, the way upstream already
# does Remote Desktop. Eight spaces of indent to match its neighbours.
pack_unattend_firewall_group() {
  cat <<'ONTRAK'
        <!-- ONTRAK_WINRM_GROUP — see infra/incus-windows-pack.sh -->
        <FirewallGroup wcm:action="add" wcm:keyValue="WindowsRemoteManagement">
          <Active>true</Active>
          <Group>Windows Remote Management</Group>
          <Profile>all</Profile>
        </FirewallGroup>
ONTRAK
}

# The listener half of the fix used to be a RunSynchronousCommand here. It is now
# `infra/windows/golden-local/main.ps1`, which runs from the ISO at first logon; the
# comment above the rewrite says why it could not stay an answer-file value, and
# scripts/tests/test_unattend_winrm.py fails if anything puts a long one back.

# The hook itself, tab-indented the way pack.sh indents its shell blocks.
pack_qemu_accel_block() {
  cat <<'ONTRAK'
	# ONTRAK_QEMU_ACCEL_BEGIN — set by infra/build-golden-image.sh; see
	# infra/incus-windows-pack.sh. Empty helper means this host runs the guest on
	# KVM and nothing here applies.
	if [ -n "${ONTRAK_QEMU_ACCEL_HELPER:-}" ]; then
		"${ONTRAK_QEMU_ACCEL_HELPER}" --apply "${name}" "${ONTRAK_QEMU_ACCEL_PROJECT:-default}"
	fi
	# ONTRAK_QEMU_ACCEL_END
ONTRAK
}
