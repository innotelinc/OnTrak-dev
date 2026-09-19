"""The admin panel: everything the person running the *programme* needs.

The instructor page (``/instructor``) is for running a class: who is on a machine,
what is warm, build this template now. The admin panel is for the estate around it:
accounts, the scenario and platform catalogue, the tickets students wrote, the
schedule, the marking record and the audit trail.

They are separate pages because they are separate jobs, and because the admin panel
touches things that can break a class (deleting an account, rebuilding a template)
— it writes everything it does to the event log, so "who changed that" has an
answer.

Kept in its own module so ``app.py`` stays about the student experience. The
helpers it needs are passed in as an :class:`AdminContext` rather than imported
again, so CSRF checking, ownership and rendering each have exactly one definition.
"""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..models import SessionState
from ..scheduler import Scheduler
from ..tickets import feedback_text as ticket_feedback_text

ADMIN_SECTIONS = [
    ("overview", "Overview", "Estate at a glance, and the operations you can run from here"),
    ("users", "Accounts", "Students and instructors who can sign in"),
    ("scenarios", "Scenarios", "The catalogue, and whether each entry validates"),
    ("platforms", "Platforms", "Operating systems, images, templates and pools"),
    ("tickets", "Tickets", "The write-ups students handed in, and how they were marked"),
    ("sessions", "Sessions", "Every session, live and historical"),
    ("schedule", "Schedule", "Prewarm and teardown windows"),
    ("results", "Results", "Submitted grades and the marking record"),
    ("audit", "Audit", "Everything the control plane did, in order"),
]


@dataclass
class AdminContext:
    """Everything the admin routes need, injected by the app factory."""

    settings: Any
    store: Any
    repo: Any
    catalog: Any
    lessons: Any
    manager: Any
    render: Callable
    redirect: Callable
    require_instructor: Callable
    check_csrf: Callable
    csrf_token: Callable
    session_link: Callable
    workload_groups: Callable
    catalog_entry: Callable
    lesson_index: Callable


