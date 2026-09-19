# Architecture

## The one thing to get straight first

**Windows cannot run in an LXC/Incus container.** Containers share the host's
Linux kernel; Windows needs a Windows kernel. Incus hosts Windows as a **virtual
machine** (QEMU/KVM with VirtIO devices), and OnTrak builds everything on that:

| Property you wanted | How it is delivered |
| --- | --- |
| Instant provisioning | `incus copy` from a copy-on-write snapshot (ZFS/btrfs): seconds, not minutes |
| Clean reset every attempt | destroy the VM, clone the scenario's `clean` snapshot again |
| Many students at once | pre-booted pool + Incus cluster for multi-host; the portal takes requests as they come |
| Scripted configuration | PowerShell over WinRM or the Incus agent, driven by the scenario's `setup.ps1` / `check.ps1` |

If you ever genuinely need *containers* (process isolation with a Windows kernel),
that is Windows containers on Windows hosts, a different stack that cannot host
device/driver/BCD/GPO exercises because those live outside the container boundary.

## Components

```
students ──► portal (FastAPI)  ──► session manager ──► incus ──► Windows VM per student
                  │                     │                          ▲
                  │ sqlite              └── scenario repo           │ WinRM / incus-agent
                  │  (users, sessions,       scenarios/<id>/        │
                  │   results, events)       scenario.yaml          │
                  ▼                          setup.ps1             │
            Guacamole (guacd + webapp) ────── check.ps1 ────────────┘
                  ▲
                  └── signed, encrypted, single-connection payload
```

| Piece | Responsibility | Where |
| --- | --- | --- |
| Portal | Authentik SSO sign-in, workload picker, console iframe, check/complete buttons, results, instructor console | `ontrak/portal/` |
| Session manager | template build, warm pool, allocation, grading, reset, time limits, complete-and-destroy | `ontrak/sessions.py` |
| Incus client | every VM/snapshot operation, via the `incus` CLI | `ontrak/incus.py` |
| Guest driver | runs the scenario scripts inside the guest (WinRM / incus-agent / SSH / no-op) | `ontrak/guest.py` |
| Workload catalog | OS/Office manifests, device profiles, validation, provisioning plans | `ontrak/catalog.py` |
| Media store | resolves media; downloads free media, reverifies checksums, refuses licensed media | `ontrak/media.py` |
| Scenario repo | loads and validates the catalogue and the grading contract | `ontrak/scenarios.py` |
| Fault primitives + generator | reviewed faults, and validated scenario generation from them | `ontrak/primitives.py`, `ontrak/generator.py` |
| Selection | chooses a scenario for a student and explains the choice | `ontrak/selection.py` |
| Scheduler | prewarm windows before a class, drain the pool after it | `ontrak/scheduler.py` |
| Scoring | turns guest JSON into a weighted report | `ontrak/scoring.py` |
| Guacamole links | signed + encrypted SSO payloads for browser consoles | `ontrak/guac.py` |
| Store | users, sessions, submitted results, events (SQLite, WAL, self-migrating) | `ontrak/store.py` |
| Demo mode | in-memory hypervisor + a driver that reports plausible grades | `ontrak/memory.py`, `ontrak/demo.py` |

The control plane has **no long-running state of its own** beyond SQLite. Pool
membership is derived from instance names plus live session rows, so restarting
the portal or the reaper never corrupts anything.

## The VM lifecycle

```
                    ┌──────────────────────────────────────────────────────┐
  infra/build-golden-image.sh                                           │
    Windows ISO ─► incus-windows ─► image ─► post-install.ps1 ─► ontrak-win-base
                    └──────────────────────────────────────────────────────┘
                                          │
  infra/build-templates.sh                ▼        (once per scenario, or after editing one)
    init from image ─► boot ─► setup.ps1 ─► verify ONTRAK-SETUP-OK ─► power off ─► snapshot "clean"
                                          │
                                          ▼                             tpl-<scenario>/clean
  request ─► claim a pre-booted pool VM, or clone the snapshot ─────────────────┘
                                          │
                                          ▼                              ontrak-pool-<scenario>-<n>
  ready ─► student works ─► "Check my work" ─► check.ps1 ─► weighted report
                    │                     │
                    │                     └─► "Reset" ─► destroy VM ─► clone again
                    └─► session ends (TTL, idle recycle, or student finishes) ─► destroy VM
```

