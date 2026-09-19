"""Demo mode: the whole student flow, with no hypervisor and no Windows.

Why this exists: every question about OnTrak ("what does a student actually see?",
"does grading work?", "can we show this to the training team on Thursday?") otherwise
requires a Linux host with KVM, Incus and a 40-minute Windows image build. Demo mode
replaces the hypervisor with an in-memory one and the guest with a driver that reports
plausible grading results, so the portal, the lifecycle, the scoring and the instructor
view can all be exercised in five seconds.

What it deliberately does **not** do: prove the Windows path works. That needs real
hardware, and `ontrak doctor` is the check for it.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from .config import Settings, load_settings
from .guest import BaseDriver, CommandResult
from .memory import InMemoryIncus
from .models import SessionState
from .scenarios import JSON_BEGIN, JSON_END, SETUP_OK_MARKER, ScenarioRepository
from .sessions import SessionManager
from .store import Store

DEMO_STUDENTS = ["student1", "student2", "student3", "student4", "student5", "student6"]
DEMO_INSTRUCTOR = "instructor"


@dataclass
class DemoEnvironment:
    settings: Settings
    store: Store
    catalog: Catalog
    repository: ScenarioRepository
    incus: InMemoryIncus
    driver: DemoDriver
    manager: SessionManager


class DemoDriver(BaseDriver):
    """A guest that answers plausibly instead of actually doing anything.

    Setup scripts report the success marker. Check scripts report every objective the
    scenario declares, passing with probability ``success_rate`` (deterministic for a
    given seed), so a demo can show either a clean pass or realistic partial credit.
    """

    name = "demo"

    def __init__(
        self,
        settings: Settings,
        repository: ScenarioRepository,
        *,
        success_rate: float = 1.0,
        seed: int = 0,
    ):
        super().__init__(settings)
        self.repository = repository
        self.success_rate = max(0.0, min(1.0, float(success_rate)))
        self.seed = seed
        self.calls: list[tuple[str, str]] = []

    # -- behaviour -----------------------------------------------------
    def _scenario_for(self, remote_path: str):
        for scenario in self.repository.list():
            if scenario.id in remote_path:
                return scenario
        return None

    def _report(self, remote_path: str) -> CommandResult:
        scenario = self._scenario_for(remote_path)
        if scenario is None:
            return CommandResult(True, 0, "")
        rng = random.Random(f"{self.seed}:{scenario.id}")
        checks = []
        for objective in scenario.objectives:
            passed = rng.random() < self.success_rate
            checks.append(
                {
                    "objective": objective.id,
                    "passed": passed,
                    "detail": (
                        f"demo mode simulated {'a correct fix' if passed else 'an incomplete fix'} "
                        f"({objective.text.lower()})"
                    ),
                }
            )
        payload = json.dumps({"checks": checks})
        return CommandResult(True, 0, f"{JSON_BEGIN}\n{payload}\n{JSON_END}")

    def run_shell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        """Shell transport for Linux scenarios, mirroring run_powershell.

        A demo that only spoke PowerShell could not exercise the Linux half of the
        catalogue — and the Linux half is where the CLI lessons live.
        """
        self.calls.append((instance, script[:200]))
        if "setup.sh" in script:
            return CommandResult(True, 0, f"demo setup applied\n{SETUP_OK_MARKER}")
        if "check.sh" in script:
            return self._report(script)
        return CommandResult(True, 0, "ok")

    def run_script_file(
        self, remote_path: str, host: str = "", instance: str = "", timeout: int = 300
    ) -> CommandResult:
        """Both platforms' scripts by filename, so the same demo drives PowerShell and shell."""
        self.calls.append((instance, f"run:{remote_path}"))
        if remote_path.endswith(("setup.ps1", "setup.sh")):
            return CommandResult(True, 0, f"demo setup applied\n{SETUP_OK_MARKER}")
        if remote_path.endswith(("check.ps1", "check.sh")):
            return self._report(remote_path)
        return CommandResult(True, 0, "ok")

    # -- BaseDriver surface --------------------------------------------
    def run_powershell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        self.calls.append((instance, script[:200]))
        if "setup.ps1" in script:
            return CommandResult(True, 0, f"demo setup applied\n{SETUP_OK_MARKER}")
        if "check.ps1" in script:
            return self._report(script)
        return CommandResult(True, 0, "ok")

    def wait_ready(self, session, timeout: int | None = None) -> bool:
        return True

    def _write_bytes(
        self,
        data: bytes,
        remote_path: str,
        host: str = "",
        instance: str = "",
        timeout: int = 120,
    ) -> CommandResult:
        self.calls.append((instance, f"upload:{remote_path}:{len(data)}B"))
        return CommandResult(True, 0, "demo upload")


