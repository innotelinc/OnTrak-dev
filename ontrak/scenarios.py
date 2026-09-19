"""Scenario catalogue: loading, validation and selection.

A scenario is a directory::

    scenarios/net-dns-failure/
        scenario.yaml    ticket text, objectives + weights, hints, metadata
        setup.ps1        injects the fault (must end with ONTRAK-SETUP-OK)
        check.ps1        grades objectives (must call Write-OnTrakReport)
        resources/       optional extra files uploaded with setup.ps1

Objectives declared in ``scenario.yaml`` are the contract: ``check.ps1`` must
report on exactly those ids, and :meth:`ScenarioRepository.validate` enforces
that statically so a typo cannot silently score zero.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import CATEGORY_LABELS, Category, Objective
from .tickets import TicketError, load_form, validate_form

# Markers the guest-side helper writes around its JSON payload. Using markers
# means PowerShell banners, progress streams and stray warnings cannot corrupt
# the result: we parse only what sits between them.
JSON_BEGIN = "###ONTRAK-JSON-BEGIN###"
JSON_END = "###ONTRAK-JSON-END###"

SETUP_OK_MARKER = "ONTRAK-SETUP-OK"
SETUP_OK_HELPER = "Write-OnTrakSetupOk"
CHECK_ENTRYPOINT = "Write-OnTrakReport"
COMMON_LIB = "OnTrak.Common.ps1"

# A scenario targets one platform. Windows guests are driven over WinRM with
# PowerShell; Linux guests are driven over the Incus agent or SSH with shell. The
# script contract is the same on both — inject the fault, then report JSON between
# two markers — which is what lets the grader, the portal and the scoring maths stay
# platform-independent.
WINDOWS = "windows"
LINUX = "linux"
PLATFORMS = (WINDOWS, LINUX)
SETUP_NAMES = {WINDOWS: "setup.ps1", LINUX: "setup.sh"}
CHECK_NAMES = {WINDOWS: "check.ps1", LINUX: "check.sh"}

# Shell equivalents of the PowerShell helpers (scenarios/_lib/ontrak-common.sh).
SHELL_SETUP_OK_HELPER = "ontrak_setup_ok"
SHELL_CHECK_HELPER = "ontrak_check"
SHELL_CHECK_ENTRYPOINT = "ontrak_report"
SHELL_COMMON_LIB = "ontrak-common.sh"

DEFAULT_PASS_SCORE = 80.0
MAX_DIFFICULTY = 4

_CATEGORY_ALIASES: dict[str, str] = {
    "hardware": Category.HARDWARE.value,
    "hw": Category.HARDWARE.value,
    "driver": Category.HARDWARE.value,
    "drivers": Category.HARDWARE.value,
    "software": Category.SOFTWARE.value,
    "apps": Category.SOFTWARE.value,
    "app": Category.SOFTWARE.value,
    "network": Category.NETWORK.value,
    "net": Category.NETWORK.value,
    "connectivity": Category.NETWORK.value,
    "os": Category.OS.value,
    "boot": Category.OS.value,
    "performance": Category.OS.value,
    "perf": Category.OS.value,
    "security": Category.SECURITY.value,
    "malware": Category.SECURITY.value,
    "identity": Category.IDENTITY.value,
    "identities": Category.IDENTITY.value,
    "access": Category.IDENTITY.value,
    "accounts": Category.IDENTITY.value,
    "directory": Category.IDENTITY.value,
    "ad": Category.IDENTITY.value,
    "ldap": Category.IDENTITY.value,
}


class ScenarioError(RuntimeError):
    """Raised when a scenario is missing or invalid."""


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    briefing: str
    objectives: list[Objective]
    directory: Path
    difficulty: int = 2
    minutes: int = 25
    pass_score: float = DEFAULT_PASS_SCORE
    hints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    requires_internet: bool = False
    ticket: dict = field(default_factory=dict)
    reset_notes: str = ""
    resources: list[str] = field(default_factory=list)
    instance_devices: list[dict] = field(default_factory=list)
    instance_config: dict = field(default_factory=dict)
    setup_script: str = ""
    check_script: str = ""
    # Optional catalog entry id ("win11-24h2", "ubuntu-24.04") naming the platform
    # this fault should be built on. Empty means the site's golden image.
    workload: str = ""
    # The same fault usually belongs on more than one platform. Each workload gets its
    # own template and pool, so a scenario can be offered on Windows 11 *and* Ubuntu
    # without being duplicated.
    workloads: list[str] = field(default_factory=list)
    platform: str = WINDOWS
    generated_from: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    # The incident write-up the student has to hand in (see ontrak/tickets.py).
    # ``None`` means this scenario is graded on machine state alone, which is what
    # every scenario written before the ticket system does.
    ticket_form: object | None = None

    @property
    def platform_workloads(self) -> list[str]:
        """Every catalog workload this scenario can be built for (may be empty)."""
        ids = [w for w in (self.workloads or ([self.workload] if self.workload else [])) if w]
        return list(dict.fromkeys(ids))

    @property
    def is_linux(self) -> bool:
        return self.platform == LINUX

    # -- introspection -------------------------------------------------
    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category.title())

    @property
    def total_weight(self) -> float:
        return sum(o.weight for o in self.objectives)

    @property
    def critical_objectives(self) -> list[Objective]:
        return [o for o in self.objectives if o.critical]

    def objective(self, objective_id: str) -> Objective | None:
        return next((o for o in self.objectives if o.id == objective_id), None)

    def hints_up_to(self, level: int) -> list[str]:
        return self.hints[: max(0, min(level, len(self.hints)))]

    @property
    def ticket_header(self) -> dict[str, str]:
        """The ticket's own header — who reported it, on what, how urgent.

        ``ticket:`` also carries ``form:``, the field list with its weights, hints
        and the terms a competent answer has to contain. The session page used to
        render the whole block as a key/value table, which printed that rubric
        beside the student — the answers, in the page they were meant to answer
        from. Only scalars survive here and ``form`` is excluded by name, so a
        new key in the block cannot leak by default: an entry has to be a plain
        label/value to reach the page at all.
        """
        header: dict[str, str] = {}
        for key, value in (self.ticket or {}).items():
            if key == "form" or not isinstance(value, (str, int, float)):
                continue
            text = str(value).strip()
            if text:
                header[key.replace("_", " ").title()] = text
        return header

    def public(self, hint_level: int = 0) -> dict:
        """Portal-facing view. Never includes script bodies or unrevealed hints."""
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "category_label": self.category_label,
            "difficulty": self.difficulty,
            "minutes": self.minutes,
            "briefing": self.briefing,
            # The header, never the raw block: ``ticket`` holds the marking rubric
            # (see ticket_header) and a view that leaked it would leak the answers.
            "ticket": self.ticket_header,
            "tags": self.tags,
            "pass_score": self.pass_score,
            "requires_internet": self.requires_internet,
            "reset_notes": self.reset_notes,
            "workload": self.workload,
            "workloads": self.platform_workloads,
            "platform": self.platform,
            "lessons": list(self.lessons),
            "has_ticket": self.ticket_form is not None,
            "hint_count": len(self.hints),
            "hints_revealed": self.hints_up_to(hint_level),
            "objectives": [
                {
                    "id": o.id,
                    "text": o.text,
                    "weight": o.weight,
                    "critical": o.critical,
                }
                for o in self.objectives
            ],
        }


class ScenarioRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._scenarios: dict[str, Scenario] | None = None

    # -- loading -------------------------------------------------------
    def load(self, force: bool = False) -> dict[str, Scenario]:
        if self._scenarios is None or force:
            self._scenarios = self._discover()
        return self._scenarios

    def reload(self) -> dict[str, Scenario]:
        return self.load(force=True)

    def _discover(self) -> dict[str, Scenario]:
        found: dict[str, Scenario] = {}
        if not self.root.exists():
            raise ScenarioError(f"scenario directory not found: {self.root}")
        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            manifest = directory / "scenario.yaml"
            if not manifest.exists():
                continue
            scenario = self._load_one(directory, manifest)
            found[scenario.id] = scenario
        return found

    def _load_one(self, directory: Path, manifest: Path) -> Scenario:
        try:
            data = yaml.safe_load(manifest.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ScenarioError(f"{manifest}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ScenarioError(f"{manifest}: top level must be a mapping")

        scenario_id = str(data.get("id") or directory.name)
        platform = normalise_platform(str(data.get("platform") or WINDOWS))
        objectives = [
            Objective.from_dict(item) for item in (data.get("objectives") or []) if isinstance(item, dict)
        ]
        ticket_data = data.get("ticket") if isinstance(data.get("ticket"), dict) else {}
        try:
            form = load_form(ticket_data)
        except TicketError as exc:
            raise ScenarioError(f"{manifest}: {exc}") from exc
        return Scenario(
            id=scenario_id,
            title=str(data.get("title") or scenario_id),
            category=normalise_category(str(data.get("category") or Category.SOFTWARE.value)),
            briefing=str(data.get("briefing") or "").strip(),
            objectives=objectives,
            directory=directory,
            difficulty=int(data.get("difficulty", 2)),
            minutes=int(data.get("minutes", 25)),
            pass_score=float(data.get("pass_score", DEFAULT_PASS_SCORE)),
            hints=[str(h) for h in (data.get("hints") or [])],
            tags=[str(t) for t in (data.get("tags") or [])],
            requires_internet=bool(data.get("requires_internet", False)),
            ticket=dict(data.get("ticket") or {}),
            reset_notes=str(data.get("reset_notes") or "").strip(),
            resources=[str(r) for r in (data.get("resources") or [])],
            instance_devices=[d for d in (data.get("instance_devices") or []) if isinstance(d, dict)],
            instance_config=dict(data.get("instance_config") or {}),
            platform=normalise_platform(str(data.get("platform") or WINDOWS)),
            setup_script=str(directory / SETUP_NAMES[platform]),
            check_script=str(directory / CHECK_NAMES[platform]),
            workload=str(data.get("workload") or ""),
            workloads=[str(w) for w in (data.get("workloads") or [])],
            generated_from=[str(g) for g in (data.get("generated_from") or [])],
            lessons=[str(item) for item in (data.get("lessons") or [])],
            ticket_form=form,
        )

    # -- access --------------------------------------------------------
    def get(self, scenario_id: str) -> Scenario:
        scenarios = self.load()
        if scenario_id not in scenarios:
            raise ScenarioError(
                f"unknown scenario {scenario_id!r}; available: {', '.join(sorted(scenarios)) or 'none'}"
            )
        return scenarios[scenario_id]

    def list(self) -> list[Scenario]:
        order = {c.value: i for i, c in enumerate(Category)}
        return sorted(
            self.load().values(),
            key=lambda s: (order.get(s.category, 99), s.difficulty, s.id),
        )

    def by_category(self) -> dict[str, list[Scenario]]:
        grouped: dict[str, list[Scenario]] = {}
        for scenario in self.list():
            grouped.setdefault(scenario.category, []).append(scenario)
        return grouped

    def ids(self) -> list[str]:
        return [s.id for s in self.list()]

    # -- validation ----------------------------------------------------
    def _script_problems(self, scenario: Scenario, ids: list[str], prefix: str) -> list[str]:
        """Check the platform's setup/check contract.

        Both platforms owe us the same two things — a setup script that confirms the
        fault was applied, and a check script that reports every objective — so the
        failure modes are identical even though the languages are not.
        """
        problems: list[str] = []
        setup_name = SETUP_NAMES.get(scenario.platform, "setup.ps1")
        check_name = CHECK_NAMES.get(scenario.platform, "check.ps1")
        setup_path = Path(scenario.setup_script)
        check_path = Path(scenario.check_script)

        if scenario.platform == WINDOWS:
            setup_ok_tokens = (SETUP_OK_HELPER, SETUP_OK_MARKER)
            setup_ok_hint = f"{SETUP_OK_HELPER} or the literal {SETUP_OK_MARKER}"
            entrypoint = CHECK_ENTRYPOINT
        else:
            setup_ok_tokens = (SHELL_SETUP_OK_HELPER, SETUP_OK_MARKER)
            setup_ok_hint = f"{SHELL_SETUP_OK_HELPER} or the literal {SETUP_OK_MARKER}"
            entrypoint = SHELL_CHECK_ENTRYPOINT

        if not setup_path.exists() or not setup_path.stat().st_size:
            problems.append(f"{prefix} {setup_name} is missing or empty")
        else:
            setup_text = setup_path.read_text(errors="replace")
            if not any(token in setup_text for token in setup_ok_tokens):
                problems.append(
                    f"{prefix} {setup_name} never confirms success (needs {setup_ok_hint}); "
                    "template build would reject a partially applied fault"
                )

        if not check_path.exists() or not check_path.stat().st_size:
            problems.append(f"{prefix} {check_name} is missing or empty")
        else:
            check_text = check_path.read_text(errors="replace")
            if entrypoint not in check_text:
                problems.append(f"{prefix} {check_name} must call {entrypoint}")
            for objective_id in ids:
                if not re.search(rf"['\"]{re.escape(objective_id)}['\"]", check_text):
                    problems.append(
                        f"{prefix} {check_name} never reports objective {objective_id!r} "
                        "(it would always score as failed)"
                    )
            if scenario.platform == LINUX and SHELL_CHECK_HELPER not in check_text:
                problems.append(
                    f"{prefix} {check_name} must report through {SHELL_CHECK_HELPER} so the "
                    "objective ids and weights match the manifest"
                )
        return problems

    def validate(
        self, scenario_ids: list[str] | None = None, catalog=None, lessons=None
    ) -> list[str]:
        """Return a list of human-readable problems (empty means healthy).

        This is stricter than loading: it checks the objective/check-script
        contract, so ``make validate`` in CI catches the class of bug where a
        scenario scores 0% forever because a check id was renamed. ``lessons`` (a
        :class:`ontrak.lessons.LessonRepository`, passed in to avoid an import
        cycle) additionally verifies that every lesson a scenario points a student
        at actually exists — a dead link in a hint is a support call.
        """
        problems: list[str] = []
        # A caller that hands us a catalog object has not necessarily loaded it yet,
        # and an unloaded catalog looks exactly like an empty one: every declared
        # workload would be reported as missing. Loading here makes the check honest.
        if catalog is not None and hasattr(catalog, "load"):
            with contextlib.suppress(Exception):
                catalog.load()
        try:
            scenarios = self.load(force=True)
        except ScenarioError as exc:
            return [str(exc)]

        selected = scenarios.values() if not scenario_ids else [
            s for s in scenarios.values() if s.id in set(scenario_ids)
        ]
        for scenario in selected:
            prefix = f"[{scenario.id}]"
            expected_dir = scenario.directory.name
            if scenario.id != expected_dir:
                problems.append(f"{prefix} id does not match directory name {expected_dir!r}")
            if not scenario.title or scenario.title == scenario.id:
                problems.append(f"{prefix} missing a human-readable title")
            if not scenario.briefing:
                problems.append(f"{prefix} missing briefing text (the student's ticket)")
            if not 1 <= scenario.difficulty <= MAX_DIFFICULTY:
                problems.append(f"{prefix} difficulty must be 1..{MAX_DIFFICULTY}")
            if scenario.minutes <= 0:
                problems.append(f"{prefix} minutes must be positive")
            if not 0 < scenario.pass_score <= 100:
                problems.append(f"{prefix} pass_score must be in (0, 100]")
            if not scenario.objectives:
                problems.append(f"{prefix} declares no objectives")
            if len(scenario.critical_objectives) == len(scenario.objectives) and len(scenario.objectives) > 3:
                problems.append(
                    f"{prefix} every objective is critical; keep critical for the "
                    "must-not-miss items so partial credit stays meaningful"
                )
            ids = [o.id for o in scenario.objectives]
            duplicates = {i for i in ids if ids.count(i) > 1}
            if duplicates:
                problems.append(f"{prefix} duplicate objective id(s): {', '.join(sorted(duplicates))}")
            for objective in scenario.objectives:
                if objective.weight <= 0:
                    problems.append(f"{prefix} objective {objective.id} needs weight > 0")
                if not objective.text:
                    problems.append(f"{prefix} objective {objective.id} has no text")
            # Weights total 100 so that a pass mark means the same thing in every
            # scenario, and so a student who has done 80% of the work is told so.
            if scenario.objectives and abs(scenario.total_weight - 100) > 0.01:
                problems.append(
                    f"{prefix} objective weights total {scenario.total_weight:g}, not 100"
                )

            problems.extend(self._script_problems(scenario, ids, prefix))

            for workload_id in scenario.platform_workloads:
                if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", workload_id):
                    problems.append(
                        f"{prefix} workload {workload_id!r} is not a catalog entry id "
                        "(lowercase, dash-separated)"
                    )
                elif catalog is not None and workload_id not in catalog.entries:
                    problems.append(
                        f"{prefix} names workload {workload_id!r}, which is not in the catalog"
                    )

            # Teaching material: a scenario that hands a student a command-line fault
            # without a walkthrough to learn it from is an unfair ticket, so a Linux
            # scenario with hints must name at least one lesson.
            if scenario.platform == LINUX and scenario.hints and not scenario.lessons:
                problems.append(
                    f"{prefix} is a Linux scenario and offers hints but names no lessons; "
                    "link the command walkthroughs a student needs (see docs/lessons.md)"
                )
            for lesson in scenario.lessons:
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", lesson):
                    problems.append(f"{prefix} lesson id {lesson!r} must be lowercase and dash-separated")
                elif lessons is not None and lessons.find(lesson) is None:
                    problems.append(
                        f"{prefix} lesson {lesson!r} does not exist under lessons/ "
                        "(the student would follow a dead link)"
                    )
            problems.extend(validate_form(scenario.ticket_form, prefix))
            for resource in scenario.resources:
                if not (scenario.directory / resource).exists():
                    problems.append(f"{prefix} declared resource {resource!r} does not exist")
            for device in scenario.instance_devices:
                if not device.get("name") or not device.get("type"):
                    problems.append(
                        f"{prefix} each entry in instance_devices needs a 'name' and a 'type'"
                    )
                elif device.get("type") == "nic" and not device.get("network"):
                    problems.append(
                        f"{prefix} nic device {device['name']!r} needs a 'network' "
                        "(build would fail without one)"
                    )
        return problems


def normalise_platform(value: str) -> str:
    """Map the ways a manifest might name a platform onto the two we support."""
    key = str(value).strip().lower().replace(" ", "")
    aliases = {
        "windows": WINDOWS,
        "win": WINDOWS,
        "ps": WINDOWS,
        "powershell": WINDOWS,
        "linux": LINUX,
        "unix": LINUX,
        "shell": LINUX,
        "sh": LINUX,
    }
    if key in aliases:
        return aliases[key]
    raise ScenarioError(
        f"unknown platform {value!r}; use one of: {', '.join(PLATFORMS)}"
    )


def normalise_category(value: str) -> str:
    key = value.strip().lower().replace(" ", "_")
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    try:
        return Category(key).value
    except ValueError as exc:
        allowed = ", ".join(sorted(CATEGORY_LABELS))
        raise ScenarioError(f"unknown category {value!r}; use one of: {allowed}") from exc


def default_scenario_repository(settings) -> ScenarioRepository:
    return ScenarioRepository(settings.scenarios_dir)
