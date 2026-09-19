# Running OnTrak in Docker

The whole control plane and the browser console run as containers. The training
machines do not — they are Incus virtual machines on the host, and that split is
the design, not a shortcut.

```bash
docker compose up -d --build  # the whole installation, first run included
# portal   http://localhost:8080        console   http://localhost:8080/guacamole/
make ps                       # health of every service
make logs                     # follow
docker compose logs lab-setup # what the first run set up
make down                     # stop (the state and media volumes survive)
```

`make up` is the same command with the addresses printed at the end. `--build`
only matters after a `git pull`: without it compose reuses the image from the
previous commit, and a stale `lab-setup` reports a host step it never ran.

What the lab host needs, for the version that installs Incus for you:

| | |
| --- | --- |
| Linux, Debian or Ubuntu | the host step installs with `apt`. Another distribution: install Incus yourself, then run the stack with `ONTRAK_LAB_SETUP=off`. |
| Docker Engine with Compose v2 | the setup container is privileged and needs `pid: host`, so it must be the daemon on the lab host itself. |
| x86-64 with virtualisation, `/dev/kvm` | bare metal with VT-x/AMD-V, or a VM with nested virtualisation. Nothing else can boot a Windows guest. |
| RAM for the machines in flight | the default student profile is 2 vCPU / 4 GiB; size the host for a class, not for one student ([operations.md](operations.md)). |

**Docker Desktop, or Docker on another machine:** the host step cannot reach the
lab host's namespaces and says so. The portal still runs, which is the useful
half for a workshop or a demo:

```bash
make up-remote                                            # or:
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d
```

## The first run

`docker compose up` on a machine that has only Docker is a complete
installation. A one-shot `lab-setup` service runs before anything else and does
the two things a fresh checkout cannot do for itself:

1. **Secrets.** It writes `.env` (via `scripts/secrets.sh`, which never
   overwrites a value that is already there) and publishes the shared keys to a
   volume the portal and the console gateway both read. This step has to happen
   inside a container: compose interpolates variables and reads `env_file` when
   it loads the project, *before* any container runs, so a stack that required a
   `.env` could never create one.
2. **Incus on the host.** If the host cannot run training machines yet, it runs
   the host's own `infra/bootstrap-host.sh` — installs Incus, initialises the
   daemon, and creates the storage pool, lab bridge, project and profiles. It
   does that by entering the host's namespaces (`privileged: true`, `pid: host`)
   and running the script as *the host's file*, so a host prepared this way and a
   host prepared by hand end up identical.

Only then do the portal and the gateway start: both wait on
`service_completed_successfully`, because the files `lab-setup` writes are what
they read. `scripts/check-first-run-contract.py` checks that arrangement against
the rendered configuration, where a comment cannot rot.

Two things it deliberately does **not** do: it never blocks the control plane
(a host that cannot run VMs is reported, and the portal still comes up — in demo
mode, or against a remote cluster), and it never guesses a storage driver,
because cloning is what makes a reset cheap and only you know what the disk is:

```bash
# in .env, before the first run
ONTRAK_STORAGE_DRIVER=zfs          # dir (default) | btrfs | zfs | lvm
ONTRAK_STORAGE_SOURCE=/dev/nvme1n1 # a block device, for zfs/lvm
```

`ONTRAK_LAB_SETUP=force` re-runs the host step; `off` skips it. See
[operations.md](operations.md) for what the pool layout does to class timings.

## What is a container, and what is not

| Piece | Runs as | Why |
| --- | --- | --- |
| `portal` — FastAPI student portal: scenarios, sessions, tickets, grading, admin panel | **container** | plain Python service: stateless apart from the database, no privileges of its own |
| `guacamole` + `guacd` — HTML5 console (RDP/SSH into the training machines) | **containers** | off-the-shelf upstream images, the job they were built for |
| The training machines: Windows 95→11, Server, Office, and the Linux distributions | **Incus VMs on the host** | they must boot a real kernel, hold a driver fault, survive a "malware" scenario and be snapshot-restored between students. A container cannot be Windows 95, and a broken machine that shares the host kernel is an outage, not a lesson |
| Bare Incus, the bridge and the profiles | **host services** | created by `infra/bootstrap-host.sh` |

So the containers are the *control plane*. The lab is the host.

## Three ways to reach a hypervisor

The portal shells out to the `incus` CLI, so "where is Incus" is a mount, not a
build flag.

**1. Local Incus on the same host (the normal lab).** This is the base stack,
and `lab-setup` installs and initialises Incus for you if it is not there yet.
`/var/lib/incus` is bind-mounted into the portal, which is where the daemon's
socket and its configuration live.

That directory is the whole Incus API: an unprivileged reader of it can create,
delete and exec into machines. **The portal container is a hypervisor admin,
because that is what it is.** Treat it like root on the lab host — which is also
why it is not published beyond loopback by default, and why the mount is the
directory rather than the socket file (a host without Incus yet gets an empty
directory instead of Docker creating a stray file where the daemon will later
want its own socket).

