# Operations

## Capacity: the arithmetic

Three resources bind, in this order: **RAM**, then **storage**, then **CPU**.

Baseline per student VM (from `infra/incus/profile.yaml`): 2 vCPU, 4 GiB RAM,
48 GiB thin disk. A booted Windows VM idles at a few percent CPU and ~1.5-2 GiB
resident, but plan for the limit, not the idle.

### RAM is the binding constraint

```text
RAM ≈ host_overhead (8-16 GiB)
    + max(live_students, pool_target) × 4 GiB
    + refill_headroom
```

The `max(...)` is the important part, and it is why the pool targets count
*claimed* VMs too: when a student claims a pooled VM the pool shrinks by one, so
a full class holds `students` VMs, not `students + target`. The reaper only builds
replacements as sessions end.

| Host | Students (simultaneous) | Notes |
| --- | --- | --- |
| 8 vCPU / 16 GiB, `dir` storage | 2-3 | Development only. Every clone is a full 20-30 GB copy. |
| 16 vCPU / 64 GiB, ZFS or btrfs | 8-12 | Small class. Pool of 4-6 prewarmed for instant handoff. |
| 32 vCPU / 128 GiB, ZFS or btrfs | 20-26 | Comfortable class of 20 with a pool of 10. |
| 64 vCPU / 256 GiB, ZFS or btrfs | 45-55 | Two classes back to back, or one large lab. |
| Incus cluster (2× 32 vCPU / 128 GiB) | 40-50 | Recommended above ~30 students; one control node runs the portal. |

### CPU: mind the performance scenario

`os-perf-startup` deliberately burns one core per student. In that scenario CPU
is not overcommittable: 30 students need roughly 30 cores' worth of headroom, or
the CPU objective becomes noisy and grading turns flaky. Either split the cohort,
shrink the pool for that session, or raise the VM's `limits.cpu` and adjust the
scenario's threshold. Every other scenario is happy at 2-3× CPU overcommit.

### Storage

| Storage driver | Clone cost | When to use |
| --- | --- | --- |
| `zfs` / `btrfs` | Copy-on-write, seconds, megabytes | **Use this.** Snapshots and resets are the whole design. |
| `lvm` (thin) | Similar, with thin snapshots | Fine if you already run LVM thin. |
| `dir` | Full copy: 20-30 GB and minutes per VM | Works, but a 30-student class means ~900 GB and a slow start. |

```text
disk ≈ templates (20-30 GiB × scenarios, powered off)
     + pool VMs × (a few GiB on CoW; full size on dir)
     + recordings, if guac.recording is on
```

`ontrak doctor` warns when the pool driver is `dir`.

## Scheduled prewarm and teardown

A class starts at 09:00. If every student's first action is "provision me a machine", the
first minutes are spent watching a clone bar and the host takes a burst of load. Both
problems disappear if the pool is warm before the class and drained after it.

```yaml
schedule:
  enabled: true
  windows:
    - label: morning-class
      days: [mon, wed]
      start: "09:00"
      end: "12:00"
      prewarm_minutes: 30      # start filling at 08:30
      target: 15               # unclaimed, booted machines per scenario
      scenarios: [net-dns-failure, os-perf-startup]
```

```bash
ontrak schedule show     # the windows, and which phase we are in right now
ontrak schedule tick     # decide and perform: prewarm, recycle idle, drain
```

`tick` is idempotent and safe to run from cron every minute. It prewarms only a genuine
deficit — a class mid-session does not trigger a second wave — recycles sessions idle beyond
`session.idle_recycle_minutes` while a window is open, and drains unclaimed pool machines
when a window closes (never one a student is using).

Without a schedule you can do the same by hand:

```bash
ontrak pool prewarm --scenario net-dns-failure --count 15
ontrak pool drain --scenario net-dns-failure      # after the class
```

## Media, before the first class

The catalog describes media; the media store holds it. Check what you actually have:

```bash
ontrak media status      # present / fetchable / operator-required, with sizes
ontrak media missing     # the exact filenames to supply from your own licences
ontrak media fetch       # free media only: evaluation ISOs and image-server entries
```

