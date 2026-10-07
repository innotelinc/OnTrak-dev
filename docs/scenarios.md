# Scenarios

## Which scenarios matter most for tech-support training

There is no single right answer, but there is a defensible teaching order. Two
properties decide how much a scenario earns its place in a curriculum:

1. **Volume** — how often a first/second-line technician meets it in real work.
2. **Gradability** — whether "fixed" can be verified automatically and
   unambiguously, so the student gets honest feedback without an instructor
   watching.

Scored on both, the order that this catalogue follows is:

| Rank | Family | Why it earns the time |
| --- | --- | --- |
| 1 | **Network / connectivity** (DNS, IP, gateway, wifi) | The highest-volume class of "nothing works" tickets, and the most gradable: a name either resolves or it does not. It also teaches the diagnostic reflex that transfers to everything else — check the layer you can actually see before blaming the application. |
| 2 | **OS boot & performance** | Very common in the field, and the one family where the student must go *looking* for the cause instead of being handed an error message. Startup persistence, runaway processes and disabled services cover most of what makes a workstation "slow" or "broken at boot". |
| 3 | **Software / corrupted settings** | Dominant in any environment with a line-of-business app. Excellent teaching material because the fix is rarely "reinstall": it is reading a log, correcting a configuration, and clearing the state the crash left behind. |
| 4 | **Hardware / drivers** | Lower ticket volume than the first three, but it is where junior technicians lose the most time, because Device Manager problem codes are not self-explanatory. Worth teaching, but you need a scenario that is genuinely device-level (see the note below). |
| 5 | **Security incidents (simulated)** | Lowest routine volume for general support, highest consequence. Treat it as the capstone: it is the only family that also tests *process* — evidence, documentation, and doing the work in an order that does not destroy the evidence. It is the family where a written record is part of the grade. |

Two practical caveats:

* **Teach in ticket language, not fault language.** Every scenario here is framed
  as a user's complaint. That is the transferable skill: translating "my machine
  is dying" into a measurable symptom.
* **Weight the mix to your organisation.** If your learner cohort supports a
  warehouse of thin clients, wireless and roaming profiles deserve more than a
  device-driver exercise. Use the shipped six as a template of *kinds* of exercise
  and replace the specifics with your own estate's stories — you will find the
  fault-injection and grading machinery is the reusable part, not the ticket text.

### A note on hardware scenarios

You cannot make a real driver fail in software. What you *can* do is put a real
device into a problem state — which is what `hw-driver-device` does: the template
gets a second network adapter (`instance_devices` in `scenario.yaml`), and
`setup.ps1` disables it at the device level, which is exactly what Device Manager
shows as a yellow warning triangle. The student fixes it with enable/rescan/
reinstall-driver, and grading accepts any of those because it grades the outcome
(no disabled or unhealthy adapters) rather than the method.

The extra adapter is an unmanaged `nictype: p2p` device rather than a second card
on the lab bridge. Incus refuses two NICs on one managed network — *"Instance DNS
name conflict ... because both are connected to same network"* — since each adapter
would claim the instance's own DNS record, and `eth0` already owns the lab network.
A `p2p` adapter still carries a live link, so re-enabling it brings the port up.

## The lab fact sheet

Scenarios share one fictional estate so that tickets reinforce each other:

