# The installer ISO

`infra/build-installer-iso.sh` builds a bootable image that installs Ubuntu Server
24.04 LTS and leaves it as a working range host: Incus, the lab bridge, the OnTrak
checkout and the portal stack. It is a remaster of Canonical's own live-server ISO —
not a hand-rolled installer — so firmware support, drivers and the installer itself
are whatever Ubuntu ships.

It offers two installs, and they differ in one thing: the target disk. The default
installs onto the machine's own disk, unattended. The second is the same install with
the installer's storage screen left up, so you choose the disk — which is how a USB
stick becomes a portable range host (["A portable range"](#a-portable-range-on-a-usb-stick)).

```bash
make installer-iso                  # → dist/ontrak-installer-24.04.5-amd64.iso
ONTRAK_ISO_SMOKE=1 make installer-iso-smoke   # ...and boot it in QEMU to prove it
```

## What the operator does, and what happens

Booting the image starts the Ubuntu installer, which stops on exactly one screen:
**identity**. That is the whole point of the design — the admin username, the
hostname, the password and an optional SSH key are answered at the console, so no
credential is baked into an image that gets copied around. Everything else is
already decided: locale, keyboard, DHCP on every NIC, LVM on the largest disk, the
SSH server, security updates.

The image is unattended apart from that screen — and the storage screen of the second
entry below, which is where that entry's target is chosen. It boots from BIOS, UEFI, a
USB stick and virtual media, because it carries the original El Torito, isohybrid MBR
and GPT boot equipment.

### Which console the installer answers on

Every entry carries `console=ttyS0` alongside the VGA console, so a machine with no
display can be installed over serial. It has a consequence worth knowing before you
stand in front of a monitor: **subiquity takes the serial console as its own when the
kernel offers one**, so its prompts are answered there and a keyboard on the machine's
own VGA console does not reach them. Measured on the built image: a keystroke on the
PS/2 keyboard leaves subiquity's first screen unchanged. Reordering the `console=`
arguments does not change it either — what triggers it is the serial console being on
the command line at all.

So:

* **At a monitor or a VM's graphical console**: press `e` on the entry, delete
  `console=ttyS0`, and boot. The installer then runs on the VGA console — which is the
  console `install-test.sh` drives.
* **On a machine you cannot sit at** (a rack host, or a VM with a serial console
  attached): leave the entry alone and answer it over serial (`ipmitool sol activate`,
  `virsh console`, or any serial terminal). Its first question is subiquity's own —
  basic or rich mode — and `Enter` takes the default.

### The menu, and what each entry does

Both entries run the same autoinstall and produce the same range host. They are
rendered from one template — `infra/installer/autoinstall/user-data.dist`, by
`infra/installer/render-autoinstall.py` — as two profiles, because everything about the
install except which screens stay up has to stay identical.

| Menu entry | Target | Stops on | Autoinstall |
| --- | --- | --- | --- |
| *Install OnTrak on this machine's disk (unattended)* | the largest disk, LVM | identity | `/nocloud/` |
| *Install OnTrak on the disk you choose (USB stick, or another disk)* | whichever disk you pick | identity, then storage | `/nocloud-choose-disk/` |

The first is grub's default, so an unanswered boot is still the bare-metal install and
nothing about that path changed. Both are offered for the standard and the HWE kernel,
which is what the `[HWE kernel]` suffix on a title marks.

`infra/installer/patch-grub.py` writes that menu into the grub.cfg Canonical ships,
and `scripts/tests/test_patch_grub.py` runs it against the menu this base image
actually carries — tabs, single-quoted titles, the `linux16` memory tester and the
`if [ "$grub_platform" = "efi" ]` branch around the UEFI entries included — so a
release that changes the shape of it fails a test rather than a machine at a console.

## A portable range, on a USB stick

The *disk you choose* entry is how a stick becomes a range host: write the ISO to a
stick, boot it, install onto the stick, then boot the stick. Four things are worth
knowing first.

* **There is no live mode, and there cannot be.** A server ISO has no live session —
the only thing that boots off it is the installer — so "run the range from the stick"
means an installed stick, not a live one. A live overlay would be the wrong shape
anyway: the Incus pool, the container runtime and PostgreSQL's state all want a real
filesystem under them, which is the same reason [`operations.md`](operations.md) gives
the pool a disk of its own.
* **That entry installs onto the medium it booted from**, so it boots `toram`: casper
copies the live media into RAM first, which frees the stick to be erased. It needs the
room for that copy — this image asks for about **4 GiB of free RAM** — and it degrades
gently rather than failing if it does not get it: the log says `Begin: Copying live_media
to ram … Not enough free memory (…) to copy live media in ram.` and the install carries
on from the medium. That is fine when the target is another disk, and *not* fine when the
target is the stick it booted from, so check for that line if a self-install goes wrong.
Installing from virtual media, or a second stick, onto the first avoids the question
entirely. The *machine's disk* entry is not a substitute: it takes the largest disk,
which on a laptop is the internal one.
* **The pool still wants a device of its own.** The storage screen is where you leave
room for it — partition the stick there and point
`ONTRAK_INCUS__STORAGE_POOL` and the profile at the partition — and the reason is the
measured one in
[operations.md](operations.md#put-the-pool-on-its-own-disk): a pool on `/` takes
PostgreSQL down with it when it fills, and the only symptom arrives through the portal
as "sign-in is not set up".
* **Use a USB SSD rather than a flash stick.** Templates are 20-30 GiB each and the
pool writes through them; flash makes a build an hour and wears out. A portable range
is also a small one — a student or two — so `dir` storage on the stick's pool
partition is a reasonable choice where copy-on-write would matter more, per the
capacity table in [operations.md](operations.md).

After the install the machine reboots, and the first boot is when the range is
built, from `infra/installer/firstboot/ontrak-firstboot.sh`:

| Step | What it does |
| --- | --- |
| packages | `git`, `docker.io`, `docker-compose-v2` — what the install itself does not need |
| checkout | clones `https://github.com/innotelinc/OnTrak.git` to `/opt/ontrak` |
| hypervisor | `infra/bootstrap-host.sh` — Incus, the `ontrak0` bridge, the project, the profiles |
| setup | `make setup` — venv, dependencies, guard hooks, `.env` |
| stack | `make up` — portal, `guacd`, Guacamole and the trunk gateway |

It is idempotent, and it runs from a systemd unit that only disarms when the
hypervisor step succeeded (so a host that booted without `/dev/kvm` stays armed).
Watch it, or re-run it, with:

```bash
journalctl -fu ontrak-firstboot
cat /var/log/ontrak-firstboot.log
sudo /usr/local/sbin/ontrak-firstboot.sh --force
```

A host that has no `/dev/kvm` still gets the portal; it just cannot create
machines, and the script says so rather than dying quietly.

## What it deliberately does not do

Two things are left to the operator, because both need a decision the image cannot
make for them:

* **Range content.** The golden Windows image and the scenario templates
  (`make golden`, `make templates`) take hours and need Windows media that is not
  redistributable. Set `ONTRAK_BUILD_TEMPLATES=1` in `/etc/ontrak/firstboot.env` to
  have the first boot do it anyway — see [catalog.md](catalog.md).
* **The estate's names.** DNS, certificates and the edge belong to Cerulean, not to
  this host (`make provision-plan`, then `make provision`) — see
  [stack.md](stack.md) and the sign-in section of [operations.md](operations.md).

Sign-in is Authentik's: the stack comes up with generated local secrets, and the
`ONTRAK_PORTAL__OIDC_*` values in `/opt/ontrak/.env` are what point it at the
estate's identity provider.

## Settings

First-boot settings live in `/etc/ontrak/firstboot.env` on the installed host
(`/etc/ontrak/firstboot.env.example` documents every key, and the ISO installs it):

| Setting | Why you would change it |
| --- | --- |
| `ONTRAK_REPO_URL`, `ONTRAK_BRANCH` | a fork or a release branch rather than `main` |
| `ONTRAK_GIT_TOKEN` | the remote is private — tried only after the anonymous clone fails |
| `ONTRAK_STORAGE_DRIVER`, `ONTRAK_STORAGE_SOURCE` | `zfs` or `btrfs` on a spare device, which is what makes provisioning fast for a real class |
| `ONTRAK_NETWORK`, `ONTRAK_NET_CIDR`, `ONTRAK_DOMAIN` | the lab bridge does not match the estate |
| `ONTRAK_BUILD_TEMPLATES=1` | build the range's images as part of the first boot |

Build-time settings, for `make installer-iso`:

| Variable | Default |
| --- | --- |
| `ONTRAK_UBUNTU_RELEASE` | `24.04.5` (the base image, from `releases.ubuntu.com/24.04`) |
| `ONTRAK_BASE_ISO` | a base ISO already on disk, instead of downloading it |
| `ONTRAK_ISO_OUT` | `dist/ontrak-installer-<release>-amd64.iso` |
| `ONTRAK_ISO_CACHE` | `${XDG_CACHE_HOME:-$HOME/.cache}/ontrak-installer` |
| `ONTRAK_INSTALLER_USERNAME`, `ONTRAK_INSTALLER_HOSTNAME` | `ontrak`, `ontrak-range` |
| `ONTRAK_INSTALLER_PASSWORD` | a random one per build, printed at the end |
| `ONTRAK_ISO_SMOKE=1` | also boot the result in QEMU (`ONTRAK_ISO_SMOKE_SECONDS`, default 600) |
| `ONTRAK_INSTALL_TEST_*` | the install test's scratch dir, deadlines, disk size and identity (see the script) |

The username, hostname and password are **defaults for the identity screen**, not
secrets: whoever is at the console sets their own. The build writes the fallback to
`dist/ontrak-installer-<release>-amd64-creds.txt` and prints it, so a truly
unattended boot still ends in a host somebody can sign in to. Delete that file once
the machine is built.

## How the build works

1. Downloads (and sha256-verifies) the Ubuntu Server live ISO, cached.
2. Extracts it with xorriso.
3. Renders the autoinstall twice from one template — `machine` into `/nocloud/` and
   `choose-disk` into `/nocloud-choose-disk/`, each with the `meta-data` the nocloud
   datasource requires (`infra/installer/render-autoinstall.py`).
4. Copies the first-boot payload to `/ontrak/`.
5. Retitles and duplicates every `/casper/*vmlinuz` boot entry (the standard and HWE
   kernels) in the extracted `grub.cfg` — the serial console alongside VGA, so a
   headless machine can be installed and watched over serial. One entry per autoinstall,
   the `choose-disk` one also boots `toram`, and the datasource argument is **quoted**
   (`infra/installer/patch-grub.py`, and see the troubleshooting row below for why).
6. Repacks with xorriso, replaying the boot equipment that
   `-report_el_torito as_mkisofs` reports for the original image.
7. Verifies the payload is on the image — **both** autoinstalls, byte-for-byte against
   the rendered templates — that every boot entry asks for the datasource that was
   actually written to the image (and that the `choose-disk` ones boot `toram`), and
   that an El Torito catalogue and an isohybrid MBR/GPT are present.

xorriso is the only tool that has to be recent; if it is not installed, the build
uses it from a throwaway `ubuntu:24.04` container (built once, tagged
`ontrak-iso-builder:24.04`) rather than installing anything on the operator's
machine.

## Installing a machine, as a test

`infra/installer/install-test.sh` is the only check that answers the question an
operator actually has: it boots the image, answers the identity screen over the QEMU
monitor the way a person at a console would, waits for the install to reboot the
machine, boots what was installed, signs in over SSH as the default identity, and
checks that the payload really landed — the first-boot script, its unit enabled, the
settings example, the account, the SSH server — and prints the first boot's log.

It takes grub's default entry, which is the unattended install on the machine's disk:
that is the path an operator most often walks, and the one a regression would break
silently. The *disk you choose* entry is deliberately **not** covered by it — that one
stops for a decision about storage, and the test's answer to a prompt is an `Enter`
keystroke, which is not a choice of disk. Its autoinstall, its `toram` and its
placement in the menu are held by `scripts/tests/test_patch_grub.py` and by the
build's own verification instead, and a boot of it is worth doing once by hand before
you hand the stick to somebody.

```bash
make installer-iso-test                       # the newest ISO in dist/
bash infra/installer/install-test.sh dist/ontrak-installer-24.04.5-amd64.iso
```

It needs KVM — an install under software emulation takes hours, and the test says so
rather than pretending. The disk and logs are kept when it fails, so the `screendump` of
the installer's screen and the console log are there to read.

In CI it is a manual dispatch — *Installer ISO* → *Run workflow* → *install* — but
GitHub's **hosted** runners have no `/dev/kvm`: a run there stops at the test's own
guard, which is the honest outcome rather than a job that pretends for fifteen minutes.
It is wired up for a self-hosted runner that has one; until then, run it where KVM
actually is — on the range host itself.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| The installer asks every question, the machine installs interactively, and nothing at all appears on the serial console | the boot entry's datasource argument is not **quoted**. Grub ends an argument at `;`, so `autoinstall ds=nocloud;s=/cdrom/nocloud/ console=ttyS0` reaches the kernel as `autoinstall ds=nocloud`: the seed directory is gone (so cloud-init finds no autoinstall), `console=ttyS0` goes with it, and nothing — grub, the kernel or the installer — says a word about it | it is a one-character-class fix in the source of the menu, `infra/installer/patch-grub.py`, and the build now refuses to produce an image whose entries are unquoted. On a finished image, check with `xorriso -osirrox on -indev <iso> -extract /boot/grub/grub.cfg -` and look for `autoinstall "ds=nocloud;s=/cdrom/nocloud/"` |
| The installer asks every question, but the boot entry does look right | the autoinstall is not where the entry says it is | check the `ds=nocloud` path the build printed against `/nocloud` and `/nocloud-choose-disk` on the ISO |
| The installer sits on a screen asking about **basic or rich mode**, and the machine's own keyboard does nothing | that is subiquity's serial-console question: the entry carries `console=ttyS0`, so its UI is on the serial console and the VGA keyboard is not connected to it | answer it over serial, or boot with `console=ttyS0` removed from the entry (`e` at the grub menu) to install from the monitor — see [Which console the installer answers on](#which-console-the-installer-answers-on) |
| The menu offers one install, or Canonical's titles rather than OnTrak's | the grub.cfg was not patched — an ISO built by something else, or by a revision before the second entry existed | the build fails loudly on this (`no /casper/vmlinuz boot entry to patch`); to check a finished image, `xorriso -osirrox on -indev <iso> -extract /boot/grub/grub.cfg -` and look for both titles |
| The choose-disk install stops before it starts, or comes back saying it cannot find an autoinstall | the entry did not get `toram` and the autoinstall is being read from a medium the installer is also erasing | install from virtual media or a second stick onto the first, or put `toram` back: it belongs on the choose-disk entries and only on those |
| "Waiting for the autoinstall to be fetched by subiquity" never clears | the ISO was written with a tool that stripped the `appended partition`/MBR area — write it with `dd`, or boot it as virtual media |
| The install ends powered off | `shutdown` in `autoinstall/user-data.dist` was changed; the reboot is what runs the first-boot unit |
| `install-test.sh` refuses to start | no usable `/dev/kvm` on that machine — it is not a bug, it is the test declining to spend hours emulating one install (`ONTRAK_INSTALL_TEST_ALLOW_TCG=1` accepts the slow path) |
| First boot says the hypervisor is not ready | no `/dev/kvm`: enable VT-x/AMD-V, or nested virtualisation in a VM — then `sudo /usr/local/sbin/ontrak-firstboot.sh --force` |
| First boot cannot clone | a private remote without `ONTRAK_GIT_TOKEN`; the script prints the token it wants |
| `make up` fails in the first boot | usually no internet for the image build, or a port already bound — `cd /opt/ontrak && docker compose logs` |

Installation failures leave a tarball at `/var/log/installer-failure.tar.gz` on the
installed system (`error-commands` in the user-data keeps the installer's own logs).

`make installer-iso-smoke` boots the finished image in QEMU with a 40 GB target disk
and watches the serial console for the installer and for the autoinstall being read —
worth running before handing an ISO to somebody with physical access to a machine.