Expect a first-run download in the tens of gigabytes if you fetch everything. Free media
can be served from a local mirror instead — a manifest `url:` accepts `file://`.

## Time limits

Each session is created with a time limit the student picks (`session.time_limit_choices`,
default 45/90/180 minutes), stored on the session row rather than derived from config on
read, so what a student was granted is auditable. Operators change it mid-session:

```bash
ontrak session limit --session-id 42 --minutes 120   # set the clock
ontrak session extend --session-id 42 --minutes 15   # grant extra time as a delta
```

Expiry destroys the machine and closes the session. Nothing is graded on the way out — an
unsubmitted session has no result, which is the intended meaning of "results only".

## Running a workshop with no host

Demo mode exercises the whole student flow with an in-memory hypervisor: no Incus, no
Windows, no secrets. Use it for walkthroughs, screenshots, conference talks and CI:

```bash
ontrak demo run --students 6 --success-rate 1.0   # a clean class
ontrak demo run --students 6 --success-rate 0.6   # realistic partial credit
ontrak demo serve                                 # the portal, in demo mode
```

It proves the lifecycle, the scoring and the UI. It does **not** prove the Windows path —
the same caveat applies as everywhere else: `ontrak doctor` and one real template build.

Running the platform itself in Docker (`docker compose up -d`) changes none of this: the
containers are the control plane and the console gateway, while the training machines stay
Incus VMs on the host. The first run prepares that host for you — it installs Incus and
creates the pool, bridge, project and profiles — so the only thing left before a class is
the part that genuinely needs a human: check the storage driver, build the templates you
intend to use, and size the pool. See [docker.md](docker.md) for what is containerised, the
three ways to reach a hypervisor, and the volume that holds the results.

## Before, during and after a class

### Before (10 minutes)

```bash
make doctor                                                    # host healthy? (incus, secrets, console gateway key)

.venv/bin/ontrak scenario validate                           # catalogue healthy?
.venv/bin/ontrak template build --all                        # after any scenario edit
.venv/bin/ontrak pool prewarm --scenario net-dns-failure --count 30
make serve          # terminal 1: portal
make reap           # terminal 2: expiry + pool top-ups
```

Prewarm only the scenario(s) you are teaching now. Keeping a pool warm for all
six scenarios multiplies idle RAM by six for no benefit.

There is no roster to import: sign-in is Authentik's, and the portal creates an
account on the first sign-in, keyed on the account's Authentik **email** address.
Provision the class in Authentik (and, if you gate the range to a cohort, set
`ONTRAK_PORTAL__OIDC_REQUIRED_GROUP`) before the session — a student who signs in
before that still lands on the row that holds their results, because the row is
their email either way.

### During

* Students self-serve from `/dashboard`; each gets their own VM and timer.
* `/instructor` shows live sessions, the pool, results, and a **Watch** link per
  session (Guacamole) when `guac.recording` or shadowing is enabled.
* Stuck student: give them `+15 minutes` or reset their session from the
  instructor page rather than debugging the VM yourself.
* A session left open on a screenless desktop is recycled automatically
  (`session.idle_recycle_minutes`), so you rarely need to end sessions by hand.

### The student's console: RDP for Windows, SSH for Linux

Guacamole speaks RDP and SSH, and the guest decides which one a scenario needs.
A Windows VM brokers RDP (`guest.rdp_port`). A Linux *container* answers no RDP at
all, so an RDP connection aimed at one used to render as a page saying "the remote
desktop server is currently unreachable" — a message that blamed the student's
machine for a transport that was never going to exist, and said nothing about the
scenario being fine.

With `guac.linux_ssh` on, `ontrak template build` provisions the template for a
shell console instead: `openssh-server` installed, root's password set to
`guest.password`, `PermitRootLogin` and `PasswordAuthentication` enabled, and a
`00-ontrak-console.conf` drop-in written into `sshd_config.d` so an image's own
drop-in cannot override it. That runs **after** the fault is injected and after
`setup.sh` has verified it, and is the last thing written before the snapshot — a
fault that touches accounts or permissions (`id-locked-account`,
`linux-sudo-delegation`) must not be able to take the console's credential with
it. A build where sshd does not come up **fails**, naming the reason, rather than
snapshotting a template whose console will lie.

