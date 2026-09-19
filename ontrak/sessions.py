"""Session lifecycle.

The model is deliberately simple and matches the reset policy: **a session is
always a fresh clone of a scenario template, and a reset throws the VM away and
clones again.** No in-place mutation, no drift, no "did the last student leave
something behind". On ZFS/btrfs the clone is copy-on-write, so throwing VMs
away is cheaper and far more reliable than trying to un-break a machine.

Two kinds of instance exist:

``tpl-<scenario>``
    Booted once by ``build-templates``, fault injected, powered off, then
    snapshotted as ``clean``. This is the source of truth for the scenario.
``ontrak-pool-<scenario>-<n>``
    A pre-booted clone waiting to be claimed. Keeps handoff instant for a class
    of 30 instead of making everyone wait a minute for Windows to boot.

A claimed pool instance is recorded on the session row; availability is derived
from "pool-named instance that no live session references", so the pool needs no
bookkeeping of its own and survives control-plane restarts.
"""

from __future__ import annotations

import contextlib
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .catalog import Catalog, CatalogEntry, CatalogError
from .config import Settings
from .guest import (
    BaseDriver,
    GuestError,
    build_driver,
    build_shell_driver,
    quote_ps,
    quote_sh,
)
from .incus import IncusClient, IncusError
from .models import ScoreReport, Session, SessionState, iso, parse_iso, seconds_since, utcnow
from .scenarios import (
    CHECK_NAMES,
    COMMON_LIB,
    LINUX,
    SETUP_NAMES,
    SETUP_OK_MARKER,
    SHELL_COMMON_LIB,
    WINDOWS,
    ScenarioError,
    ScenarioRepository,
)
from .scoring import evaluate
from .store import Store
from .tickets import TicketGrade, blend
from .tickets import grade as mark_ticket

POOL_SNAPSHOT = "clean"
TEMPLATE_PSEUDO_STUDENT = "<template>"

# Printed by the console-transport provisioning script once sshd is listening; the
# template build asserts on it rather than trusting the exit code of a pipeline.
CONSOLE_SETUP_MARKER = "ontrak-console-ready"


def console_transport_script(settings: Settings) -> str:
    """The shell that turns a Linux guest into one the browser console can open.

    Kept as a module-level function over `settings` so the exact text a template
    build runs is the text a test can read: this script is the difference between a
    console that works and a page that says the remote desktop server is
    unreachable, and asserting on a string is how it stays that way.
    """
    password = settings.guest.password
    user = settings.guest.linux_user or "root"
    port = settings.guest.ssh_port
    return "\n".join(
        [
            "set -e",
            "export DEBIAN_FRONTEND=noninteractive",
            # Only the daemon is needed; the client tools, docs and recommends are
            # dead weight carried by every clone of the image.
            "if ! command -v sshd >/dev/null 2>&1; then",
            "  (apt-get update -qq && apt-get install -y -qq --no-install-recommends"
            " openssh-server) >/dev/null 2>&1 || apt-get install -y -qq"
            " --no-install-recommends openssh-server >/dev/null 2>&1",
            "fi",
            "mkdir -p /run/sshd",
            # `chpasswd` rather than `passwd`: it does not prompt, and it *unlocks* an
            # account a scenario locked — which is exactly why it runs last.
            f"echo {quote_sh(f'{user}:{password}')} | chpasswd",
            "for key in PermitRootLogin PasswordAuthentication; do",
            '  if grep -qE "^#?$key" /etc/ssh/sshd_config; then',
            '    sed -i "s|^#*$key.*|$key yes|" /etc/ssh/sshd_config',
            '  else',
            '    echo "$key yes" >> /etc/ssh/sshd_config',
            "  fi",
            "done",
            # Ubuntu and Debian read `Include /etc/ssh/sshd_config.d/*.conf` from near
            # the *top* of sshd_config, and sshd keeps the **first** value it sees per
            # keyword — so a drop-in outranks the main file, and an image's own drop-in
            # could outrank ours. `00-` rather than `99-` is what puts our two lines
            # first, where nothing but a file named ahead of it can override them.
            "mkdir -p /etc/ssh/sshd_config.d",
            "printf '%s\\n' 'PermitRootLogin yes' 'PasswordAuthentication yes'"
            " > /etc/ssh/sshd_config.d/00-ontrak-console.conf",
            "systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd"
            " >/dev/null 2>&1 || true",
            "systemctl start ssh >/dev/null 2>&1 || systemctl start sshd"
            " >/dev/null 2>&1 || service ssh start >/dev/null 2>&1 || /usr/sbin/sshd",
            f"ss -ltn 2>/dev/null | grep -q ':{port} ' || sleep 2",
            f"ss -ltn 2>/dev/null | grep -q ':{port} '",
            f"printf '{CONSOLE_SETUP_MARKER}\\n'",
        ]
    )

# Workload id prefixes that mean "a Linux guest". Used only to label pool rows for
# the operator view; the scenario's own `platform` decides how a guest is driven.
LINUX_TAG_PREFIXES = {
    "alpine",
    "almalinux",
    "archlinux",
    "arch",
    "centos",
    "debian",
    "fedora",
    "gentoo",
    "kali",
    "linuxmint",
    "nixos",
    "opensuse",
    "oracle",
    "raspios",
    "rhel",
    "rockylinux",
    "ubuntu",
    "void",
    "voidlinux",
    "ontrak-idp",
}


class SessionError(RuntimeError):
    """Raised when a session cannot be created, reset or graded."""