### Instance naming

| Pattern | Meaning | Count |
| --- | --- | --- |
| `tpl-<scenario>` | booted, fault-injected VM with snapshot `clean`; powered off | one per scenario |
| `tpl-<scenario>/clean` | the source of truth for that scenario | — |
| `ontrak-pool-<scenario>-<n>` | pre-booted clone awaiting a student | `pool.targets` per scenario |
| `ontrak-sess-<scenario>-<id>` | clone made directly for a session (pool was empty) | transient |

A claimed pool VM keeps its pool name: the session row records which instance a
student owns. Availability is therefore `pool-named, RUNNING, has an IP, and not
referenced by any non-terminal session` — no separate pool bookkeeping to drift.

### Session states

| State | Set when | Who can leave it |
| --- | --- | --- |
| `requested` | row created, waiting for a worker | portal thread / CLI → `allocating` |
| `allocating` | claiming or cloning | manager → `ready` / `error` |
| `provisioning` | pool VM claimed, waiting on the guest | manager → `ready` / `error` |
| `ready` | address + transport confirmed | student opens the page → `in_use` |
| `in_use` | student is working | check → `passed`/`in_use`; reset → `recycling` |
| `checking` | grading in flight | manager → `passed` / `in_use` |
| `passed` | a check met the pass mark with all critical objectives | still usable for practice |
| `recycling` | destroy + re-clone in progress | manager → `ready` / `error` / `destroyed` |
| `destroyed`, `error` | terminal (error is recoverable only by reset) | — |

`passed` is sticky: once a student has demonstrated the fix, breaking it again
does not take the credit back.

## Reset policy: destroy, don't repair

Reset throws the VM away and clones the clean snapshot again. Reasons:

* **Guaranteed state.** A scenario's fault plus whatever the student changed is
  unbounded; undoing it is guesswork. A clone is exact.
* **Cheap on CoW storage.** With ZFS or btrfs a clone shares blocks with the
  template until written, so a "new" 48 GiB Windows VM costs seconds and a few
  hundred MB.
* **Comparable scores.** Every attempt starts from an identical fault, which is
  what makes a class leaderboard meaningful.
* **No leak between students.** A student cannot leave something behind that
  affects the next student.

Costs, and how they are handled:

| Downside | Mitigation |
| --- | --- |
| Boot time on a cold clone (60-120 s) | warm pool (`pool prewarm`) for the scenarios in play |
| Student notes live inside the VM | the ticket tells students to keep notes on the desktop *and* the portal keeps scores; `guac.recording` keeps an audit trail |
| Pool RAM cost | pool targets default to 0; prewarm just before a class |

## Warm pool

A pooled VM is a clone that has already booted Windows and answered a transport
handshake, so handoff is "assign and go". Pool sizing is a RAM decision:

```
resident RAM ≈ Σ_scenarios (target × VM memory)   +  active students × VM memory
```

Because a class normally works one scenario at a time, the default configuration
sets `pool.targets` per scenario and you prewarm the scenario you are about to
teach:

```bash
.venv/bin/ontrak pool prewarm --scenario net-dns-failure --count 30
```

`pool.max_total` is a hard ceiling so a runaway prewarm cannot exhaust the host.
See [operations.md](operations.md) for the arithmetic and a capacity table.

## Access path and security model

* **Isolation.** Student VMs live on a dedicated bridge (`ontrak0`) with NAT to the
  internet and no route to the host's management network. They cannot see each
  other's traffic beyond ARP/DHCP on that bridge.
* **Console.** The student's browser talks to Guacamole, never to RDP directly.
  The portal mints a *signed and AES-encrypted* payload scoped to exactly one
  connection with an expiry (`guac.link_ttl_minutes`). The RDP password is inside
  the ciphertext, so it is neither visible in the URL nor reusable out of band.