| Thing | Value |
| --- | --- |
| Intranet DNS domain | `ontrak.lab` (served by the lab bridge's own DNS) |
| DNS server | the bridge gateway, e.g. `10.20.0.1` (supplied by DHCP) |
| File server | `fileserver.ontrak.lab` — HTTP on 80, from `infra/lab-services.sh` |
| Staff portal | `portal.ontrak.lab` — HTTP on 8080 |
| Client subnet / DHCP range | `10.20.0.0/24`, DHCP `10.20.0.100-10.20.0.200` |
| Client gateway | `10.20.0.1` |
| Training account | local `student`, member of `Administrators` |
| Student notes file | `C:\Users\student\Desktop\ontrak-notes.txt` |
| Fault artifacts directory | `C:\ProgramData\OnTrak` (Defender-excluded at image build) |
| Scenario scripts on the guest | `C:\ProgramData\OnTrak\scenarios\<id>\`, shared lib in `..\lib\` |

## Anatomy of a scenario

```
scenarios/<id>/
├── scenario.yaml     ticket, objectives + weights, hints, metadata
├── setup.ps1         injects the fault; must confirm success
├── check.ps1         grades each objective; must emit the JSON payload
└── resources/        optional extra files uploaded alongside setup.ps1
```

### `scenario.yaml`

```yaml
id: net-dns-failure            # must match the directory name
title: "Nothing resolves on the intranet"
category: network              # hardware | software | network | os | security
difficulty: 1                  # 1..4, shown to students
minutes: 20                    # suggested time on ticket
pass_score: 80                 # score needed, on top of all critical objectives
requires_internet: true
tags: [dns, dhcp, resolver]
ticket:                        # rendered as a table on the session page
  from: "Priya Raman (Accounting)"
  system: "WORKSTATION-042"
  priority: "High"
briefing: |                    # the student's ticket, verbatim
  ...
objectives:                    # the grading contract
  - id: restore-resolver
    text: "The adapter resolves through the lab DNS server again"
    weight: 40
    critical: true
    hint: "Compare ipconfig /all with a working machine."
hints:                         # revealed one at a time, most generic first
  - "Start with ipconfig /all"
instance_devices:              # optional: extra hardware, applied at template build
  - name: eth1                 # an unmanaged NIC: an extra adapter cannot share the
    type: nic                  # lab bridge with eth0 (Incus rejects two NICs on one
    nictype: p2p               # managed network), and p2p still has a live link
instance_config:               # optional: extra incus config keys
  limits.memory: 4GiB
resources: []                  # optional files uploaded with setup.ps1
reset_notes: |                 # shown to students about what reset does
  ...
```

### The script contract

`setup.ps1` injects the fault and **must** finish with `Write-OnTrakSetupOk`
(which prints `ONTRAK-SETUP-OK`). Template build refuses to snapshot a scenario
whose setup did not confirm, so a half-applied fault can never reach a student.
Write your setup to be idempotent: rebuilding a template is a normal operation, and
so is being asked to run twice (see below).

### Faults that cut their own channel

A build reads `setup.ps1`'s result over the guest's own network (WinRM, or SSH for
Linux). A fault that breaks that network can therefore cut the connection carrying
its own confirmation: `net-static-ip-conflict` turns the adapter off DHCP, and
Windows tears the interface down with the lease, so `ONTRAK-SETUP-OK` never arrives
although the fault applied perfectly.

The build handles that by asking again: a first attempt that came back with **no
output at all** — the signature of a severed channel — is retried on a fresh
connection, to whatever address Incus now reports, up to a small bounded number of
attempts. A run that returns *text* is an answer, even when the answer is no, and is
never repeated.

Two rules follow for a scenario like this:

* **Recognise your own work.** The second ask arrives with the fault already in
  place, so check first and report rather than re-apply — re-applying the addressing
  change would cut the connection the retry is using. See the guard at the top of
  `scenarios/net-static-ip-conflict/setup.ps1`.
* **Don't move a machine that the range has to find.** Incus reports a virtual
  machine's address from its DHCP lease (the golden image carries no Incus agent),
  so a guest that leaves DHCP and takes a *different* address becomes unreachable to
  the console, the grader and the pool: keep the address it already has, and change
  only how it was obtained and what it routes through.

`check.ps1` calls `Add-OnTrakCheck` once per objective and then `Write-OnTrakReport`
exactly once. The payload is JSON between two markers, which makes grading immune
to noise on stdout:

```
###ONTRAK-JSON-BEGIN###
{"checks":[{"objective":"restore-resolver","passed":true,"detail":"dns=10.20.0.1"}]}
###ONTRAK-JSON-END###
```

Both scripts dot-source the shared library:

```powershell
. "$PSScriptRoot\..\..\lib\OnTrak.Common.ps1"
```

`scenarios/_lib/OnTrak.Common.ps1` provides the reporting contract plus helpers
that encode the lab's hard-won details: `Get-OnTrakPrimaryAdapter` (the adapter
that actually carries traffic), `Test-OnTrakDnsName -DnsOnly` (so a hosts entry
cannot fake a working resolver), `Get-OnTrakPnpDevice` (filters phantom devices
left over from imaging), `Test-OnTrakReportField` (grades written notes),
`Get-OnTrakCpuLoad` (samples instead of a single reading), and so on. Add to it
rather than copying logic between scenarios.

### Grading model

* **Weighted objectives** — score is `100 × passed_weight / total_weight`, so
  partial credit is meaningful and the numbers on the session page add up.
* **Critical objectives** — a gate: resolution requires *all* critical objectives
  passed *and* `pass_score` reached. Use it for the objective the ticket actually
  cares about ("the user can reach the file server again"), not for the whole
  scenario. Validation warns if you mark everything critical, because then partial
  credit stops meaning anything.
* **Unreported objectives count as failures.** A check that crashed must never look
  like a pass. Validation statically enforces that `check.ps1` mentions every
  objective id, and a test in `tests/test_scenarios.py` enforces it for the shipped
  catalogue, so this only bites authors who skip `make validate`.
* **Duplicate reports:** if the same objective is reported twice, the failure wins.
  A flapping probe cannot fake a pass.
* **Observations, not verdicts.** `-Detail` should carry the evidence your check
  saw ("dns servers=10.20.0.99; lookup works=False"). It is what students learn
  from, and what an instructor needs when a score is disputed.

## The shipped scenarios

### 1. `net-dns-failure` — "Nothing resolves on the intranet" (network, 1/4)

* **Broken:** a static, non-existent DNS server (`10.20.0.99`) on the primary
  adapter. Everything else is healthy, so the machine looks connected.
* **Fix:** return the adapter to DHCP-supplied DNS (or point it at the working
  resolver), clear the DNS cache, verify.
* **Graded:** resolver restored (critical) / intranet name resolves (critical) /
  file server reachable on 80.
* **Common wrong answer:** `ipconfig /flushdns` and declaring victory. A cached
  entry is not the fault, and the checks ignore the cache.
* **Gotcha:** the check uses `-DnsOnly`, so a hosts-file entry cannot fake it.

### 2. `net-static-ip-conflict` — "A contractor 'optimised' the network settings" (network, 2/4)

* **Broken:** static address with a gateway (`10.20.0.254`) that does not exist.
  The VM stays reachable *on-link*, which is why remote support still works while
  nothing off-subnet does.
* **Fix:** back to DHCP per the site standard, verify gateway and service.
* **Graded:** DHCP enabled (critical) / gateway answers (critical) / file server
  reachable.
* **Common wrong answer:** fixing the address but not the route, then reporting
  "the address is correct now".

### 3. `sw-app-crash` — "The CRM app crashes on launch" (software, 2/4)

* **Broken:** three independent faults around a simulated app: invalid JSON in
  `config.json` (trailing comma *and* a decommissioned server), a wrong per-user
  override in `HKCU\Software\OnTrak\CrmApp`, and a stale lock file left by the crash.
* **Fix:** read the app's log, repair the config, correct the registry override,
  delete the lock, re-run the self-test.
* **Graded:** config parses and points at the approved backend (critical) /
  override correct / lock cleared / self-test exits 0 with `SELFTEST OK` (critical).
* **Common wrong answer:** deleting the lock file only — the app then fails on
  config. This is deliberate: one fix is not enough, which mirrors real crash
  recovery.

### 4. `os-perf-startup` — "Takes ten minutes to boot and then crawls" (os, 3/4)

* **Broken:** an unwanted "PC Speed Booster" burns a full CPU core. It comes back
  from **two** places: a scheduled task at startup (runs on every boot) and a Run
  key (visible to the student at logon). The Print Spooler is also set to Disabled.
* **Fix:** find the process, remove both persistence mechanisms, delete the payload
  file, restore the spooler service.
* **Graded:** CPU load under threshold and no burner process (critical) / both
  persistence entries gone (critical) / payload file deleted / spooler running
  with a non-disabled startup type.
* **Common wrong answer:** killing the process in Task Manager and removing the
  Run key. The scheduled task relaunches it and the CPU objective fails — a
  designed lesson in persistence.
* **Gotcha:** the CPU objective samples 5 readings a second apart rather than
  trusting one number, and separately checks for the process, because "quiet for a
  second" is not "fixed".

### 5. `hw-driver-device` — "Dock Ethernet shows a warning triangle" (hardware, 3/4)

* **Broken:** the second network adapter's device is disabled (a warning triangle
  in Device Manager). The primary adapter is untouched, so remote support still
  works.
* **Fix:** enable the device (or reinstall/rescan the driver) and confirm the link
  is up. Write up what was found.
* **Graded:** no disabled or unhealthy adapters (critical) / both adapters up /
  notes file with `Evidence:` and `Action:` lines.
* **Common wrong answer:** leaving it disabled and calling it "cleanup". Grading is
  outcome-based on purpose: enable, rescan and driver-reinstall all pass.
* **Requires:** `instance_devices` support (the extra NIC) — the build fails loudly
  with a clear message if the second adapter is missing. The extra adapter must be
  an unmanaged NIC (`nictype: p2p`); `ontrak scenario validate` rejects a second card
  on the lab network before the build gets that far.

### 6. `sec-malware-persistence` — "EDR alert: unexpected persistence and a new administrator" (security, 4/4)

* **Broken (simulated, benign):** a `svchost32.vbs` payload running from
  `C:\Users\Public\update`, Run key + scheduled task persistence, a rogue local
  administrator `svc_backup`, a hosts entry redirecting `portal.ontrak.lab`, and
  Defender real-time protection turned off.
* **Fix:** work the four alerts, remove persistence and payload, deal with the
  account, clean the hosts file, re-enable protection, and write up the incident.
* **Graded:** persistence gone (critical) / payload deleted and not running /
  rogue admin no longer privileged / hosts file clean / Defender real-time on
  (critical) / notes with `Evidence:` and `Action:`.
* **Why the write-up is graded:** the professional skill in an incident is the
  record. `Test-OnTrakReportField` requires two labelled lines with real content, so
  "rebooted the PC" earns nothing.
* **Gotchas:** the payload is deliberately harmless (no network, no spreading) —
  this is an investigation exercise, not a malware sample. Real-time protection
  may be locked by Windows 11 **tamper protection**; `setup.ps1` records what
  actually happened and grading reports it honestly instead of pretending the
  fault was applied. See the note in `infra/windows/post-install.ps1` for how to
  bake tamper protection off if your cohort needs that objective to bite.

### 7. `sec-phishing-triage` — "Suspicious invoice email" (security, 3/4)

**What the student finds:** a reported phishing email in the user's mailbox, a placeholder
attachment sitting in `Downloads` with a `.doc.exe` name, and a hosts-file entry the user
added themselves to "fix" an unreachable intranet page.

**Objectives:** name the indicators (spoofed sender domain, failed SPF, the attachment);
isolate the attachment; restore the hosts file; write the incident up.

**Why it is here:** it is the only scenario that grades *process*. Nothing has executed,
so there is no malware to remove — the marks are for recognising the indicators, containing
the artifact that could run, undoing the change the user made, and documenting all three.
A student who quarantines but writes nothing gets partial credit, which is the correct
outcome for a triage ticket.

## Generating scenarios from fault primitives

Writing each scenario by hand does not scale past a handful, and a course that ends up with
four scenarios teaches four things. OnTrak therefore keeps a library of **fault primitives**
in `ontrak/primitives.py`: each one carries a plausible, reversible fault, the objectives
that describe a fix, and the grading that decides whether the fix worked.

```bash
ontrak generate list                       # the primitives, and curated combinations
ontrak generate one --primitive dns-resolver-trapped --scenario gen-dns
ontrak generate one --primitive dns-resolver-trapped --primitive service-disabled \
    --scenario gen-monday-morning --title "Nothing works since this morning"
ontrak generate matrix --prefix gen        # one scenario per primitive
ontrak scenario validate                   # the same gate hand-written scenarios pass
```

Generation is deliberately **not** free-form. A primitive is a code change and gets reviewed,
because it embeds the judgement about what "fixed" means. What generation automates is the
plumbing: ticket text, weighted objectives, the `setup.ps1` that injects the fault, the
`check.ps1` that grades it, and hints.

Three guarantees hold for anything generated:

1. **It is validated before it exists.** `generate` writes the scenario and immediately runs
   the same `ScenarioRepository.validate` that CI runs. A check script that never reports an
   objective fails generation rather than failing a student.
2. **Objective weights are normalised to 100**, so a composed ticket keeps the same pass
   mark semantics as every other scenario.
3. **It is honest about what it needs.** Combining a primitive that needs internet with a
   workload that cannot verify it is refused by selection, not silently graded as zero.

The generated directory is a normal scenario: hand-edit it afterwards if you want, or
regenerate with `--force`. The manifest records `generated_from:` so provenance survives.

## Authoring a new scenario

```bash
mkdir -p scenarios/my-ticket
$EDITOR scenarios/my-ticket/scenario.yaml    # start from a shipped one
$EDITOR scenarios/my-ticket/setup.ps1        # inject; end with Write-OnTrakSetupOk
$EDITOR scenarios/my-ticket/check.ps1        # Add-OnTrakCheck per objective, then Write-OnTrakReport
make validate                                # schema + objective/check contract
.venv/bin/ontrak template build my-ticket  # boots Windows once, injects, snapshots "clean"
.venv/bin/ontrak session start --student you --scenario my-ticket
.venv/bin/ontrak session console <id>      # open the URL Guacamole gave you
```

Checklist before you let students near it:

1. `make validate` is clean.
2. Build the template, open a console, confirm the fault is *visible* to someone
   with no inside knowledge (can a competent tech find it from the ticket text?).
3. Walk the intended fix; confirm the score reaches 100 and the report text is a
   sensible explanation, not a restatement of the answer.
4. Try the most likely wrong fix; confirm it scores less than the pass mark.
5. Reset, and confirm the fault is back and identical (same adapter, same
   addresses, same file paths).

## Known fragility (things to re-verify on a new image or a new Windows build)

| Area | Risk | What to do |
| --- | --- | --- |
| VBScript payloads | Microsoft is retiring the script host; `wscript.exe` may be absent | both scenarios that need it detect that and fall back to a PowerShell payload; keep that pattern |
| Defender | the lab's benign artifacts can be quarantined | image build adds path exclusions for `C:\ProgramData\OnTrak` and `C:\Users\Public\update`; do not exclude `powershell.exe` (AMSI hole) unless a specific scenario forces it |
| Defender state objective | tamper protection can refuse `Set-MpPreference` | `setup.ps1` records the real outcome and grading reports it; bake tamper protection off if the objective must always be actionable |
| Device scenarios | depend on the VM's device inventory | declare hardware in `instance_devices`; don't rely on what "usually" exists |
| Printer scenarios | printer drivers are not guaranteed on client SKUs | not shipped for that reason; if you add one, create it with `Add-Printer` in `setup.ps1` and verify on your image first |
| Evaluation ISOs | 90/180-day expiry | rebuild before a course, or move to volume licensing |
| Cloned SIDs | images are not sysprep'd, so clones share a machine SID | fine standalone; never assume domain join works in a scenario |