@dataclass
class PoolStatus:
    scenario_id: str
    target: int
    ready: int
    claimed: int
    total: int
    template_ready: bool
    # Which platform this pool is for. Empty means the site's golden image, which is
    # what scenarios that do not name a workload use.
    workload: str = ""

    @property
    def label(self) -> str:
        return f"{self.scenario_id}@{self.workload}" if self.workload else self.scenario_id

    @property
    def platform(self) -> str:
        """Which driver family this pool needs, inferred from the workload id."""
        if not self.workload:
            return WINDOWS
        return LINUX if self.workload.split("-")[0] in LINUX_TAG_PREFIXES else WINDOWS

    @property
    def deficit(self) -> int:
        """How many instances to create to reach the target.

        Counted against *all* pool-named instances, including the ones students
        have claimed. A claimed VM still has its pool name, so a class where every
        student holds one of 30 target VMs shows a deficit of 0 rather than asking
        the reaper to build 30 more and exhaust the host's memory.
        """
        return max(0, self.target - self.total)

    @property
    def shortfall(self) -> int:
        """Unclaimed instances missing right now (handoff latency, not capacity)."""
        return max(0, self.target - self.ready)

    @property
    def healthy(self) -> bool:
        return self.deficit == 0


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        repo: ScenarioRepository | None = None,
        incus: IncusClient | None = None,
        driver: BaseDriver | None = None,
        catalog: Catalog | None = None,
        shell_driver: BaseDriver | None = None,
    ):
        self.settings = settings
        self.store = store
        self.repo = repo or ScenarioRepository(settings.scenarios_dir)
        self.incus = incus
        self.driver = driver or build_driver(settings)
        # Linux guests are driven over the Incus agent (or SSH) rather than WinRM, so
        # the manager holds both transports and picks per scenario.
        self.shell_driver = shell_driver or build_shell_driver(settings, client=incus)
        self.catalog = catalog
        if catalog is not None:
            # Lets instance-name parsing recognise workload suffixes without an import
            # cycle between config and the catalog. The catalog must be *loaded* first:
            # an unloaded one is indistinguishable from an empty one, and name parsing
            # would then read `pool-<scenario>-<workload>-1` as a different scenario's
            # pool — silently, because an unmatched name just looks like "no machines".
            catalog.load()
            settings.incus.known_workloads = tuple(catalog.entries)
        # Kept as an attribute (not ``self.repo``) because the scheduler and the
        # portal both reach for ``manager.repository``.
        self.repository = self.repo
        # One lock per session, so a portal worker thread cannot race an
        # instructor action or another worker into provisioning the same session
        # twice (which would clone two VMs and orphan one of them).
        self._lock_guard = threading.Lock()
        self._session_locks: dict[int, threading.Lock] = {}

    # ------------------------------------------------------------------
    # infrastructure helpers
    # ------------------------------------------------------------------
    def _require_incus(self) -> IncusClient:
        if self.incus is None:
            raise SessionError(
                "no Incus connection available: the `incus` binary is not on PATH. "
                "Run infra/bootstrap-host.sh, then `ontrak doctor` to confirm."
            )
        if hasattr(self.incus, "available") and not self.incus.available():
            raise SessionError(
                "no Incus connection available: the `incus` binary is not on PATH. "
                "Run infra/bootstrap-host.sh, then `ontrak doctor` to confirm."
            )
        return self.incus

    def _guest_path(self, *parts: str) -> str:
        """Windows guest path under the work directory."""
        root = self.settings.guest.work_dir.rstrip("\\")
        return "\\".join([root, *parts])

    def _posix_path(self, *parts: str) -> str:
        """Linux guest path under the work directory."""
        root = self.settings.guest.linux_work_dir.rstrip("/")
        return "/".join([root, *parts])

    def _join(self, scenario, *parts: str) -> str:
        """Path inside whichever guest platform the scenario targets."""
        if getattr(scenario, "platform", WINDOWS) == LINUX:
            return self._posix_path(*parts)
        return self._guest_path(*parts)

    def _driver_for(self, scenario) -> BaseDriver:
        """The transport this scenario's guest speaks."""
        if getattr(scenario, "platform", WINDOWS) == LINUX:
            return self.shell_driver
        return self.driver

    @property
    def _lib_source(self) -> Path:
        return self.settings.scenarios_dir / "_lib" / COMMON_LIB

    @property
    def _shell_lib_source(self) -> Path:
        return self.settings.scenarios_dir / "_lib" / SHELL_COMMON_LIB

    def _guest_args(self, session: Session) -> dict:
        return {"host": session.host_ip, "instance": session.instance}

    # ------------------------------------------------------------------
    # templates and pools, keyed by (scenario, workload)
    # ------------------------------------------------------------------
    def workload_pairs(self, ids: list[str] | None = None) -> list[tuple]:
        """Every (scenario, workload) pair the lab can offer.

        A scenario that names workloads is offered once per workload, each with its own
        template and pool. A scenario that names none is offered once on the site's
        golden image, which is what every pre-catalog scenario does.
        """
        pairs = []
        for scenario in self.repo.list():
            if ids and scenario.id not in ids:
                continue
            workloads = scenario.platform_workloads
            if not workloads or self.catalog is None:
                pairs.append((scenario, ""))
                continue
            for workload_id in workloads:
                try:
                    self.catalog.get(workload_id)
                except CatalogError:
                    self.store.log_event(
                        "workload_unknown",
                        f"scenario {scenario.id} names workload {workload_id!r}, not in the catalog",
                    )
                    continue
                pairs.append((scenario, workload_id))
        return pairs

    def template_status(self) -> list[dict]:
        incus = self.incus
        rows = []
        for scenario, workload in self.workload_pairs():
            name = self.settings.incus.template_name(scenario.id, workload)
            exists = bool(incus and incus.exists(name))
            rows.append(
                {
                    "scenario_id": scenario.id,
                    "workload": workload,
                    "platform": scenario.platform,
                    "name": name,
                    "exists": exists,
                    "snapshot": bool(exists and incus and incus.has_snapshot(name, POOL_SNAPSHOT)),
                    "running": bool(exists and incus and incus.instance_status(name) == "RUNNING"),
                    "ready": bool(exists and incus and incus.has_snapshot(name, POOL_SNAPSHOT)),
                }
            )
        return rows

    def scenario_availability(self, scenario_id: str, workload: str | None = None) -> str:
        """``""`` when a session can start, otherwise the reason it cannot.

        Provisioning needs exactly one of two things: a booted machine in the pool
        to claim, or the template's ``clean`` snapshot to clone. When neither
        exists the session is doomed before it starts — which is how a student
        ended up holding a session whose entire content was
        ``template tpl-sw-app-crash is missing snapshot clean``: the portal took
        the request, the worker failed in a thread, and the row came back ``error``
        carrying a message written for an operator.

        Asking first means a scenario the range cannot run is refused before a
        session row exists, so the student gets something they can act on (and hand
        to an instructor) instead of a burned slot.
        """
        scenario = self.repo.get(scenario_id)  # raises ScenarioError for unknown ids
        workload = self._resolve_workload(scenario_id, workload)
        incus = self.incus
        if incus is None:  # demo / CI: there is nothing to probe
            return ""
        template = self.settings.incus.template_name(scenario.id, workload)
        label = f"{scenario.id}@{workload}" if workload else scenario.id
        base_image = self._base_image(scenario, workload)
        try:
            if incus.exists(template) and incus.has_snapshot(template, POOL_SNAPSHOT):
                return ""
            if self._available_pool(scenario.id, workload=workload):
                return ""
            image_published = incus.image_exists(base_image)
        except IncusError:
            # An unreachable hypervisor cannot tell us this scenario is *unrunnable*,
            # only that we cannot tell. Claiming a block here would refuse every
            # scenario on the range during an Incus outage; the session start path
            # reports the hypervisor failure the way it always has.
            return ""
        # Nothing can serve it. Name the layer that is actually missing, because
        # the two have different repairs and only one of them is a template build.
        if not image_published:
            entry = self.workload_entry(workload) if workload else self.workload_for(scenario)
            if entry is not None:
                return (
                    f"{scenario.id} is not available on this range yet: the "
                    f"{base_image!r} image it is built from has not been published. "
                    f"Ask an instructor to run `ontrak image build {entry.id}`."
                )
            return (
                f"{scenario.id} is not available on this range yet: the golden image "
                f"{base_image!r} has not been built, so every scenario that runs on it "
                "is unavailable. Ask an instructor to run infra/build-golden-image.sh."
            )
        return (
            f"{scenario.id} is not available on this range yet: its template "
            f"{template} has not been built. Ask an instructor to run "
            f"`ontrak template build {label}`."
        )

    # How long the dashboard's unavailability map is reused. Every probe behind it
    # is an `incus` CLI round trip, so asking on every page load made the dashboard
    # pay for it on each render; a scenario's template does not appear and vanish
    # within half a minute.
    UNAVAILABLE_TTL_SECONDS = 30.0

    def unavailable_scenarios(self) -> dict[str, str]:
        """``{scenario_id: reason}`` for every scenario this range cannot start.

        Computed per *pair* — a scenario offered on more than one workload counts as
        available when any of them can run, since the student picks the scenario and
        the portal resolves the workload. Cached briefly (see
        :attr:`UNAVAILABLE_TTL_SECONDS`) so the dashboard is not paying for a probe
        per scenario on every render.
        """
        if self.incus is None:  # demo / CI
            return {}
        now = time.monotonic()
        cached = getattr(self, "_unavailable_cache", None)
        if cached is not None and now - cached[0] < self.UNAVAILABLE_TTL_SECONDS:
            return dict(cached[1])
        try:
            startable = {
                (row["scenario_id"], row["workload"])
                for row in self.template_status()
                if row["ready"]
            }
            for status in self.pool_status():
                if status.ready:
                    startable.add((status.scenario_id, status.workload))
            reasons: dict[str, str] = {}
            for scenario, workload in self.workload_pairs():
                if (scenario.id, workload) in startable:
                    reasons.pop(scenario.id, None)
                    continue
                reasons.setdefault(scenario.id, self.scenario_availability(scenario.id, workload))
        except IncusError as exc:
            # Every probe behind this map is an Incus round trip, so an unreachable
            # hypervisor fails all of them. Refusing every scenario would turn an
            # Incus outage into "this range has no scenarios" — the dashboard already
            # says the hypervisor is the problem, in its own banner. Not cached, so
            # the map fills in as soon as Incus answers again.
            self.store.log_event("unavailable_probe_failed", str(exc))
            return {}
        self._unavailable_cache = (now, dict(reasons))
        return reasons

    def build_templates(
        self, ids: list[str] | None = None, force: bool = False, workloads: list[str] | None = None
    ) -> dict[str, str]:
        """Build/replace scenario templates. Returns ``{'<scenario>@<workload>': status}``."""
        results: dict[str, str] = {}
        # An id that matches no scenario used to filter the pairs down to nothing, so
        # the CLI printed no lines at all and exited 0: a typo read exactly like a
        # successful build. Report it as that scenario's outcome instead, so the rest
        # of the request still runs and the exit code is non-zero.
        for scenario_id in ids or []:
            try:
                self.repo.get(scenario_id)
            except ScenarioError as exc:
                results[scenario_id] = f"failed: {exc}"
        for scenario, workload in self.workload_pairs(ids):
            if workloads and workload not in workloads:
                continue
            key = f"{scenario.id}@{workload}" if workload else scenario.id
            try:
                self.ensure_template(scenario.id, workload=workload, force=force)
                results[key] = "ready"
            except (SessionError, IncusError, GuestError) as exc:
                results[key] = f"failed: {exc}"
                self.store.log_event("template_failed", f"{key}: {exc}")
        return results

    def ensure_template(self, scenario_id: str, force: bool = False, workload: str = "") -> str:
        """Create ``tpl-<scenario>[-<workload>]`` with a ``clean`` snapshot, idempotently.

        Boots a clone of the workload's image, applies the fault, shuts the guest down
        and snapshots. Idempotent because re-running a template build is a normal
        operator move after editing a scenario. One template per (scenario, workload)
        pair is what lets the same fault be offered on Windows 11 and Ubuntu without
        the scenario being written twice.
        """
        incus = self._require_incus()
        scenario = self.repo.get(scenario_id)
        if workload and workload not in scenario.platform_workloads and scenario.platform_workloads:
            raise SessionError(
                f"scenario {scenario_id} is not offered on workload {workload!r}; "
                f"it declares {', '.join(scenario.platform_workloads)}"
            )
        name = self.settings.incus.template_name(scenario_id, workload)

        if not force and incus.exists(name) and incus.has_snapshot(name, POOL_SNAPSHOT):
            return name

        base_image = self._base_image(scenario, workload)
        if not incus.image_exists(base_image):
            entry = self.workload_entry(workload)
            if entry is not None:
                raise SessionError(
                    f"workload image {base_image!r} for scenario {scenario_id} is not published "
                    f"yet; build it with `ontrak image build {entry.id}` (media: "
                    f"{entry.media.source}/{entry.media.kind})"
                )
            raise SessionError(
                f"golden image {base_image!r} not found; run "
                "infra/build-golden-image.sh first (or `ontrak doctor` for details)"
            )

        if incus.exists(name):
            incus.stop_instance(name, force=True)
            incus.delete_instance(name, force=True)

        profiles = [p for p in ("default", self.settings.incus.profile) if p]
        incus.create_instance(name, base_image, profiles)
        # The workload decides the hardware profile (legacy guests cannot use VirtIO
        # and need different disks/NICs), then the scenario layers on any extra
        # hardware it needs: hardware cannot be added from inside the guest, so it
        # has to exist before first boot.
        entry = self._apply_workload(name, scenario, workload)
        if entry is not None:
            self.store.log_event(
                "workload_applied",
                f"{scenario_id}@{workload} -> {entry.id} (profile {entry.device_profile}, "
                f"{entry.resources.cpu} CPU / {entry.resources.memory})",
            )
        self._apply_instance_spec(name, scenario)

        driver = self._driver_for(scenario)
        session = Session(
            id=None,
            student=TEMPLATE_PSEUDO_STUDENT,
            scenario_id=scenario_id,
            state=SessionState.PROVISIONING,
            instance=name,
            rdp_user=self.settings.guest.user,
            rdp_password=self.settings.guest.password,
            workload=workload,
        )
        incus.start_instance(name)
        try:
            self._await_guest(session, self._ready_timeout(scenario), driver=driver)
            self._upload_scenario_files(session, scenario, include_setup=True, driver=driver)
            setup_name = SETUP_NAMES[scenario.platform]
            result = driver.run_script_file(
                self._join(scenario, "scenarios", scenario_id, setup_name),
                timeout=self.settings.session.check_timeout_seconds,
                **self._guest_args(session),
            )
            combined = (result.stdout or "") + (result.stderr or "")
            if not result.ok or SETUP_OK_MARKER not in combined:
                raise SessionError(
                    f"{setup_name} for {scenario_id} did not report {SETUP_OK_MARKER} "
                    f"(exit {result.exit_code}). Output tail: {combined[-800:].strip()}"
                )
            self._provision_console_transport(session, scenario, driver)
        finally:
            # Fault injection may leave the guest unresponsive (broken NIC, runaway
            # CPU). Force-stop anyway: the snapshot must capture the fault, and a
            # half-built template is worse than a hard power-off.
            with contextlib.suppress(IncusError):
                self._require_incus().stop_instance(name, force=True, timeout=60)

        incus.create_snapshot(name, POOL_SNAPSHOT)
        label = f"{scenario_id}@{workload}" if workload else scenario_id
        self.store.log_event("template_built", f"{label} -> {name}/{POOL_SNAPSHOT}")
        return name

    def _provision_console_transport(self, session: Session, scenario, driver: BaseDriver) -> None:
        """Put an sshd in this Linux template, so the browser console can be a shell.

        Guacamole speaks RDP and SSH. A Linux container answers no RDP at all, so an
        RDP connection pointed at one produced the console iframe's
        "the remote desktop server is currently unreachable" — a page that blamed the
        student's machine for a transport that was never going to exist, and said
        nothing about the scenario being fine.

        So when ``guac.linux_ssh`` is on, a Linux template is built with sshd running,
        root's password set to the lab credential, and password auth permitted. It runs
        *after* the fault is injected and *after* the setup script has verified it, and
        is the last thing written before the snapshot: a fault that touches accounts or
        permissions (``id-locked-account``, ``linux-sudo-delegation``) must not be able
        to take the console's credential with it, and re-asserting it here is what makes
        that true rather than lucky.

        Off by default and gated on the setting, because it is the one step in a
        template build that reaches the network (apt) and opens a port in every Linux
        guest. A build without it behaves exactly as before.
        """
        if getattr(scenario, "platform", WINDOWS) != LINUX:
            return
        if not self.settings.guac.linux_ssh:
            return
        # An empty lab password would make `chpasswd` set an empty one — a console
        # anyone on the lab network can open as root. Refusing is the only reading of
        # "this deployment has no credential for the guest" that cannot become that.
        if not self.settings.guest.password:
            raise SessionError(
                "guac.linux_ssh is on but guest.password is empty, so there is nothing "
                "to authenticate the console with. Set ONTRAK_GUEST__PASSWORD (or turn "
                "guac.linux_ssh off)."
            )

        script = console_transport_script(self.settings)
        user = self.settings.guest.linux_user or "root"
        result = driver.run_shell(
            script,
            timeout=self.settings.session.check_timeout_seconds,
            **self._guest_args(session),
        )
        combined = (result.stdout or "") + (result.stderr or "")
        if not result.ok or CONSOLE_SETUP_MARKER not in combined:
            raise SessionError(
                "the SSH console transport was not installed, so the template's console "
                f"would report the remote desktop server as unreachable (exit "
                f"{result.exit_code}). Output tail: {combined[-800:].strip()}"
            )
        self.store.log_event(
            "console_transport",
            f"{session.scenario_id}: sshd on port {self.settings.guest.ssh_port} as "
            f"{user} (guac.linux_ssh)",
        )

    def _ready_timeout(self, scenario) -> int:
        if getattr(scenario, "platform", WINDOWS) == LINUX:
            return self.settings.guest.linux_ready_timeout_seconds
        return self.settings.guest.ready_timeout_seconds

    # ------------------------------------------------------------------
    # workloads (which OS/build a guest runs)
    # ------------------------------------------------------------------
    def workload_for(self, scenario) -> CatalogEntry | None:
        """Resolve the catalog entry a scenario declares, if any.

        A scenario may name a workload (``workload: win11-24h2``) to say "build this
        fault on that platform". Without a catalog, or without the field, we fall
        back to the site's golden image, which is what older scenarios assume.
        """
        workload_id = getattr(scenario, "workload", "") or ""
        if not workload_id or self.catalog is None:
            return None
        try:
            return self.catalog.get(workload_id)
        except CatalogError:
            self.store.log_event(
                "workload_unknown",
                f"scenario {scenario.id} names workload {workload_id!r}, which is not in the catalog",
            )
            return None

    def workload_entry(self, workload_id: str) -> CatalogEntry | None:
        """Resolve an explicit workload id through the catalog."""
        if not workload_id or self.catalog is None:
            return None
        try:
            return self.catalog.get(workload_id)
        except CatalogError:
            self.store.log_event("workload_unknown", f"workload {workload_id!r} is not in the catalog")
            return None

    def _base_image(self, scenario, workload: str = "") -> str:
        """The Incus image alias a template should be built from.

        The pair's workload wins when it has one; otherwise the scenario's own declared
        workload, and otherwise the site's golden image.
        """
        entry = self.workload_entry(workload) if workload else self.workload_for(scenario)
        if entry is None:
            return self.settings.incus.image_alias
        if entry.media.kind == "image" or entry.recipe in {"image-alias", "container-image"}:
            return entry.image_alias
        # An ISO-based workload is turned into an image once by
        # `ontrak image build`, which publishes it under this alias.
        return f"ontrak-{entry.id}"

    def _apply_workload(self, name: str, scenario, workload: str = "") -> CatalogEntry | None:
        """Apply a workload's device profile and resource limits to a new instance.

        This is what makes the legacy platforms work at all: Windows 95/98/ME cannot
        use VirtIO and need an IDE disk, an emulated NIC and a chipset they recognise,
        and none of that can be changed from inside the guest.
        """
        entry = self.workload_entry(workload) if workload else self.workload_for(scenario)
        if entry is None:
            return None
        incus = self._require_incus()
        for device_name, spec in entry.resolved_devices().items():
            options = dict(spec.get("options") or {})
            if spec.get("type") == "nic" and options.get("network") in {None, "lab"}:
                options["network"] = self.settings.incus.network
            incus.remove_device(name, device_name)
            incus.add_device(name, str(spec.get("type", "disk")), device_name, **options)
        incus.set_configs(name, entry.resolved_config())
        return entry

    def _apply_instance_spec(self, name: str, scenario) -> None:
        """Apply scenario-declared instance config and devices to a new instance."""
        incus = self._require_incus()
        if scenario.instance_config:
            incus.set_configs(name, scenario.instance_config)
        for device in scenario.instance_devices:
            options = {k: v for k, v in device.items() if k not in {"name", "type"}}
            # A nic device on a network name is the common case: resolve it to the
            # configured lab bridge so scenarios stay portable.
            if device.get("type") == "nic" and options.get("network") in {None, "lab"}:
                options["network"] = self.settings.incus.network
            incus.add_device(name, device["type"], device["name"], **options)

    # ------------------------------------------------------------------
    # warm pool
    # ------------------------------------------------------------------
    def _all_instances(self):
        return self._require_incus().list_instances()

    def _claimed_instances(self) -> set[str]:
        live = self.store.list_sessions(limit=2000)
        return {s.instance for s in live if s.instance and not s.state.is_terminal}

    def _pool_instances(self, scenario_id: str, instances=None, workload: str = "") -> list:
        """Pool instances for exactly this (scenario, workload) pair.

        Matched by parsing the name against the known workloads rather than by prefix:
        ``pool-a-1`` (no workload) and ``pool-a-linux-1`` would otherwise both look like
        pools for the same scenario.
        """
        instances = instances if instances is not None else self._all_instances()
        parser = self.settings.incus.parse_pool_name
        out = []
        for instance in instances:
            parsed = parser(instance.name)
            if not parsed:
                continue
            scenario, found_workload, _index = parsed
            if scenario == scenario_id and found_workload == workload:
                out.append(instance)
        return out

    def _available_pool(self, scenario_id: str, instances=None, workload: str = "") -> list:
        claimed = self._claimed_instances()
        return [
            i
            for i in self._pool_instances(scenario_id, instances, workload)
            if i.running and i.ipv4 and i.name not in claimed
        ]

    def _next_pool_index(self, scenario_id: str, instances=None, workload: str = "") -> int:
        highest = 0
        parser = self.settings.incus.parse_pool_name
        for instance in self._pool_instances(scenario_id, instances, workload):
            parsed = parser(instance.name)
            if parsed:
                highest = max(highest, parsed[2])
        return highest + 1

    def pool_status(self, scenario_id: str | None = None, workload: str | None = None) -> list[PoolStatus]:
        incus = self.incus
        if incus is None:
            return []
        instances = self._all_instances()
        claimed = self._claimed_instances()
        rows: list[PoolStatus] = []
        for scenario, pair_workload in self.workload_pairs():
            if scenario_id and scenario.id != scenario_id:
                continue
            if workload is not None and pair_workload != workload:
                continue
            pool = self._pool_instances(scenario.id, instances, pair_workload)
            ready = len([i for i in pool if i.running and i.ipv4 and i.name not in claimed])
            owned = len([i for i in pool if i.name in claimed])
            template = self.settings.incus.template_name(scenario.id, pair_workload)
            rows.append(
                PoolStatus(
                    scenario_id=scenario.id,
                    workload=pair_workload,
                    target=self.settings.pool.target_for(scenario.id, pair_workload),
                    ready=ready,
                    claimed=owned,
                    total=len(pool),
                    template_ready=incus.has_snapshot(template, POOL_SNAPSHOT)
                    if incus.exists(template)
                    else False,
                )
            )
        return rows

    def prewarm(self, scenario_id: str, count: int, workload: str = "") -> int:
        """Create ``count`` booted, unclaimed VMs for a (scenario, workload) pair.

        Returns how many were actually created (bounded by ``pool.max_total``).
        """
        self._require_incus()
        if count <= 0:
            return 0
        instances = self._all_instances()
        pool_total = sum(
            len(self._pool_instances(s.id, instances, w)) for s, w in self.workload_pairs()
        )
        budget = max(0, self.settings.pool.max_total - pool_total)
        to_create = min(count, budget)
        if to_create == 0:
            self.store.log_event(
                "prewarm_skipped", f"{scenario_id}: pool at max_total={self.settings.pool.max_total}"
            )
            return 0
        created = 0
        index = self._next_pool_index(scenario_id, instances, workload)
        for _ in range(to_create):
            name = self.settings.incus.pool_name(scenario_id, index, workload)
            index += 1
            try:
                self._provision_pool_instance(scenario_id, name, workload)
            except (SessionError, IncusError, GuestError) as exc:
                self.store.log_event("prewarm_failed", f"{scenario_id}@{workload} {name}: {exc}")
                break
            created += 1
        if created:
            label = f"{scenario_id}@{workload}" if workload else scenario_id
            self.store.log_event("prewarmed", f"{label}: {created} VM(s)")
        return created

    def _provision_pool_instance(self, scenario_id: str, name: str, workload: str = "") -> str:
        scenario = self.repo.get(scenario_id)
        self._require_incus()
        self._clone_from_template(scenario, name, workload)
        session = Session(
            id=None,
            student=TEMPLATE_PSEUDO_STUDENT,
            scenario_id=scenario_id,
            state=SessionState.PROVISIONING,
            instance=name,
            rdp_user=self.settings.guest.user,
            rdp_password=self.settings.guest.password,
            workload=workload,
        )
        self._await_guest(session, self._ready_timeout(scenario), driver=self._driver_for(scenario))
        return name

    def refill_pool(self, scenario_ids: list[str] | None = None) -> dict[str, int]:
        """Top every pool back up to its configured target."""
        created: dict[str, int] = {}
        for status in self.pool_status():
            if scenario_ids and status.scenario_id not in scenario_ids:
                continue
            if status.deficit and status.template_ready:
                made = self.prewarm(status.scenario_id, status.deficit, status.workload)
                if made:
                    created[status.label] = made
        return created

    def _clone_from_template(self, scenario, target_name: str, workload: str = "") -> str:
        incus = self._require_incus()
        template = self.settings.incus.template_name(scenario.id, workload)
        if not incus.exists(template) or not incus.has_snapshot(template, POOL_SNAPSHOT):
            label = f"{scenario.id}@{workload}" if workload else scenario.id
            raise SessionError(
                f"template {template} is missing snapshot {POOL_SNAPSHOT}; run "
                f"`ontrak template build {label}`"
            )
        incus.copy_instance(f"{template}/{POOL_SNAPSHOT}", target_name, instance_only=True)
        incus.start_instance(target_name)
        return target_name

    def _await_guest(self, session: Session, timeout: int, driver: BaseDriver | None = None) -> str:
        """Wait for an address, then for the guest transport to answer."""
        driver = driver or self.driver
        incus = self._require_incus()
        deadline = time.time() + timeout
        ip = ""
        while time.time() < deadline:
            ip = incus.instance_ip(session.instance) or ""
            if ip:
                break
            time.sleep(3)
        if not ip:
            raise SessionError(f"{session.instance} never obtained an address on {self.settings.incus.network}")
        session.host_ip = ip
        remaining = max(30, int(deadline - time.time()))
        if not driver.wait_ready(session, timeout=remaining):
            raise SessionError(
                f"{session.instance} at {ip} never became reachable over the "
                f"{driver.name} transport"
            )
        return ip

    # ------------------------------------------------------------------
    # scenario files
    # ------------------------------------------------------------------
    def _upload_scenario_files(
        self, session: Session, scenario, include_setup: bool, driver: BaseDriver | None = None
    ) -> None:
        """Copy the shared lib, the scenario scripts and any resources into the guest.

        The layout is identical on both platforms — ``lib/`` next to ``scenarios/<id>/``
        — so a script can find its library with the same relative path whether it is
        PowerShell or shell.
        """
        driver = driver or self._driver_for(scenario)
        args = self._guest_args(session)
        linux = getattr(scenario, "platform", WINDOWS) == LINUX
        # Everything in scenarios/_lib lands in the guest's lib/ directory: the two
        # platform libraries always, plus anything a scenario family needs there —
        # the simulated directory service the identity scenarios run, for instance.
        # Uploading the whole directory (rather than naming files here) is what lets a
        # new shared tool ship without touching the session manager.
        for source in sorted(self.settings.scenarios_dir.joinpath("_lib").glob("*")):
            if not source.is_file():
                continue
            driver.upload_file(source, self._join(scenario, "lib", source.name), **args)
        if include_setup:
            driver.upload_file(
                scenario.setup_script,
                self._join(scenario, "scenarios", scenario.id, SETUP_NAMES[scenario.platform]),
                **args,
            )
        driver.upload_file(
            scenario.check_script,
            self._join(scenario, "scenarios", scenario.id, CHECK_NAMES[scenario.platform]),
            **args,
        )
        for resource in scenario.resources:
            source = scenario.directory / resource
            relative = resource if linux else resource.replace("/", "\\")
            destination = self._join(scenario, "scenarios", scenario.id, "resources", relative)
            driver.upload_file(source, destination, **args)

    # ------------------------------------------------------------------
    # allocation
    # ------------------------------------------------------------------
    def create_session(
        self,
        student: str,
        scenario_id: str,
        *,
        workload: str | None = None,
        time_limit_minutes: int | None = None,
    ) -> Session:
        """Create the session row and return immediately.

        Split from :meth:`provision` so the portal can answer a browser instantly
        and do the slow part (clone + boot + WinRM handshake) in a worker thread
        while the page polls for progress. Idempotent per (student, scenario):
        reloading a page or re-running the CLI will not burn a second VM.

        ``time_limit_minutes`` is the student's clock for this session; it defaults
        to ``session.ttl_minutes`` and can be changed later (instructor action), which
        is why it is stored on the row rather than derived from config on read.
        """
        student = student.strip().lower()
        self.repo.get(scenario_id)  # raises ScenarioError if unknown
        if workload and self.catalog is not None:
            # A typo'd platform should read like every other user error, not leak a
            # catalog exception out of the session API.
            try:
                self.catalog.get(workload)
            except CatalogError as exc:
                raise SessionError(f"unknown workload {workload!r}; see `ontrak catalog list`") from exc
        limit = int(time_limit_minutes or self.settings.session.default_time_limit)
        workload = self._resolve_workload(scenario_id, workload)
        if limit <= 0:
            raise SessionError("the time limit must be a positive number of minutes")
        live = self.store.live_sessions_for(student)

        for existing in live:
            if existing.scenario_id == scenario_id and existing.state != SessionState.ERROR:
                self.touch(existing)
                return existing
        if len(live) >= self.settings.session.max_per_student:
            busy = ", ".join(sorted({s.scenario_id for s in live}))
            raise SessionError(
                f"{student} already has a live session ({busy}); reset or end it first "
                "(session.max_per_student)"
            )

        session = Session(
            id=None,
            student=student,
            scenario_id=scenario_id,
            state=SessionState.REQUESTED,
            rdp_user=self.settings.guest.user,
            rdp_password=self.settings.guest.password,
            expires_at=iso(utcnow() + timedelta(minutes=limit)),
            workload=str(workload or ""),
            time_limit_minutes=limit,
        )
        self.store.create_session(session)
        self.store.log_event(
            "requested",
            f"{student} requested {scenario_id}"
            + (f" on workload {workload}" if workload else "")
            + f" with a {limit} minute limit",
            session.id,
        )
        return session

    def _resolve_workload(self, scenario_id: str, workload: str | None) -> str:
        """Work out which platform a session should be built on.

        An explicit choice wins, but only if the scenario actually offers it; otherwise
        the scenario's first declared workload is used, and a scenario that declares
        none falls back to the site's golden image.
        """
        scenario = self.repo.get(scenario_id)
        offered = scenario.platform_workloads
        wanted = str(workload or "").strip()
        if wanted:
            if offered and wanted not in offered:
                raise SessionError(
                    f"scenario {scenario_id} is not offered on workload {wanted!r}; "
                    f"it declares: {', '.join(offered)}"
                )
            if not offered and self.catalog is not None:
                # A scenario without declared workloads can still be asked for on a
                # platform, as long as that platform can host its family of fault.
                entry = self.workload_entry(wanted)
                if entry is None:
                    raise SessionError(f"unknown workload {wanted!r}; see `ontrak catalog list`")
                families = entry.scenario_families
                if families and scenario.category not in families:
                    raise SessionError(
                        f"workload {wanted} does not support {scenario.category} scenarios"
                    )
            return wanted
        return offered[0] if offered else ""

    def set_time_limit(self, session: Session, minutes: int) -> Session:
        """Set or change a student's time limit mid-session."""
        minutes = int(minutes)
        if minutes <= 0:
            raise SessionError("the time limit must be a positive number of minutes")
        session.set_time_limit(minutes)
        self.store.save_session(session)
        self.store.log_event("time_limit_set", f"{minutes} min", session.id)
        return session

    def extend_limit(self, session: Session, minutes: int) -> Session:
        """Grant extra time as a delta ("+15 minutes")."""
        return self.extend(session, int(minutes))

    def _lock_for(self, session: Session) -> threading.Lock:
        key = session.id or 0
        with self._lock_guard:
            return self._session_locks.setdefault(key, threading.Lock())

    def provision(self, session: Session) -> Session:
        """Bring a requested session up: claim a pooled VM or clone the template.

        Blocking, and serialised per session. The state is re-read from the store
        *inside* the lock, so a second caller (a page reload, an instructor, the
        CLI) waits for the first to finish and then returns the same session
        instead of cloning a second VM.
        """
        with self._lock_for(session):
            fresh = self.store.get_session(session.id) if session.id else None
            target = fresh or session
            if target.state not in {SessionState.REQUESTED, SessionState.ALLOCATING}:
                return target
            return self._provision_locked(target)

    def _provision_locked(self, session: Session) -> Session:
        scenario = self.repo.get(session.scenario_id)
        session.state = SessionState.ALLOCATING
        self.store.save_session(session)

        driver = self._driver_for(scenario)
        try:
            if not self._claim_pool(session):
                name = self.settings.incus.session_name(scenario.id, session.id or "x", session.workload)
                self._clone_from_template(scenario, name, session.workload)
                session.instance = name
                self.store.save_session(session)
                self.store.log_event("cloned", name, session.id)
            self._await_guest(session, self._ready_timeout(scenario), driver=driver)
            if self.settings.session.randomize_credentials and not scenario.is_linux:
                self._rotate_credentials(session)
            session.state = SessionState.READY
            session.ready_at = iso()
            session.last_activity_at = iso()
            self.store.save_session(session)
            self.store.log_event("ready", f"{session.instance} at {session.host_ip}", session.id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the student verbatim
            session.state = SessionState.ERROR
            session.error = str(exc)
            self.store.save_session(session)
            self.store.log_event("provision_failed", str(exc), session.id)
        return session

    def allocate(
        self,
        student: str,
        scenario_id: str,
        *,
        workload: str | None = None,
        time_limit_minutes: int | None = None,
    ) -> Session:
        """Create and provision in one call. Returns a READY or ERROR session."""
        return self.provision(
            self.create_session(
                student,
                scenario_id,
                workload=workload,
                time_limit_minutes=time_limit_minutes,
            )
        )

    def _claim_pool(self, session: Session) -> str | None:
        # A pool is per (scenario, workload): the same fault on Windows 11 and on
        # Ubuntu are different machines, so a Windows pool must never hand a student
        # a machine for a Linux scenario.
        available = self._available_pool(session.scenario_id, workload=session.workload)
        if not available:
            return None
        # Lowest name first: it has been idle longest and its page cache is cold.
        chosen = min(available, key=lambda instance: instance.name)
        session.instance = chosen.name
        session.host_ip = chosen.ipv4
        session.state = SessionState.PROVISIONING
        self.store.save_session(session)
        self.store.log_event("claimed_pool", chosen.name, session.id)
        return chosen.name

    def _rotate_credentials(self, session: Session) -> None:
        alphabet = string.ascii_letters + string.digits + "!@#%*+=?"
        password = "".join(secrets.choice(alphabet) for _ in range(20))
        script = (
            f"$p = ConvertTo-SecureString {quote_ps(password)} -AsPlainText -Force;"
            f"Set-LocalUser -Name {quote_ps(self.settings.guest.user)} -Password $p;"
            "'rotated'"
        )
        # Windows-only: a Linux guest is reached through the Incus agent or a key, so
        # there is no password to rotate for the student's login.
        result = self.driver.run_powershell(script, timeout=60, **self._guest_args(session))
        if result.ok:
            session.rdp_password = password
        else:
            self.store.log_event(
                "credential_rotation_failed",
                (result.stderr or result.stdout)[-400:],
                session.id,
            )

    # ------------------------------------------------------------------
    # using a session
    # ------------------------------------------------------------------
    def claim_for_use(self, session: Session) -> Session:
        """Mark a session in use (student opened the console)."""
        if session.state in {SessionState.READY, SessionState.PASSED}:
            session.state = SessionState.IN_USE
        self.touch(session)
        self.store.save_session(session)
        return session

    def touch(self, session: Session) -> None:
        session.last_activity_at = iso()
        if session.id:
            self.store.update_session(session.id, last_activity_at=session.last_activity_at)

    def extend(self, session: Session, minutes: int) -> Session:
        session.extend(minutes)
        self.store.save_session(session)
        self.store.log_event("extended", f"+{minutes} min", session.id)
        return session

    def reveal_hint(self, session: Session, scenario=None) -> Session:
        scenario = scenario or self.repo.get(session.scenario_id)
        if session.hint_level < len(scenario.hints):
            session.hint_level += 1
            self.store.save_session(session)
            self.store.log_event("hint", f"level {session.hint_level}", session.id)
        return session

    # ------------------------------------------------------------------
    # the in-house ticket
    # ------------------------------------------------------------------
    def ticket_form(self, scenario):
        """The write-up rubric for a scenario, or ``None`` when it has no ticket.

        A form with no fields is treated as "no ticket" rather than as a rubric the
        student can never satisfy.
        """
        form = getattr(scenario, "ticket_form", None)
        return form if form is not None and getattr(form, "fields", None) else None

    def ticket_form_for(self, session: Session):
        return self.ticket_form(self.repo.get(session.scenario_id))

    def save_ticket_draft(self, session: Session, values: dict) -> dict[str, str]:
        """Keep the student's work in progress so a page reload does not lose it."""
        stored = {str(k): str(v) for k, v in (values or {}).items()}
        if session.id:
            self.store.save_ticket_draft(session.id, stored)
        return stored

    def ticket_answers(self, session: Session) -> dict[str, str]:
        return self.store.ticket_draft(session.id or 0)

    def grade_ticket(self, session: Session, values: dict | None = None) -> TicketGrade | None:
        """Mark the write-up without recording it (the preview a student sees)."""
        scenario = self.repo.get(session.scenario_id)
        form = self.ticket_form(scenario)
        if form is None:
            return None
        answers = values if values is not None else self.ticket_answers(session)
        return mark_ticket(form, answers, session_id=session.id or 0, scenario_id=scenario.id)

    def run_checks(self, session: Session, record: bool | None = None) -> ScoreReport:
        """Grade the current VM state against the scenario.

        ``record`` decides whether this attempt is written to the results table.
        By default it follows ``session.persist_progress``: a student may check
        their work as often as they like, but only the grade they hand in at
        "Complete & End" is kept, so nothing is scored on progress.
        """
        if record is None:
            record = bool(self.settings.session.persist_progress)
        scenario = self.repo.get(session.scenario_id)
        if not session.instance:
            report = ScoreReport(
                session_id=session.id or 0,
                scenario_id=session.scenario_id,
                error="session has no VM",
            )
            return report

        session.state = SessionState.CHECKING
        self.store.save_session(session)
        driver = self._driver_for(scenario)
        try:
            self._upload_scenario_files(session, scenario, include_setup=False, driver=driver)
            result = driver.run_script_file(
                self._join(scenario, "scenarios", scenario.id, CHECK_NAMES[scenario.platform]),
                timeout=self.settings.session.check_timeout_seconds,
                **self._guest_args(session),
            )
            report = evaluate(scenario, session.id or 0, (result.stdout or "") + "\n" + (result.stderr or ""))
            if not result.ok and not report.outcomes:
                report.notes.append(f"check script exited {result.exit_code}")
        except (GuestError, IncusError, SessionError) as exc:
            report = ScoreReport(
                session_id=session.id or 0,
                scenario_id=session.scenario_id,
                error=f"could not run checks: {exc}",
            )

        session.checks_run += 1
        session.last_activity_at = iso()
        if not report.error:
            session.best_score = max(session.best_score, report.score)
            if report.resolved:
                session.resolved = True
        # The VM stays usable either way: a failed check is a coaching moment, not
        # a dead end. Resolution is sticky once earned.
        session.state = SessionState.PASSED if session.resolved else SessionState.IN_USE
        self.store.save_session(session)
        if session.id:
            if record:
                self.store.add_result(report, session.student)
            self.store.log_event(
                "checked" if record else "checked_discarded", report.summary_line(), session.id
            )
        session.last_report = report
        return report

    def complete(self, session: Session, values: dict | None = None) -> ScoreReport:
        """The student's "Complete & End": grade once, keep the result, destroy the VM.

        This is the only grading run whose outcome is stored. It marks two things and
        blends them:

        * the **machine**, from the scenario's ``check`` script, and
        * the **ticket**, from the write-up the student handed in.

        A scenario with no ticket form grades exactly as it always did. A scenario
        with one treats documentation as part of the work: an unsubmitted ticket
        scores zero and the attempt cannot be marked resolved, which is the honest
        reading of "the fix nobody recorded".

        After this the session is terminal, the instance is gone, and the student gets
        a fresh machine next time — which is also what makes a reset unnecessary to be
        perfectly clean.
        """
        if session.state.is_terminal:
            raise SessionError(
                f"session {session.id} is already {session.state.value}; there is nothing to complete"
            )
        scenario = self.repo.get(session.scenario_id)

        # Machine first, not recorded yet: the grade that gets stored is the blend, and
        # storing the machine half separately would put two rows in the results table
        # for one submission.
        report = self.run_checks(session, record=False)
        if values is not None:
            self.save_ticket_draft(session, values)

        form = self.ticket_form(scenario)
        ticket: TicketGrade | None = None
        # If the machine could not be graded at all, blending in a good write-up would
        # manufacture a passing score for an unverified machine. Mark the ticket, log
        # it, but leave the attempt at zero and say so.
        if form is not None and report.error:
            report.notes.append(
                "machine grading failed, so the write-up was marked but not blended into "
                "the score"
            )
            ticket = self.grade_ticket(session)
            report.ticket_score = ticket.score if ticket else 0.0
            report.ticket_weight = form.weight
            report.ticket_outcomes = [o.to_dict() for o in (ticket.outcomes if ticket else [])]
            session.state = SessionState.FAILED
            session.resolved = False
            session.notes = (session.notes + " [completed]").strip()
            self.store.save_session(session)
            self.store.add_result(report, session.student)
            if ticket is not None:
                self.store.save_ticket(ticket, session.student)
                self.store.clear_ticket_draft(session.id or 0)
                self.store.log_event("ticket_graded", ticket.summary_line(), session.id)
            self.store.log_event("completed", report.summary_line(), session.id)
            self._destroy_instance(session.instance)
            session.instance = ""
            session.host_ip = ""
            self.store.save_session(session)
            session.last_report = report
            return report
        if form is not None:
            ticket = self.grade_ticket(session)
            report.machine_score = report.machine_score or report.score
            report.ticket_score = ticket.score if ticket else 0.0
            report.ticket_weight = form.weight
            report.ticket_outcomes = [o.to_dict() for o in (ticket.outcomes if ticket else [])]
            report.score = blend(report.machine_score, ticket, form.weight)
            if ticket is None or not ticket.submitted:
                report.notes.append(
                    f"no ticket was submitted; the write-up is {form.weight:.0f}% of this "
                    "grade and counts as zero"
                )
                report.resolved = False
            elif ticket.score < form.pass_score:
                report.notes.append(
                    f"the write-up scored {ticket.score:.0f}% (pass mark {form.pass_score:.0f}%)"
                )
                report.resolved = False
            else:
                report.notes.append(
                    f"write-up: {ticket.score:.0f}% ({ticket.passed_count}/{len(ticket.outcomes)} fields)"
                )

        report.resolved = bool(report.resolved)
        report.notes.append(f"final submission judged against {scenario.title}")
        if report.has_ticket:
            report.notes.append(report.breakdown())
        session.state = SessionState.PASSED if report.resolved else SessionState.FAILED
        session.resolved = bool(report.resolved)
        session.best_score = max(session.best_score, report.score)
        session.notes = (session.notes + " [completed]").strip()
        self.store.save_session(session)

        self.store.add_result(report, session.student)
        if ticket is not None:
            self.store.save_ticket(ticket, session.student)
            self.store.clear_ticket_draft(session.id or 0)
            self.store.log_event("ticket_graded", ticket.summary_line(), session.id)
        self.store.log_event("completed", report.summary_line(), session.id)

        self._destroy_instance(session.instance)
        session.instance = ""
        session.host_ip = ""
        self.store.save_session(session)
        session.last_report = report
        return report

    def drain_pool(self, scenario_id: str, workload: str | None = None) -> int:
        """Delete unclaimed pooled VMs for a scenario (end of a class window).

        Only unclaimed instances go: a student still working keeps their machine.
        ``workload=None`` drains every platform this scenario is offered on, which is
        what a class-ending ``ontrak pool drain --scenario X`` means; passing a
        workload drains just that platform's pool.
        """
        if self.incus is None:
            return 0
        pairs = (
            [pair_workload for _scenario, pair_workload in self.workload_pairs([scenario_id])]
            if workload is None
            else [workload]
        )
        removed = 0
        for pair_workload in dict.fromkeys(pairs):
            for instance in self._available_pool(scenario_id, workload=pair_workload):
                self._destroy_instance(instance.name)
                removed += 1
        if removed:
            label = f"{scenario_id}@{workload}" if workload else scenario_id
            self.store.log_event("pool_drained", f"{label}: {removed} VM(s)")
        return removed

    # ------------------------------------------------------------------
    # reset / recycle
    # ------------------------------------------------------------------
    def reset(self, session: Session) -> Session:
        """Throw the VM away and hand back a fresh clone of the clean snapshot."""
        if session.state.is_terminal:
            raise SessionError(f"session {session.id} is {session.state.value} and cannot be reset")
        scenario = self.repo.get(session.scenario_id)
        session.state = SessionState.RECYCLING
        session.error = ""
        self.store.save_session(session)

        self._destroy_instance(session.instance)
        session.instance = ""
        session.host_ip = ""
        self.store.save_session(session)

        try:
            if not self._claim_pool(session):
                name = self.settings.incus.session_name(
                    session.scenario_id, session.id or "x", session.workload
                )
                self._clone_from_template(scenario, name, session.workload)
                session.instance = name
                self.store.save_session(session)
            self._await_guest(session, self._ready_timeout(scenario), driver=self._driver_for(scenario))
            if self.settings.session.randomize_credentials and not scenario.is_linux:
                self._rotate_credentials(session)
            session.state = SessionState.READY
            session.ready_at = iso()
            session.last_activity_at = iso()
            self.store.log_event("reset", f"{session.instance} at {session.host_ip}", session.id)
        except Exception as exc:  # noqa: BLE001
            session.state = SessionState.ERROR
            session.error = f"reset failed: {exc}"
            self.store.log_event("reset_failed", str(exc), session.id)
        self.store.save_session(session)
        return session

    def recycle(self, session: Session, reason: str = "expired") -> None:
        """Destroy the VM and close the session (student's time is up)."""
        session.state = SessionState.RECYCLING
        self.store.save_session(session)
        self._destroy_instance(session.instance)
        session.state = SessionState.DESTROYED
        session.host_ip = ""
        session.notes = (session.notes + f" [{reason}]").strip()
        session.last_activity_at = iso()
        self.store.save_session(session)
        self.store.log_event("recycled", reason, session.id)

    def end(self, session: Session, reason: str = "student quit") -> None:
        self.recycle(session, reason=reason)

    def _destroy_instance(self, name: str) -> None:
        if not name or self.incus is None:
            return
        try:
            if self.incus.exists(name):
                self.incus.stop_instance(name, force=True, timeout=60)
                self.incus.delete_instance(name, force=True)
        except IncusError as exc:
            self.store.log_event("destroy_failed", f"{name}: {exc}")

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def reap(self) -> dict:
        """Expire sessions and top the pool back up. Safe to run in a loop."""
        recycled: list[int] = []
        for session in self.store.list_sessions(
            states=[
                SessionState.READY,
                SessionState.IN_USE,
                SessionState.PASSED,
                SessionState.FAILED,
                SessionState.CHECKING,
            ]
        ):
            if session.is_expired:
                self.recycle(session, reason="ttl_expired")
                recycled.append(session.id or 0)
                continue
            idle_for = seconds_since(session.last_activity_at) or 0
            if idle_for > self.settings.session.idle_recycle_minutes * 60:
                # Never reap a grading run in flight.
                if session.state == SessionState.CHECKING:
                    continue
                self.recycle(session, reason=f"idle_{int(idle_for // 60)}m")
                recycled.append(session.id or 0)

        # Stale ERROR/ALLOCATING rows: surface them for the instructor but do not
        # keep half-built instances around forever.
        for session in self.store.list_sessions(states=[SessionState.ALLOCATING, SessionState.PROVISIONING]):
            if (seconds_since(session.created_at) or 0) > self.settings.pool.claim_timeout_seconds * 4:
                self._destroy_instance(session.instance)
                session.state = SessionState.ERROR
                session.error = session.error or "provisioning timed out"
                session.instance = ""
                self.store.save_session(session)

        refilled = {} if not self.settings.pool.enabled else self.refill_pool()
        return {"recycled": recycled, "refilled": refilled}

    def stats(self) -> dict:
        state_counts = {
            state.value: self.store.count_sessions(states=[state]) for state in SessionState
        }
        return {
            "sessions": state_counts,
            "students": len(self.store.list_users("student")),
            "pool": [
                {**status.__dict__, "deficit": status.deficit, "healthy": status.healthy}
                for status in self.pool_status()
            ],
            "templates": self.template_status(),
        }

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def get_owned_session(self, student: str, session_id: int, allow_instructor: bool = False) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            raise SessionError(f"session {session_id} not found")
        if not allow_instructor and session.student != student.strip().lower():
            raise SessionError(f"session {session_id} belongs to another student")
        return session

    def session_age_minutes(self, session: Session) -> float:
        started = parse_iso(session.ready_at) or parse_iso(session.created_at) or utcnow()
        return (utcnow() - started).total_seconds() / 60.0
