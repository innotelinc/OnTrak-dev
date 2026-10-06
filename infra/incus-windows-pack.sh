#!/usr/bin/env bash
#
# The edits this range makes to incus-windows' tools/pack.sh.
#
# `infra/build-golden-image.sh` sources this file; nothing else should. It is
# separate for one reason: these are the only part of the golden build a test can
# reach. Upstream pack.sh cannot be run without incus, Windows evaluation media
# and two hours, so a rewrite that quietly stops matching — because the pinned
# third-party checkout moved — is otherwise discovered on a host, after the
# build, in the middle of the night. In a file of their own they can be run
# against a fixture: see scripts/tests/test_pack_sh_rewrites.py.
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