**2. A remote Incus cluster over HTTPS.** `docker-compose.remote.yml` turns the
host step off (nothing to install here) and points the portal at a named remote,
with the CLI's trust config mounted read-only:

```bash
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d   # or: make up-remote
```

Set `ONTRAK_INCUS__REMOTE` to the remote's name (and `ONTRAK_INCUS__PROJECT`) in
`.env`; uncomment the trust-config mounts at the bottom of that file for your
operator account. A cluster also removes the single-host ceiling: placement
across hosts is the daemon's problem, not the portal's.

**3. No hypervisor at all.** Demo mode runs an entire class against an
in-memory Incus — no socket, no Windows, no guests:

```bash
make demo                    # on the host: a 6-student class, end to end
make docker-demo             # the same thing inside the image
docker compose run --rm -e ONTRAK_DEMO__ENABLED=true portal demo serve
```

Use it for a workshop, a screenshot, a smoke test, or CI. `make up` also works
with no hypervisor at all: the first-run step reports that the host cannot be
prepared, the portal starts anyway, serves everything stored in its database, and
tells you on the admin panel that the hypervisor is unreachable rather than
failing to render.

## Volumes

| Volume | Holds | Lose it and… |
| --- | --- | --- |
| `ontrak-state` → `/app/state` | SQLite: accounts, sessions, **submitted grades**, tickets, audit log | you lose every result. Back this up; nothing else here is worth a second copy |
| `ontrak-media` → `/app/media` | installation media (`ontrak media fetch`) | you re-download it. It is large and reproducible, never back it up |
| `ontrak-secrets` → `/run/ontrak` (portal, gateway) | the shared keys the first run generated | the portal and the gateway lose their common key and console links stop opening. `ONTRAK_LAB_SETUP=force` regenerates it, which logs everyone out |
| `deploy/guacamole/recordings` → `/recordings` | optional session recordings (`ONTRAK_GUAC__RECORDING=true`) | you lose the recordings. They can be very large, and they show a student's screen |

`../state` on the host is still the path the *host* uses when you run
`make serve` instead of the container. The two are deliberately separate: mixing
them means a container upgrade can lock the host out of its own database.

## Ports

| Port | Default | Service |
| --- | --- | --- |
| 8080 | `ONTRAK_BIND_ADDR:ONTRAK_PORTAL__PORT` | the origin gateway: the student portal and admin panel at `/`, the console at `/guacamole/` |

One published port, on purpose. The portal and the console are separate containers
but share an origin (`deploy/gateway/nginx.conf` routes them), because the estate's
edge forwards a host rather than a path — and because a port per service is one
more host port to collide with whatever else the machine is running.

On the deployment the range answers on three names, provisioned through Cerulean
(DNS, the wildcard certificate and the edge host — `make provision`), which is
the only supported way: a record added by hand and a certificate fetched by hand
are two things that drift apart, and the drift is a browser warning in front of a
class.

| Address | What serves it |
| --- | --- |
| `https://ontrak.innotel.us/` | the range itself — the portal, and the console at `/guacamole/` |
| `https://student.ontrak.innotel.us/` | the same portal; what a student is given |
| `https://admin.ontrak.innotel.us/` | the same portal; what an instructor is given |

One portal, three names, role-gated at sign-in: the student and staff names are
how an institution hands out one URL each, and how the edge can be told to treat
them differently later (the student name on the internet, the staff name behind
the VPN) without touching the app.

What the edge has to do with the stack bound to loopback (the default) is three
hosts and no paths: every name goes to the same address, and the stack's gateway
decides what `/` and `/guacamole/` mean. The path is passed through unchanged,
which is why `ONTRAK_GUAC__BASE_URL` keeps its trailing `/guacamole/`.

| Public | Upstream |
| --- | --- |
| `https://ontrak.innotel.us/` | `http://<lab host>:8080/` |
| `https://ontrak.innotel.us/guacamole/` | `http://<lab host>:8080/guacamole/` (routed inside the stack, not at the edge) |
| `https://student.ontrak.innotel.us/` | `http://<lab host>:8080/` |
| `https://admin.ontrak.innotel.us/` | `http://<lab host>:8080/` |

TLS is one wildcard (`*.ontrak.innotel.us`) plus a certificate for the apex — a
wildcard never covers the name it hangs off — both issued by Cerulean over
DNS-01, so nothing has to be reachable from the internet for them to exist.

`ONTRAK_BIND_ADDR` defaults to `127.0.0.1`. That is the portfolio posture — a
TLS proxy (NPM Edge / Cerulean-issued certificate) in front, never the portal
straight onto a network. For a lab where students reach the host directly, set
`ONTRAK_BIND_ADDR=0.0.0.0` and make sure `ONTRAK_GUAC__BASE_URL` is the URL
those browsers actually use; the console payload travels in a URL fragment and
must never cross a network in plain text off-host.