def build_demo_environment(
    settings: Settings | None = None,
    *,
    state_dir: str | Path | None = None,
    success_rate: float | None = None,
    seed: int | None = None,
    students: int | None = None,
) -> DemoEnvironment:
    """Wire a manager that runs entirely in memory."""
    overrides: dict = {"demo": {"enabled": True}}
    if state_dir is not None:
        overrides["paths"] = {"state": str(state_dir)}
    settings = settings or load_settings(overrides=overrides)
    if state_dir is not None:
        settings.paths.state = str(state_dir)
    settings.demo.enabled = True
    if success_rate is not None:
        settings.demo.success_rate = float(success_rate)
    if students is not None:
        settings.demo.students = int(students)
    settings.ensure_dirs()

    if settings.demo.reset_state:
        db = settings.db_path
        if db.exists():
            db.unlink()

    store = Store(settings.db_path)
    catalog = Catalog(settings.catalog_dir)
    repository = ScenarioRepository(settings.scenarios_dir)
    incus = InMemoryIncus(image_alias=settings.incus.image_alias, image_present=True)
    # Pretend the workload images a scenario names are published on this host.
    # Demo mode's whole job is to stand in for the infrastructure, and an image
    # alias that exists is exactly what a real host would have after `ontrak image
    # build` or a plain `incus image copy` — without it, every catalogue-backed
    # scenario would fail on a host check the demo has no way to satisfy.
    catalog.load()
    for entry in catalog.entries.values():
        if entry.media.kind == "image" or entry.recipe in {"image-alias", "container-image"}:
            incus.add_image(entry.image_alias)
    # add_image() also moves the "current" alias, which would leave the site's
    # golden image unpublished; put it back so scenarios with no declared platform
    # still find theirs.
    incus.add_image(settings.incus.image_alias)
    incus.image_alias = settings.incus.image_alias
    driver = DemoDriver(
        settings,
        repository,
        success_rate=settings.demo.success_rate,
        seed=settings.selection.seed if seed is None else seed,
    )
    manager = SessionManager(
        settings,
        store,
        repo=repository,
        incus=incus,  # type: ignore[arg-type]
        driver=driver,
        # The same simulated guest answers shell as well as PowerShell, so a Linux
        # scenario (setup.sh/check.sh) runs through the real manager, scoring and
        # ticket paths in demo mode too.
        shell_driver=driver,
        catalog=catalog,
    )
    return DemoEnvironment(
        settings=settings,
        store=store,
        catalog=catalog,
        repository=repository,
        incus=incus,
        driver=driver,
        manager=manager,
    )


def account_names(settings: Settings) -> list[str]:
    """The demo accounts a sign-in may choose from: the students, then the instructor.

    Shared by `seed_accounts` (which creates them) and the portal's demo door
    (which lets you in as one), so the two can never disagree about who exists.
    """
    count = max(1, min(int(settings.demo.students), len(DEMO_STUDENTS)))
    return [*DEMO_STUDENTS[:count], DEMO_INSTRUCTOR]


def seed_accounts(env: DemoEnvironment, *, students: int | None = None) -> list[str]:
    """Create the demo roster. Idempotent: re-running just refreshes the rows.

    There is no password to set — sign-in is Authentik's everywhere, and a demo
    has no IdP to sign in against, so the portal's demo door (see app.py) picks
    one of these accounts by name instead.
    """
    count = int(students if students is not None else env.settings.demo.students)
    names = DEMO_STUDENTS[: max(1, min(count, len(DEMO_STUDENTS)))]
    for name in names:
        env.store.upsert_user(name, role="student", display_name=name.title())
    env.store.upsert_user(DEMO_INSTRUCTOR, role="instructor", display_name="Instructor")
    return names


def synthesise_ticket(form) -> dict[str, str]:
    """Write a plausible incident write-up that satisfies a ticket rubric.

    Demo mode pretends a student did the work; pretending they also *documented* it is
    what makes the blended grade visible — and it means a rubric that asks for terms
    nobody can guess shows up as unreachable in a demo run rather than in a class.
    """
    values: dict[str, str] = {}
    for field in form.fields:
        if field.is_choice():
            values[field.id] = field.expected or (field.options[0] if field.options else "")
            continue
        if (field.kind or "").lower() == "number":
            values[field.id] = "1"
            continue
        terms = [*field.all_of, *(field.any_of[:1] if field.any_of else [])]
        text = ("Answered: " + ", ".join(terms) + ".") if terms else "Recorded the incident."
        # Pad to the minimum length with a sentence that is true of any repair, and
        # never with a phrase the rubric rejects.
        filler = " Verified on the machine before handing the session in."
        while len(text.split()) < max(field.min_words, 1) + 1 and len(text) < 1200:
            text = f"{text}{filler}" if not text.endswith(filler.strip()) else f"{text} Confirmed."
        values[field.id] = text
    return values


def seed_pool(
    env: DemoEnvironment,
    scenario_ids: list[str],
    *,
    per_scenario: int = 2,
    prewarm_ids: list[str] | None = None,
) -> dict[str, int]:
    """Build a template for every (scenario, platform) pair, prewarming only some.

    Templates are per pair because the same fault on Windows 11 and Ubuntu are
    different machines (see the workload matrix in docs/architecture.md). Every pair
    is built because automatic assignment can hand a student any scenario; only the
    nominated scenarios get warm VMs, which is what a real lab does to keep host
    memory under control.

    Returns ``{"<scenario>[@<platform>]": warm_count}``.
    """
    warm = set(prewarm_ids) if prewarm_ids is not None else set(scenario_ids)
    built: dict[str, int] = {}
    for scenario, workload in env.manager.workload_pairs(scenario_ids):
        key = f"{scenario.id}@{workload}" if workload else scenario.id
        env.manager.ensure_template(scenario.id, workload=workload)
        built[key] = (
            env.manager.prewarm(scenario.id, per_scenario, workload=workload)
            if scenario.id in warm
            else 0
        )
    return built


