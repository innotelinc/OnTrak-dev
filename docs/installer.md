# The installer ISO

`infra/build-installer-iso.sh` builds a bootable image that installs Ubuntu Server
24.04 LTS on a bare machine and leaves it as a working range host: Incus, the lab
bridge, the OnTrak checkout and the portal stack. It is a remaster of Canonical's own
live-server ISO — not a hand-rolled installer — so firmware support, drivers and the
installer itself are whatever Ubuntu ships.

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

The image is unattended apart from that screen. It boots from BIOS, UEFI, a USB
stick and virtual media, because it carries the original El Torito, isohybrid MBR
and GPT boot equipment.

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
3. Writes `/nocloud/user-data` and `/nocloud/meta-data` — the rendered autoinstall
   from `infra/installer/autoinstall/`.
4. Copies the first-boot payload to `/ontrak/`.
5. Adds `autoinstall ds=nocloud;s=/cdrom/nocloud/ console=ttyS0` to every
   `/casper/*vmlinuz` boot entry (the standard and HWE kernels) in the extracted
   `grub.cfg` — the serial console alongside VGA, so a headless machine can be
   installed and watched over serial.
6. Repacks with xorriso, replaying the boot equipment that
   `-report_el_torito as_mkisofs` reports for the original image.
7. Verifies the payload is on the image, that the boot entries request the
   autoinstall, and that an El Torito catalogue and an isohybrid MBR/GPT are
   present.

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

```bash
make installer-iso-test                       # the newest ISO in dist/
bash infra/installer/install-test.sh dist/ontrak-installer-24.04.5-amd64.iso
```

It needs KVM (an install under software emulation takes hours, and the test says so
rather than pretending). The disk and logs are kept when it fails, so the
`screendump` of the installer's screen and the console log are there to read. In CI it
is a manual dispatch — *Installer ISO* → *Run workflow* → *install* — because it takes
about a quarter of an hour; GitHub's Linux runners have `/dev/kvm`, which is what
makes it possible there and not on the range host.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| The installer asks every question | the boot entry did not get the autoinstall: check the `ds=nocloud` line the build printed, and `/nocloud` on the ISO |
| "Waiting for the autoinstall to be fetched by subiquity" never clears | the ISO was written with a tool that stripped the `appended partition`/MBR area — write it with `dd`, or boot it as virtual media |
| The install ends powered off | `shutdown` in `autoinstall/user-data.dist` was changed; the reboot is what runs the first-boot unit |
| First boot says the hypervisor is not ready | no `/dev/kvm`: enable VT-x/AMD-V, or nested virtualisation in a VM — then `sudo /usr/local/sbin/ontrak-firstboot.sh --force` |
| First boot cannot clone | a private remote without `ONTRAK_GIT_TOKEN`; the script prints the token it wants |
| `make up` fails in the first boot | usually no internet for the image build, or a port already bound — `cd /opt/ontrak && docker compose logs` |

Installation failures leave a tarball at `/var/log/installer-failure.tar.gz` on the
installed system (`error-commands` in the user-data keeps the installer's own logs).

`make installer-iso-smoke` boots the finished image in QEMU with a 40 GB target disk
and watches the serial console for the installer and for the autoinstall being read —
worth running before handing an ISO to somebody with physical access to a machine.