Two consequences worth knowing: it is the one step in a template build that
reaches the network (apt), and it opens port 22 in every Linux guest on the
isolated `ontrak0` bridge. Turning it off restores the previous behaviour exactly
(no console for Linux scenarios, and the page explains why). It also refuses to
run with an empty `guest.password`, because `chpasswd` would set an empty one and
the console would then be openable as root by anyone on the lab network.

### After

```bash
curl -s -b "ontrak_session=$COOKIE" http://portal:8080/instructor/results.csv > results.csv
.venv/bin/ontrak session list                     # confirm nothing is left running
.venv/bin/ontrak pool refill                      # or set targets to 0 to give the RAM back
```

Student VMs are always destroyed — there is nothing to clean up by hand. Only pool
VMs and powered-off templates persist, and templates cost no RAM.

## Multi-host

Put the hosts in an Incus cluster and point the control plane at the cluster
endpoint; placement is then the cluster's job.

```bash
incus remote add lab https://node1.lab.example.com:8443 --auth-type=tls
export ONTRAK_INCUS__REMOTE=lab
```

Keep the portal, Guacamole and SQLite on one control node, and prewarm enough VMs
for the whole class: a cluster does not make a cold Windows boot faster.

## Maintenance

| Task | Command / approach |
| --- | --- |
| Refresh the Windows image (patches, expired eval) | `make golden` — rebuilds and republishes `ontrak-win-base`, then rebuild templates |
| Scenario edited | `make validate && .venv/bin/ontrak template build <id> --force` |
| Templates regenerated | Pool VMs built from the old template keep running; end their sessions or let the reaper recycle them |
| Leaked instances | `incus --project ontrak list` and delete anything that is not `tpl-*` or an active session; `ontrak stats` shows the state counts |
| Control plane database | `state/ontrak.sqlite3` (WAL) on the host, or the `ontrak-state` volume when the portal runs in Docker (`docker run --rm -v ontrak-state:/s alpine tar czf - -C /s . > ontrak-state.tgz`). Back it up if results matter; deleting it resets users/results, not VMs |
| Logs | `journalctl -u incus`, `make logs` (or `docker compose logs -f`) for the stack, and the portal's events table (`/admin/audit`) |
| Who is an instructor | membership of the Authentik group in `ONTRAK_PORTAL__OIDC_INSTRUCTOR_GROUP` (read on every sign-in). There is no local account to promote — change the group in Authentik |
| Reclaim RAM fast | set `pool.targets` to 0, then `ontrak pool status` and delete pool VMs, or just stop them with `incus stop` |

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Session sits in `provisioning`, then `error` | guest transport never answered | Check `session.error` on the page, then `ONTRAK_INCUS__REMOTE=... incus --project ontrak info <instance>`; confirm the guest has an IP on `ontrak0`. If RDP is up but WinRM is not, the image's `post-install.ps1` step did not run — rebuild the golden image. |
| `pywinrm` errors with 401 | wrong training password, or the account is not a local admin | Compare with `guest.password`; the image sets `LocalAccountTokenFilterPolicy=1` so elevation should work |
| Template build fails with "never obtained an address" | wrong bridge or DHCP range exhausted | `incus network get ontrak0 ipv4.dhcp.ranges`; widen the range for large classes |
| Template build fails with "did not report ONTRAK-SETUP-OK" | the setup script threw | The error includes the output tail; run the VM manually and execute `setup.ps1` to see the full error |
| "template is missing snapshot clean" | scenario edited, template not rebuilt | `ontrak template build <scenario> --force` |
| Guacamole shows "connection failed" | target 3389 unreachable from guacd, or wrong credentials | `ontrak session console <id>` to inspect; confirm the VM answers on 3389 from the control node; check `guest.rdp_port` |
| Console loads but every session is refused ("Permission denied"), or the iframe never opens | the gateway signs off on a different `JSON_SECRET_KEY` than the portal's `guac.secret_key`, or its `guacamole-auth-json` extension is not enabled | `ontrak doctor` probes this directly and now **fails** on it (it posts a payload signed with `guac.secret_key` to `<guac.base_url>/api/tokens`). Make `JSON_SECRET_KEY` equal `ONTRAK_GUAC__SECRET_KEY` and recreate the gateway: `make console-recreate` (or `docker compose up -d --force-recreate guacamole`). A stack started before the key was set keeps the empty one until it is recreated. The student's page says so too instead of showing an empty console |
| Console iframe blank | `guac.base_url` is not the URL the student's browser uses, or the page is HTTP while Guacamole is HTTPS | Set `guac.base_url` to the browser-visible HTTPS URL and put a TLS proxy in front |
| Console says "the remote desktop server is currently unreachable" | the console is an **RDP** connection pointed at a Linux *container*, which answers no RDP at all | Turn on `guac.linux_ssh` and rebuild that scenario's template: the build installs and configures `sshd` (see below), and the console becomes a shell |
| Console is refused the same way **after** turning on `guac.linux_ssh` | the template predates the setting | Templates are snapshots: re-run `ontrak template build <scenario> --force` for each Linux scenario, or `infra/build-templates.sh`. A template built before the setting existed has no sshd in it |
| Students say "no machine available" | pool empty and clones are slow | Prewarm more, or move the pool to ZFS/btrfs |
| Pool keeps growing and the host swaps | refill targets too high for a full class | Lower `pool.targets`, or `pool.max_total`; remember targets count claimed VMs, so `target = class size` is the right shape |
| Grading returns 0% with "grading could not run" | `check.ps1` failed or never printed the markers | Run it manually in a session; `make validate` first, then check the guest-side error in the network detail line |
| Scores look wrong after an image change | the check reads live state that moved (an adapter name, a service name) | Prefer outcome-based checks (`docs/scenarios.md`); open a session and inspect the `-Detail` strings |