**If the TLS proxy is itself a container on this host, loopback will not do.** The
edge forwards to the host's address (`192.168.1.46:8080`), and a loopback binding
answers that from the host's *own* process namespace — not from another container,
which reaches the published port over the bridge. The symptom is a 502 from the
edge while `curl 127.0.0.1:8080` succeeds on the host, which reads as the stack
being down when it is up. Set `ONTRAK_BIND_ADDR=0.0.0.0` (or the host's address)
for that arrangement.

## Configuration

Everything under `environment:` in `docker-compose.yml` is a `ONTRAK_*` override
of `config/ontrak.yaml`, and every one of them has a default, so `.env` only
needs the values you want to differ. The two that have no default are the ones a
first run generates for you:

* `ONTRAK_PORTAL__SECRET` — signs portal session cookies.
* `ONTRAK_GUAC__SECRET_KEY` — exactly 32 hex characters; the portal signs console
  links with it and Guacamole verifies them. **One value, two services**; if they
  ever disagree, every console link silently fails to open.

`lab-setup` generates both (with the instructor and guest passwords) on the first
run, through `make secrets` / `scripts/secrets.sh`, which never overwrites a value
that is already there — a `vault://` reference counts as a value. In production
these come from Cerulean Vault by reference (see `.env.example`); the container
resolves a reference only if the deployment gives it something that can,
otherwise it fails at boot. There is no silent fallback to a local secret.

Run `bash scripts/secrets.sh` by hand to create `.env` *before* the first run —
useful when you want to set the storage driver first, or to see the generated
instructor password without reading it back out of the file.

Sign-in has no default either. The portal is SSO-only against Cerulean's
Authentik, so a stack that has never been pointed at it comes up with a login
page that says so rather than one offering a password. The way out is to register
this range's application in Cerulean and set the four `ONTRAK_PORTAL__OIDC_*`
values — see [operations.md](operations.md#sign-in). There is no local account to
fall back to.

## Operating it

```bash
make exec ARGS="user list"                      # the CLI, inside the running portal
make exec ARGS="pool status"
make exec ARGS="catalog groups"
make exec ARGS="session list --state in_use"
docker compose exec portal python3 -m ontrak scenario validate   # same thing
```

There is no instructor account seeded on boot and no password to change: sign-in
belongs to Authentik and the portal keeps no credential — see
[operations.md](operations.md#sign-in). An account appears the first time its
owner signs in, and its role comes from the Authentik instructor group.
`make demo-serve` is the one case with a door of its own, because a demo has no
IdP to sign in against.

Templates and the warm pool still need a host that can build them
(`infra/build-templates.sh`, `make templates`). Start a class with
`make pool` to check depth, or let the admin panel's Maintenance card do it.

## Upgrades

```bash
git pull
make up                       # `docker compose up -d --build`: a new image, then start
# or, without make:
docker compose build && docker compose up -d
```

The state volume is untouched by a rebuild. `docker compose down -v` deletes the
volumes — on a range that has graded results, that is data loss, so it is not a
"reset".

## Troubleshooting

**`/admin` shows "Hypervisor reads failed".** The panel is telling you exactly
what it could not read — usually `The incus daemon doesn't appear to be started`.
Everything stored in the portal still works and is still shown; only live machine
facts are missing. Start Incus on the host (`systemctl start incus`) and check it
is answering there (`incus info`); if it is not installed at all,
`ONTRAK_LAB_SETUP=force docker compose up -d` runs the host step again.

**`failed to bind host port`.** Something already owns 8080 on the host — only
one port is published, so this is the only one that can collide. Set
`ONTRAK_PORTAL__PORT` in `.env` and bring the
stack up again.

**`unix.socket` became a directory.** Bind-mounting a path that does not exist
makes Docker create it — and it creates a *directory*. The base stack mounts
`/var/lib/incus` as a directory for exactly this reason, so this only bites an
old overlay that pointed at the socket file. If it happened:

```bash
docker compose down
sudo rmdir /var/lib/incus/unix.socket     # only if it is empty and Incus is stopped
sudo systemctl restart incus              # the socket is recreated by the daemon
```

**The portal is up but no console appears in the iframe.** `ONTRAK_GUAC__BASE_URL`
is what *students' browsers* resolve, not what the container can reach. Behind a
TLS proxy it is the public console host, not `localhost`.

**`make exec ARGS=doctor` says there is no hypervisor, but Incus is installed.**
The first run says which of the three ways to reach a hypervisor it took; read it
with `docker compose logs lab-setup`. "cannot reach the host's namespaces" means
Docker is not running on the lab host itself (Docker Desktop, or a remote
daemon) — install Incus on that host with `sudo infra/bootstrap-host.sh`, or use
the remote overlay, or demo mode. If Docker *is* on the lab host and it still
says that, check for a stale image first: `make up` (which passes `--build`)
rather than a bare `docker compose up` after a `git pull`.

**The host step failed and I want to see the output again.** It is in
`docker compose logs lab-setup`, and the last line names the script *by its path
on the host*, so the retry it prints can be pasted into a shell there:

```bash
sudo /path/to/checkout/infra/bootstrap-host.sh   # the host step, alone
ONTRAK_LAB_SETUP=force make up                   # then let the stack re-check
```

**`guacamole` never becomes healthy.** It waits for `guacd`; check
`make logs` — and note the first start pulls the upstream images.
