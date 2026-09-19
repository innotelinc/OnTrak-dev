# Roadmap

This is a working document: what is verified, what is built but unproven, and what is
planned. Anything in the last two sections is a statement of intent, not a feature.

## Verified in this repository

- Python control plane: catalog, scenarios, sessions, scoring, selection, scheduler,
  generator, portal (student and admin), CLI — `pytest` (280 tests, 1 skipped) and `ruff` clean.
- Scenario and catalog validation, including the grading contract (a check script that
  cannot report an objective fails validation rather than scoring zero forever).
- Scenario generation from fault primitives: every generated scenario is validated before
  it is accepted, and CI generates the full matrix on every push.
- Demo mode end to end: assign → provision → preview check → submit → destroy, against an
  in-memory hypervisor, with results-only storage asserted by tests.
- Guacamole's JSON-auth payload format, cross-checked against the `openssl` CLI (an
  independent implementation of HMAC-prepend → AES-128-CBC → PKCS#7 with a zero IV).
- Guest PowerShell: every `scenarios/**/*.ps1` and `infra/**/*.ps1` is parsed by `pwsh` in CI;
  the Linux and identity scenarios are run end to end against the real `chmod`, `chown`,
  `useradd`, `groupadd` and `sudo` tooling in a mount namespace, setup and check scripts both.
- Per-workload (scenario × platform) template matrices: the same fault on Windows 11 and on
  Ubuntu are two templates, and the pool, status views and automatic assignment follow them.
- The in-house ticket system: rubric-marked write-ups (length, required terms, classification)
  blended with the machine grade at submission, with the rubric kept satisfiable by tests.
- The instructor admin panel: overview, users, scenarios, platforms, tickets, sessions,
  schedule, results, audit — role-gated, and rendering when the hypervisor is unreachable.
- The container stack: the portal image builds, every compose file validates, the portal and
  the console gateway report healthy, a class runs inside the image, and the admin panel
  serves every page with no Incus socket mounted.
- The one-command install, both halves of it. `docker compose up` on a machine with no
  `.env` and no Incus writes the secrets, publishes the shared portal/console key, and brings
  both services up healthy — the login page saying sign-in is not set up until the range is
  pointed at Cerulean, since sign-in itself is Authentik's and there is no local account
  (docs/operations.md), and a payload the portal signed accepted by the live gateway as a
  connection the student can open. The host half runs `infra/bootstrap-host.sh` inside the host's own
  namespaces, and that script was exercised end to end against a real Incus daemon: the
  upstream package install, the daemon, the storage pool, the lab bridge, the project and
  the limits profile — run twice, to prove a re-run changes nothing. What the dev host here
  cannot do is offer `/dev/kvm`, so what it does instead is stop at the KVM check and say
  why; booting a real Windows VM is still a lab-host job (below).
  `scripts/check-first-run-contract.py` checks the arrangement in CI, because a first run
  that silently half-works is the expensive kind of broken, and CI now runs ShellCheck at
  warning severity over every shell file — the first-run setup runs as root on someone
  else's machine before anything else does.

## Built, but not proven on real hardware

These paths are reviewed and tested only up to the Incus boundary; they need a lab host:

- Unattended Windows image builds (the `incus-windows` and `answer-file` builders) and the
  golden-image pipeline.
- WinRM and Incus-agent guest transports against real Windows guests.
- Windows, Server and Office template builds and their scenario fault injection.
- Guacamole deployment, console embedding and TLS in front of the gateway.
- ZFS/btrfs clone performance at class scale (the capacity model in
  [operations.md](operations.md) is arithmetic, not a benchmark).

The first-class fix is the same for all of them: `make check`, build one template, walk one
scenario end to end, then size the pool.

## Next

1. **Microsoft products beyond Office.** Exchange Server, SQL Server, SharePoint and
   Microsoft 365 Apps in more fidelity. The catalog shape already supports it — a product
   entry names the OS it is layered onto — but each needs a build recipe worth trusting.
2. **Cloud identity beyond the simulation.** The identity family runs against a simulated
   directory service. Entra ID / Microsoft 365 sign-in failures, MFA resets and
   conditional-access tickets are a large share of real service-desk volume; they need a
   tenant sandbox rather than a simulation before they can be graded honestly.
3. **Recording and review.** `guac.recording` is wired but unused. Turning it on for
   security scenarios, with an instructor-only playback view, is the obvious assessment
   upgrade (and needs a storage-retention decision, which belongs to ONYX).
4. **Role-based portals.** One instructor role today; cohorts (teacher, TA, marker) and
   per-cohort scenario sets are a small addition to the account model.
5. **Metrics.** Session latency, pool depth over time and pass rates per objective, exported
   for the operator. Deliberately last: it is easy to add and hard to remove.
6. **Scenario packs.** Versioned, signed bundles so a course can pin its scenario set and
   ship it to another range without copying the whole repository.
7. **Publish the image.** The Dockerfile is unbuilt-on-push today: the CI job builds it, but
   nothing pushes it to a registry. A tagged `ghcr.io/innotelinc/ontrak` would make a range
   host a `docker pull` instead of a build (and needs the registry credentials decision).

## Out of scope

- **Windows in a container.** Containers share the Linux kernel; Windows guests are VMs.
  This is not a limitation to be worked around, it is how the platforms differ.
- **Redistributing Windows or Office media.** The repository ships manifests. Operators
  supply licensed media; CI fails the build if an ISO is ever committed.
- **A general-purpose hypervisor UI.** Incus already has one. OnTrak manages the lab, not
  the hypervisor.
- **Progress tracking and gradebooks.** Results-only is a deliberate policy: the grade
  submitted at Complete & End is what OnTrak stores, and the institution's system of record
  stays the system of record.
- **Grading by observing the student's actions.** Grading reads machine state so any correct
  fix passes and the privacy of the student's session stays out of it.