def register_admin_routes(app: FastAPI, ctx: AdminContext) -> None:
    settings = ctx.settings
    store = ctx.store
    require_instructor = ctx.require_instructor

    # Reporting reads shell out to the hypervisor, and the admin panel is exactly
    # where an operator looks when Incus *is* the problem. So every one of them is
    # wrapped: a page that 500s because it could not ask Incus how many machines
    # are warm tells the operator nothing, and the page that says so tells them
    # everything. Failures are collected and rendered as a banner.
    infra_failures: list[str] = []

    def safe(call, fallback, label: str):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - a report must not raise
            infra_failures.append(f"{label}: {exc}")
            return fallback

    def take_failures() -> list[str]:
        """Drain the collected failures, so a response reports its own reads
        rather than the accumulated history of the process."""
        failures = list(infra_failures)
        infra_failures.clear()
        return failures

    def page(request: Request, template: str, section: str, context: dict, status_code: int = 200):
        """Render an admin page with the section nav and the shared chrome."""
        # `pool_error` is the older single-message form, kept for the banner that
        # the overview page already renders.
        failures = take_failures()
        return ctx.render(
            request,
            template,
            {
                "sections": ADMIN_SECTIONS,
                "section": section,
                "infra_errors": failures,
                "pool_error": failures[0] if failures else "",
                "settings_summary": {
                    "ttl_minutes": settings.session.ttl_minutes,
                    "default_target": settings.pool.default_target,
                    "max_total": settings.pool.max_total,
                    "pool_enabled": settings.pool.enabled,
                    "persist_progress": settings.session.persist_progress,
                    "destroy_on_complete": settings.session.destroy_on_complete,
                },
                **context,
            },
            status_code=status_code,
        )

    def scenario_key(scenario_id: str, workload: str = "") -> str:
        return f"{scenario_id}@{workload}" if workload else scenario_id

    # ----------------------------------------------------------------- overview --
    @app.get("/admin", response_class=HTMLResponse)
    def admin_overview(request: Request, user=Depends(require_instructor)):  # noqa: B008
        manager = ctx.manager
        sessions = store.list_sessions(limit=500)
        by_state: dict[str, int] = {state.value: 0 for state in SessionState}
        for session in sessions:
            by_state[session.state.value] = by_state.get(session.state.value, 0) + 1
        pool = safe(manager.pool_status, [], "warm pool")
        templates = safe(manager.template_status, [], "templates")
        return page(
            request,
            "admin/overview.html",
            "overview",
            {
                "users": store.count_users(),
                "by_state": by_state,
                "live": sum(by_state.get(s, 0) for s in ("ready", "in_use", "checking")),
                "recent_sessions": sessions[:12],
                "templates": templates,
                "templates_ready": sum(1 for t in templates if t["ready"]),
                "pool": pool,
                "pool_ready": sum(p.ready for p in pool),
                "tickets": store.count_tickets(),
                "results": len(store.list_results(limit=1000)),
                "scenarios": len(ctx.repo.list()),
                "lessons": len(ctx.lessons.list()),
                "events": store.count_events(),
                "events_recent": store.recent_events(limit=15),
                "problems": ctx.repo.validate(catalog=ctx.catalog, lessons=ctx.lessons),
                "lesson_problems": ctx.lessons.validate(),
                "schedule": settings.schedule.to_schedule().to_dict(),
                "schedule_phase": settings.schedule.to_schedule().action_for(datetime.datetime.now()),
            },
        )

    @app.post("/admin/maintenance")
    def admin_maintenance(
        request: Request,
        action: str = Form(...),
        scenario_id: str = Form(""),
        workload: str = Form(""),
        force: bool = Form(False),
        csrf: str = Form(""),
        user=Depends(require_instructor),  # noqa: B008
    ):
        """The operations an operator actually needs from a browser."""
        ctx.check_csrf(request, csrf)
        manager = ctx.manager
        try:
            if action == "reap":
                result = manager.reap()
                message = (
                    f"Reaped {len(result.get('recycled', []))} session(s); "
                    f"refilled {result.get('refilled') or 'nothing'}."
                )
            elif action == "refill":
                created = manager.refill_pool()
                message = f"Refilled: {created or 'nothing to do'}."
            elif action == "drain":
                if not scenario_id:
                    return ctx.redirect("/admin", request, "Draining needs a scenario.")
                removed = manager.drain_pool(scenario_id, workload or None)
                message = f"Destroyed {removed} unclaimed machine(s) for {scenario_key(scenario_id, workload)}."
            elif action == "templates":
                results = manager.build_templates(
                    [scenario_id] if scenario_id else None,
                    force=force,
                    workloads=[workload] if workload else None,
                )
                message = f"Template build: {results or 'nothing to build'}."
            elif action == "validate":
                problems = ctx.repo.validate(catalog=ctx.catalog, lessons=ctx.lessons)
                message = (
                    f"All {len(ctx.repo.list())} scenarios validate."
                    if not problems
                    else f"{len(problems)} problem(s) found: {problems[0][:140]}"
                )
            else:
                message = f"Unknown action {action!r}."
        except Exception as exc:  # noqa: BLE001 - an operator action must never 500 the panel
            message = f"{action} failed: {exc}"
        store.log_event(f"admin_{action}", message)
        return ctx.redirect("/admin", request, message)

    # -------------------------------------------------------------------- users --
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, user=Depends(require_instructor)):  # noqa: B008
        return page(
            request,
            "admin/users.html",
            "users",
            {"accounts": store.list_all_users(), "counts": store.count_users()},
        )

    # There is no create and no password reset: identity is Authentik's. An
    # account appears here the first time its owner signs in, and its role is
    # Authentik's group membership re-read on every sign-in — so the controls
    # that matter locally are enabling, disabling and deleting, not credentials.
    @app.post("/admin/users")
    def admin_users_action(
        request: Request,
        action: str = Form(...),
        username: str = Form(""),
        role: str = Form("student"),
        csrf: str = Form(""),
        user=Depends(require_instructor),  # noqa: B008
    ):
        ctx.check_csrf(request, csrf)
        username = username.strip().lower()
        if not username:
            return ctx.redirect("/admin/users", request, "A username is required.")
        # Guard rail: an instructor disabling themselves out of the panel is not a
        # thing to discover after the fact.
        if user["username"] == username and action in {"deactivate", "delete"}:
            return ctx.redirect("/admin/users", request, "You cannot disable your own account.")
        try:
            if action == "role":
                store.set_user_role(username, role)
                message = f"{username} is now {role}."
            elif action == "deactivate":
                store.deactivate_user(username)
                message = f"{username} can no longer sign in."
            elif action == "activate":
                store.activate_user(username)
                message = f"{username} can sign in again."
            elif action == "delete":
                store.delete_user(username)
                message = f"{username} deleted. Their results and tickets were kept."
            else:
                message = f"Unknown action {action!r}."
        except ValueError as exc:
            message = str(exc)
        store.log_event(f"admin_user_{action}", f"{username}: {message}")
        return ctx.redirect("/admin/users", request, message)

    # ---------------------------------------------------------------- scenarios --
    @app.get("/admin/scenarios", response_class=HTMLResponse)
    def admin_scenarios(request: Request, user=Depends(require_instructor)):  # noqa: B008
        problems = ctx.repo.validate(catalog=ctx.catalog, lessons=ctx.lessons)
        by_scenario: dict[str, list[str]] = {}
        for problem in problems:
            key = problem.split("]", 1)[0].lstrip("[") + "]"
            by_scenario.setdefault(key, []).append(problem)
        return page(
            request,
            "admin/scenarios.html",
            "scenarios",
            {
                "scenarios": ctx.repo.list(),
                "problems": problems,
                "problems_by_scenario": by_scenario,
                "templates": {
                    scenario_key(t["scenario_id"], t["workload"]): t
                    for t in safe(ctx.manager.template_status, [], "templates")
                },
                "lessons": {lesson.id: lesson for lesson in ctx.lessons.list()},
            },
        )

    # ---------------------------------------------------------------- platforms --
    @app.get("/admin/platforms", response_class=HTMLResponse)
    def admin_platforms(request: Request, user=Depends(require_instructor)):  # noqa: B008
        incus_aliases = safe(
            lambda: ctx.manager.incus.image_aliases() if ctx.manager.incus is not None else [],
            [],
            "Incus images",
        )
        groups = []
        for group in ctx.catalog.group_list():
            entries = []
            for entry in group.entries:
                try:
                    plan = ctx.catalog.plan(entry, image_ready=entry.image_alias in incus_aliases)
                except Exception:  # noqa: BLE001 - one broken entry must not break the page
                    plan = None
                entries.append({"entry": entry, "plan": plan})
            groups.append({"group": group, "entries": entries})
        return page(
            request,
            "admin/platforms.html",
            "platforms",
            {
                "groups": groups,
                "image_aliases": incus_aliases,
                "catalog_problems": ctx.catalog.validate() if hasattr(ctx.catalog, "validate") else [],
                "templates": safe(ctx.manager.template_status, [], "templates"),
                "pool": safe(ctx.manager.pool_status, [], "warm pool"),
                "pool_targets": settings.pool.targets,
                "default_target": settings.pool.default_target,
            },
        )

    # ------------------------------------------------------------------ tickets --
    @app.get("/admin/tickets", response_class=HTMLResponse)
    def admin_tickets(request: Request, user=Depends(require_instructor)):  # noqa: B008
        with_ticket = [s for s in ctx.repo.list() if s.ticket_form is not None]
        return page(
            request,
            "admin/tickets.html",
            "tickets",
            {
                "tickets": store.list_tickets(limit=200),
                "stats": store.count_tickets(),
                "scenarios_with_ticket": len(with_ticket),
                "scenario_count": len(ctx.repo.list()),
                "scenarios": {s.id: s for s in ctx.repo.list()},
                "forms": {s.id: s.ticket_form for s in with_ticket},
            },
        )

    @app.get("/admin/tickets/{session_id}", response_class=HTMLResponse)
    def admin_ticket_detail(request: Request, session_id: int, user=Depends(require_instructor)):  # noqa: B008
        session = store.get_session(session_id)
        scenario = ctx.repo.get(session.scenario_id) if session else None
        form = ctx.manager.ticket_form(scenario) if scenario else None
        grade = store.latest_ticket(session_id)
        return page(
            request,
            "admin/ticket_detail.html",
            "tickets",
            {
                "session": session,
                "scenario": scenario,
                "form": form,
                "grade": grade,
                "answers": store.ticket_values(session_id) if grade else store.ticket_draft(session_id),
                "text": ticket_feedback_text(form, grade) if (form and grade) else "",
                "attempts": store.tickets_for_session(session_id),
            },
        )

    # ----------------------------------------------------------------- sessions --
    @app.get("/admin/sessions", response_class=HTMLResponse)
    def admin_sessions(
        request: Request,
        state: str = "",
        student: str = "",
        scenario: str = "",
        limit: int = 200,
        user=Depends(require_instructor),  # noqa: B008
    ):
        valid_states = {s.value for s in SessionState}
        states = [SessionState(state)] if state in valid_states else None
        sessions = store.list_sessions(
            states=states,
            student=student or None,
            scenario_id=scenario or None,
            limit=max(1, min(int(limit or 200), 1000)),
        )
        rows = [
            {
                "session": session,
                "remaining": session.seconds_remaining(),
                "console": bool(ctx.session_link(request, session)),
                "report": store.latest_report(session.id) if session.id else None,
            }
            for session in sessions
        ]
        return page(
            request,
            "admin/sessions.html",
            "sessions",
            {
                "rows": rows,
                "states": SessionState,
                "filters": {"state": state, "student": student, "scenario": scenario, "limit": limit},
                "scenarios": ctx.repo.list(),
            },
        )

    # ----------------------------------------------------------------- schedule --
    @app.get("/admin/schedule", response_class=HTMLResponse)
    def admin_schedule(request: Request, user=Depends(require_instructor)):  # noqa: B008
        schedule = settings.schedule.to_schedule()
        now = datetime.datetime.now()
        return page(
            request,
            "admin/schedule.html",
            "schedule",
            {
                "schedule": schedule,
                "phase": schedule.action_for(now),
                "planned": [a.to_dict() for a in schedule.actions(now)],
                "pool": safe(ctx.manager.pool_status, [], "warm pool"),
            },
        )

    @app.post("/admin/schedule/tick")
    def admin_schedule_tick(request: Request, csrf: str = Form(""), user=Depends(require_instructor)):  # noqa: B008
        ctx.check_csrf(request, csrf)
        scheduler = Scheduler(ctx.manager, settings.schedule.to_schedule())
        try:
            result = scheduler.tick()
            message = f"Schedule tick: {result}"
        except Exception as exc:  # noqa: BLE001
            message = f"Schedule tick failed: {exc}"
        store.log_event("admin_schedule_tick", message)
        return ctx.redirect("/admin/schedule", request, message)

    # ------------------------------------------------------------------ results --
    @app.get("/admin/results", response_class=HTMLResponse)
    def admin_results(request: Request, user=Depends(require_instructor)):  # noqa: B008
        return page(
            request,
            "admin/results.html",
            "results",
            {
                "rows": store.list_result_rows(limit=300),
                "leaderboard": store.leaderboard(),
                "scenarios": {s.id: s for s in ctx.repo.list()},
            },
        )

    @app.get("/admin/results.csv")
    def admin_results_csv(request: Request, user=Depends(require_instructor)):  # noqa: B008
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "student",
                "scenario_id",
                "machine_score",
                "ticket_score",
                "ticket_weight",
                "final_score",
                "resolved",
                "submitted_at",
            ]
        )
        for row in store.list_result_rows(limit=5000):
            report = row["report"]
            writer.writerow(
                [
                    row["student"],
                    report.scenario_id,
                    f"{report.machine_score:.1f}",
                    "" if report.ticket_score is None else f"{report.ticket_score:.1f}",
                    f"{report.ticket_weight:.0f}",
                    f"{report.score:.1f}",
                    "yes" if report.resolved else "no",
                    report.created_at,
                ]
            )
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.read()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=ontrak-results.csv"},
        )

    # -------------------------------------------------------------------- audit --
    @app.get("/admin/audit", response_class=HTMLResponse)
    def admin_audit(
        request: Request, kind: str = "", limit: int = 300, user=Depends(require_instructor)  # noqa: B008
    ):
        return page(
            request,
            "admin/audit.html",
            "audit",
            {
                "events": store.list_events(kind=kind or None, limit=max(1, min(int(limit or 300), 2000))),
                "kinds": store.event_kinds(),
                "selected": kind,
                "limit": limit,
            },
        )

    @app.get("/admin/state.json")
    def admin_state(request: Request, user=Depends(require_instructor)):  # noqa: B008
        """A machine-readable snapshot, for a wall dashboard or a monitoring probe."""
        manager = ctx.manager
        # A monitoring probe must answer even when Incus is down, or the probe
        # reports the whole platform as down when only the hypervisor is.
        pool = [p.__dict__ for p in safe(manager.pool_status, [], "warm pool")]
        templates = safe(manager.template_status, [], "templates")
        return JSONResponse(
            {
                "users": store.count_users(),
                "sessions": {state.value: store.count_sessions([state]) for state in SessionState},
                "tickets": store.count_tickets(),
                "pool": pool,
                "templates": templates,
                "infra_errors": take_failures(),
                "scenarios": len(ctx.repo.list()),
                "lessons": len(ctx.lessons.list()),
                "events": store.count_events(),
            }
        )