* **Portal auth.** scrypt password hashes, HMAC-signed session cookies
  (`SameSite=Lax`, HttpOnly), CSRF double-submit tokens on every POST.
  Authorization is centralised in `load_session()`: instructors may act on any
  session, students only on their own — a test asserts a student cannot open,
  reset or end another student's VM.
* **Guest credentials.** All sessions share the training account baked into the
  golden image unless `session.randomize_credentials` is on. That is acceptable
  because each student has their own VM on an isolated bridge; turn the option on
  if you want per-session secrets and can accept the extra provisioning step.
* **Deliberate lab-only compromise:** the image sets
  `LocalAccountTokenFilterPolicy=1` so remote administration gets a full
  administrator token. Anyone holding the training credentials has local admin on
  that VM. Never reuse this image outside a disposable lab.
* **Student-visible faults.** Students can inspect anything in their own VM,
  including artifacts a scenario planted. Scenarios are designed so that seeing
  the fault is not the same as fixing it, and `check.ps1` is never uploaded before
  grading time.

## Scaling out

`incus.remote` can point at an Incus **cluster** (`incus remote add lab
https://host:8443`). Placement across hosts, live migration and storage are then
the cluster's problem; nothing in OnTrak changes except that you prewarm enough
VMs for the whole class. Keep the portal, Guacamole and the SQLite file on one
control node.

For ≥100 simultaneous students, add: a real database or Postgres for SQLite, a
TLS reverse proxy in front of the portal and Guacamole, and a scheduled
prewarm/teardown window per class.

## Provisioning: which path, and why

The catalog decides how a request is served, from cheapest to most expensive, using only
facts the host can supply (`ontrak catalog plan <entry>`):

```
warm-pool (~5s)  →  container-image (~8s)  →  clone-template (~45s)
                 →  image-launch (~90s)    →  build-image (operator task)
                 →  unsupported (manual platform)
```

Two properties matter. The planner is **pure** — it takes host facts as arguments, so it
can be printed, explained and tested without Incus. And it is **honest**: when an entry's
media is missing or its image has never been built, the plan says so and names the command
that fixes it instead of failing halfway through a student's first lesson.

The scenario's `workload:` field names the catalog entry it should be built on, which is
how the same fault can be offered on Windows 11 or Ubuntu. A scenario without one falls
back to the site's golden image (`incus.image_alias`).

## Results-only grading

A student may check their work as often as they like: those runs execute the real grading
scripts and show real feedback, but they are **not stored**. `persist_progress: false` is
the default, and the portal keeps the last preview in process memory so the page can still
show it.

`Complete & End` is the one graded run whose result is written to the results table. It
sets the session terminal, destroys the instance, and leaves an immutable record. Two
consequences worth knowing:

- **Nothing to reset after submission.** The machine is gone, so the next student cannot
  inherit anything and the score cannot be revised by further tinkering.
- **The score means one thing.** Objective weights total 100 in every scenario — enforced
  by validation — so an 80% pass mark is comparable across the whole catalogue.

## Extension points

* **New scenario:** drop a directory in `scenarios/`; validated by
  `ontrak scenario validate`; no code change. See [scenarios.md](scenarios.md).
* **Guest transport:** `guest.driver = winrm | incus-exec | null`. `incus-exec`
  removes the network dependency (useful for scenarios that break the NIC
  entirely) at the cost of needing virtio-vsock in the image.
* **Extra hardware in a scenario:** `instance_devices` in `scenario.yaml`, applied
  at template build (that is how the driver scenario gets a second NIC to break).
* **Session limits:** `session.ttl_minutes`, `idle_recycle_minutes`,
  `max_per_student`, `randomize_credentials`.
* **Domain-based scenarios:** install a domain controller VM, then point
  `guest.user`/`guest.winrm_transport` at Kerberos. Nothing else assumes
  standalone machines except that the image is not sysprep'd.