def run_demo(
    *,
    scenario_ids: list[str] | None = None,
    students: int | None = None,
    success_rate: float | None = None,
    state_dir: str | Path | None = None,
    complete_sessions: bool = True,
    write_ups: bool = True,
    verbose: bool = True,
) -> dict:
    """Drive a complete class: assign, provision, grade, hand in, tear down.

    Returns a summary dict (so callers — the CLI, the tests, a notebook — do not have
    to scrape stdout).
    """
    env = build_demo_environment(success_rate=success_rate, students=students, state_dir=state_dir)
    repository = env.repository
    chosen = scenario_ids or [s.id for s in repository.list()[:3]]
    if not chosen:
        raise RuntimeError("no scenarios found; nothing to demonstrate")

    names = seed_accounts(env, students=students)

    from . import selection

    summary: dict = {
        "students": [],
        "scenarios": chosen,
        "pool": seed_pool(
            env,
            [s.id for s in repository.list()],
            per_scenario=2,
            prewarm_ids=chosen,
        ),
        "graded": [],
        "completed": [],
    }

    # An explicit scenario list means "use these"; automatic assignment is only for
    # when the caller does not care which ticket a student gets.
    auto_assign = env.settings.selection.auto_assign and not scenario_ids
    history: list[str] = []
    for index, student in enumerate(names):
        if auto_assign:
            choice = selection.choose(
                repository.list(),
                history=history,
                strategy=env.settings.selection.strategy,
                max_difficulty=env.settings.selection.max_difficulty,
                seed=env.settings.selection.seed + index,
            )
            scenario_id = choice.scenario.id
            reason = choice.explain()
        else:
            scenario_id = chosen[index % len(chosen)]
            reason = "rotated from the requested list"
        history.append(scenario_id)

        session = env.manager.allocate(
            student,
            scenario_id,
            time_limit_minutes=env.settings.session.default_time_limit,
        )
        summary["students"].append(
            {
                "student": student,
                "scenario_id": scenario_id,
                "reason": reason,
                "instance": session.instance,
                "state": session.state.value,
                "error": session.error,
                "minutes": session.time_limit_minutes,
            }
        )
        if session.state == SessionState.ERROR:
            continue

        # A student checks their work (not recorded), writes up the ticket, then hands
        # it in (recorded). The write-up is synthesised from the rubric, so the demo
        # exercises the ticket grading and the blended score rather than leaving the
        # ticket component at zero.
        preview = env.manager.run_checks(session)
        if complete_sessions:
            ticket_values = None
            ticket_form = env.manager.ticket_form_for(session)
            if ticket_form is not None and write_ups:
                ticket_values = synthesise_ticket(ticket_form)
                env.manager.save_ticket_draft(session, ticket_values)
            final = env.manager.complete(session, values=ticket_values)
            summary["completed"].append(
                {
                    "student": student,
                    "scenario_id": scenario_id,
                    "score": final.score,
                    "machine_score": final.machine_score,
                    "ticket_score": final.ticket_score,
                    "resolved": final.resolved,
                    "state": session.state.value,
                }
            )
        summary["graded"].append(
            {
                "student": student,
                "scenario_id": scenario_id,
                "preview_score": preview.score,
                "preview_resolved": preview.resolved,
            }
        )

    summary["stats"] = env.manager.stats()
    summary["results"] = env.store.leaderboard()
    if verbose:
        print(render_demo_summary(summary))
    return summary


def render_demo_summary(summary: dict) -> str:
    lines = ["", "OnTrak demo run", "=" * 60]
    lines.append(f"scenarios: {', '.join(summary['scenarios'])}")
    pool = {k: v for k, v in (summary.get("pool") or {}).items() if v}
    if pool:
        lines.append("warm pool: " + ", ".join(f"{k} x{v}" for k, v in pool.items()))
    lines.append("")
    lines.append(f"{'student':<10} {'scenario':<24} {'instance':<28} state")
    for row in summary["students"]:
        lines.append(
            f"{row['student']:<10} {row['scenario_id']:<24} "
            f"{row['instance'] or '-':<28} {row['state']}"
            + (f"  ERROR: {row['error']}" if row["error"] else "")
        )
    if summary["completed"]:
        lines += ["", f"{'student':<10} {'scenario':<24} {'score':>6}  outcome"]
        for row in summary["completed"]:
            outcome = "passed" if row["resolved"] else "not resolved"
            lines.append(
                f"{row['student']:<10} {row['scenario_id']:<24} {row['score']:>5.0f}%  {outcome}"
            )
    lines += ["", "Only the submitted grade is stored; the preview check was discarded.", ""]
    return "\n".join(lines)
