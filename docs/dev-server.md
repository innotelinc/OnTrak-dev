# Moving the development server

`infra/restore-dev-server.sh` stands this development server back up on a new
machine. It exists because the server is disposable and the work in it is not, and
because a replacement host should rebuild as little as possible: everything the
script needs is already in one of three places on GitHub.

```bash
infra/restore-dev-server.sh --check                 # would this host work? (changes nothing)
infra/restore-dev-server.sh --dry-run               # every step, spelled out (changes nothing)
sudo infra/restore-dev-server.sh                    # the family stack
sudo infra/restore-dev-server.sh --with-lab         # ... plus the lab control plane
sudo infra/restore-dev-server.sh --with-lab --with-templates
```

## What is stored where

| what | where | how it comes back |
| --- | --- | --- |
| the family application (`src/`, `prisma/`, `ontrak-{tix,sentinel,sync,genie,portal}/`) | `innotelinc/OnTrak`, branch `main` | `git clone` |
| the lab, its host bootstrap, the installer | `innotelinc/OnTrak-dev`, branch `main` | `git clone` |
| the product images | `ghcr.io/innotelinc/ontrak-{training,training-migrate,tix,tix-migrate,sentinel,sentinel-migrate,genie,portal,sync-api,sync-web}` | `docker pull`, then re-tagged into the names the family stack expects |
| the Windows golden image | `ghcr.io/innotelinc/ontrak-golden` (`win11e-*`) | `make golden-pull` (`--with-templates`) |
| the range-host installer | the `installer-24.04.5` release on OnTrak-dev (three ISOs, split into parts under GitHub's asset cap) | `./reassemble.sh <tier>` from the release |

The images are published by `make publish-images` on the OnTrak side, which tags
each one with its product version, its major.minor, `latest`, and `sha-<commit>`.
The publish is a rebuild of the tree, so a tree whose images were published can be
restored without compiling anything.

## What it brings up

Three compose projects, which is what this server runs:

| project | file | what it is |
| --- | --- | --- |
| `ontrak-family` | `OnTrak/docker-compose.all.yml` (+ `docker-compose.lan-db.yml`) | ITS (training), Tix, Sentinel, Sync, Genie, Portal, three databases |
| `ontrak-authentik` | `OnTrak-dev/deploy/authentik/compose.yaml` | the local IdP the *lab* portal signs in through |
| `ontrak` | `OnTrak-dev/docker-compose.yml` | the lab control plane: portal, gateway, Guacamole, and Incus on the host |

The family half is `make all-up-lan` in the OnTrak checkout: the base file plus the
database overlay, which publishes each database on loopback *and* on this host's LAN
address. The lab half needs `/dev/kvm` for real machines; without it the control
plane still starts and demo mode still runs.

## What it deliberately does not restore

- **The Docker volumes.** Their contents are a development server's data — seeded
  accounts, attempts, tickets, sessions — and the `.env` this script writes has
  freshly generated secrets, so an old database's rows would be signed with keys
  that no longer exist. `--no-demo` skips even the seed data. Copy volumes across
  by hand (`docker volume` plus `tar`) if a particular class's results matter.
- **The installer ISO.** It is built where it is needed (`make installer-iso`,
  `make installer-iso-tiers`), and the published release exists for a range host
  that has no internet yet. See [installer.md](installer.md).
- **The scenario templates.** They are snapshots built where they will run, so they
  are built on the new host (`make templates`) rather than shipped. The golden image
  they are built from *is* pulled.

## After it finishes

The last step asks every product the same health question the `Family stack` CI job
asks, and exits 1 if one does not answer; a dry run prints those seven probes
instead of asking them. Then:

```bash
cd /srv/ontrak/OnTrak && make all-logs        # if something is missing
cd /srv/ontrak/OnTrak-dev && make check       # the lab's own preflight
```

Sign-in is the one thing a fresh host cannot have already: the family's `.env` has
new secrets and the local Authentik has new accounts, and both are printed by the
step that generated them (`deploy/authentik/setup.sh` ends by printing the seed
accounts and the password they share).