## Sign-in

The portal has no password of its own. An instructor and a student are
**Authentik** accounts in Cerulean, and the portal only decides what a signed-in
account may do — one list of who exists, one place to disable someone, and
nothing on the range to keep in step.

Cerulean registers the application, and prints the client secret for it:

```bash
# in Cerulean's checkout
python3 scripts/authentik-setup.py ontrak
```

Then set the four `ONTRAK_PORTAL__OIDC_*` values in `.env` (see `.env.example`)
and restart the portal. All four are needed: with any one missing the flow is off
and the login page says which values are expected instead of rendering a button
that leads nowhere.

**The callback is per origin.** The sign-in returns to the origin it started on,
and Authentik only accepts a callback it was registered with — so every origin
the portal answers on is listed in `ONTRAK_PORTAL__OIDC_REDIRECT_URI`. The range
answers on three names (`scripts/cerulean-provision.py` creates them):

```
https://ontrak.innotel.us/oidc/callback
https://student.ontrak.innotel.us/oidc/callback
https://admin.ontrak.innotel.us/oidc/callback
```

A name whose callback is missing cannot sign in at all — the IdP refuses the
redirect before anyone types a password. A `Host` header naming an origin that is
*not* on the list falls back to the first entry, so a forged one cannot create a
new callback.

**Roles come from Authentik groups**, read on every sign-in: a member of
`ONTRAK_PORTAL__OIDC_INSTRUCTOR_GROUP` is an instructor (the class view, reset and
the admin panel), everyone else who signs in is a student. Adding someone to the
group grants that view and removing them takes it away, with no local edit and no
second place to keep in step. An Authentik superuser is always an instructor, so
the range's owner is never locked out of it. Set
`ONTRAK_PORTAL__OIDC_REQUIRED_GROUP` to gate the whole range to one class cohort.

The portal's account is keyed on the Authentik **email address**. An account
Authentik authenticates but has no email for is refused rather than given an
invented name; the fix belongs in Authentik.

### There is no password path

There is no local account and no password form, anywhere: `POST /login` does not
exist, and the portal holds no credential of its own. An account appears on the
range the first time its owner signs in through Authentik, keyed on their email
address — and because Authentik is re-read on every sign-in, adding someone to
the instructor group grants the class view and removing them takes it away, with
nothing to keep in step locally.

Two consequences worth knowing. A *disabled* account stays disabled: an
instructor taking a student off the board is not undone by that student signing
in again. And **deleting** an account in the admin panel only removes the local
row — identity is Authentik's, so the person can sign straight back in. Revoke
the person in Authentik; use disable for the range's own control.

`ontrak demo serve` is the one exception, and only for itself: a demo has no IdP
to sign in against, so the portal opens a demo-only door — pick a demo account,
no password — mounted only while `demo.enabled` is on.

## Security and audit notes

* **Isolation boundary:** the `ontrak0` bridge. Never put the portal, Guacamole or
  the SQLite file on it, and do not bridge it to the office LAN. Student VMs are
  hostile-by-design (they run simulated malware).
* **Recording:** set `guac.recording: true` to keep an RDP recording per session
  for review. Recordings live on the guacd volume and can be large; treat them as
  student data and prune them.
* **Events:** every allocate/ready/check/reset/recycle is written to the `events`
  table with the session id, which is enough to reconstruct who did what, when.
* **Instructor actions are not yet separately audited** (an instructor reset looks
  like a student reset in the event log) — add the actor to `log_event` before this
  runs in an environment with multiple instructors.
* **Guacamole holds no credentials.** If you enable `guac.recording`, remember the
  recordings contain whatever was on the student's screen.

## Cost and licensing

* **Windows:** the golden-image path uses Microsoft *evaluation* media
  (90 days for Windows 11 Enterprise eval, 180 for Server). Rebuild before a course,
  or use volume licensing with KMS/AD for permanent infrastructure. Licence
  compliance for training VMs is the operator's responsibility.
* **Hosts:** nothing in this stack needs a commercial hypervisor; Linux + KVM +
  Incus covers it. The Zabbly repository is the upstream Incus channel.
* **Third-party build tool:** `antifob/incus-windows` automates the unattended
  Windows install. Pin a commit (`ONTRAK_INCUS_WINDOWS_REF`) — it is a build-time
  dependency, not a runtime one, and its interface changes between versions.

## Legacy platform notes

The catalog carries platforms that predate VirtIO, ACPI assumptions and remote management.
Three things to know before offering them:

- **They need their own device profile.** `legacy-9x` guests (95/98/ME) get an IDE disk, an
  rtl8139 NIC, a Pentium-class chipset and a 512 MiB ceiling; giving them VirtIO produces a
  guest that never boots. The profile is applied from the catalog entry, not by hand.
- **They cannot be graded automatically.** There is no WMI, no PowerShell and no agent, so
  those entries are `automation: none`. Scenarios on them are instructor-observed. OnTrak
  will provision them; it will not pretend to grade them.
- **Some media never automates.** Entries with `recipe: manual` have no unattended install,
  so `ontrak image build` refuses and tells the operator to build the guest by hand and
  publish it (`--publish-only`). Budget an evening per platform, once.

## Known limitations

1. **The Windows guest, Incus, ZFS and Guacamole paths are unverified here.** The
   Python control plane is unit-tested (109 tests, no Windows needed); `infra/`
   and the `.ps1` scenarios are reviewed but must be proven on your hardware. Run
   `ontrak doctor`, build one template, and walk one scenario end to end before
   committing a class to it.
2. **The portal is single-node and SQLite-backed.** Fine to roughly 100
   simultaneous students on one control node; beyond that, move to Postgres and a
   second portal replica.
3. **No scheduled class windows.** Prewarming is manual (a cron entry calling
   `ontrak pool prewarm` is the obvious next step).
4. **Instructor audit trail** does not record *which* instructor acted (see above).
5. **Scenario realism is bounded by the image.** New Windows builds move things
   (VBScript deprecation, Defender tamper protection); the fallbacks are in place
   but re-verify after each image refresh.
