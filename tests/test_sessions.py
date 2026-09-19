from __future__ import annotations

import json
from datetime import timedelta

import pytest

from ontrak.demo import synthesise_ticket
from ontrak.guest import NullDriver
from ontrak.incus import IncusError
from ontrak.models import SessionState, iso, parse_iso, utcnow
from ontrak.scenarios import JSON_BEGIN, JSON_END
from ontrak.sessions import POOL_SNAPSHOT, SessionError, SessionManager

SCENARIO = "net-dns-failure"
OBJECTIVES = ["restore-resolver", "resolve-intranet", "reach-service"]


def payload(**passed: object) -> str:
    checks = [{"objective": key, "passed": value, "detail": f"{key}={value}"} for key, value in passed.items()]
    return f"{JSON_BEGIN}{json.dumps({'checks': checks})}{JSON_END}"


def full_pass() -> str:
    return payload(**{objective: True for objective in OBJECTIVES})


def partial() -> str:
    return payload(**{"restore-resolver": True, "resolve-intranet": False, "reach-service": False})


def manager_with(settings, store, repo, incus, responses: dict[str, str]) -> SessionManager:
    return SessionManager(
        settings, store, repo=repo, incus=incus, driver=NullDriver(settings, responses=responses)
    )


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def settings_template(manager) -> str:
    return manager.settings.incus.template_name(SCENARIO)


def test_template_build_injects_the_fault_and_snapshots(manager, incus, settings):
    name = manager.ensure_template(SCENARIO)
    assert name == settings.incus.template_name(SCENARIO)
    assert incus.exists(name)
    assert incus.has_snapshot(name, POOL_SNAPSHOT)
    assert incus.instance_status(name) == "STOPPED"  # snapshots come from a clean power-off


def test_an_unknown_scenario_id_is_reported_rather_than_skipped(manager):
    """A typo used to print nothing at all and exit 0.

    An operator who asks for a template that does not exist was told nothing, which
    reads exactly like a build that happened. The rest of the request still runs.
    """
    results = manager.build_templates([SCENARIO, "net-dns-fialure"])
    assert results["net-dns-fialure"].startswith("failed: unknown scenario")
    assert "net-dns-failure" in results["net-dns-fialure"], results["net-dns-fialure"]
    assert [key for key, status in results.items() if status == "ready"], results


def test_template_build_is_idempotent(manager, incus):
    first = manager.ensure_template(SCENARIO)
    copies_before = len([c for c in incus.calls if c[0] == "create_instance"])
    again = manager.ensure_template(SCENARIO)
    assert first == again
    assert len([c for c in incus.calls if c[0] == "create_instance"]) == copies_before


def test_template_build_can_be_forced(manager, incus, settings):
    manager.ensure_template(SCENARIO)
    manager.ensure_template(SCENARIO, force=True)
    assert len([c for c in incus.calls if c[0] == "delete_instance"]) >= 1
    assert incus.has_snapshot(settings.incus.template_name(SCENARIO), POOL_SNAPSHOT)


def test_template_build_fails_loudly_when_setup_does_not_confirm(settings, store, repo, incus):
    manager = manager_with(settings, store, repo, incus, responses={})  # no ONTRAK-SETUP-OK
    with pytest.raises(SessionError, match="ONTRAK-SETUP-OK"):
        manager.ensure_template(SCENARIO)
    assert not incus.has_snapshot(settings.incus.template_name(SCENARIO), POOL_SNAPSHOT)
    assert incus.instance_status(settings.incus.template_name(SCENARIO)) == "STOPPED"


def test_template_build_requires_the_golden_image(settings, store, repo):
    from .helpers import FakeIncus

    incus = FakeIncus(image_alias=settings.incus.image_alias, image_present=False)
    manager = manager_with(settings, store, repo, incus, responses={"setup.ps1": "ONTRAK-SETUP-OK"})
    with pytest.raises(SessionError, match="golden image"):
        manager.ensure_template(SCENARIO)


def test_scenario_declared_devices_are_attached_to_the_template(manager, incus, settings):
    name = manager.ensure_template("hw-driver-device")
    devices = [d for d in incus.devices if d[0] == name]
    assert devices, "the hardware scenario needs its second NIC attached before first boot"
    _, kind, device_name, options = devices[0]
    assert (kind, device_name) == ("nic", "eth1")
    assert options["network"] == settings.incus.network


def test_build_templates_reports_per_scenario_results(manager):
    results = manager.build_templates(["net-dns-failure", "sw-app-crash"])
    assert results == {"net-dns-failure": "ready", "sw-app-crash": "ready"}


# --------------------------------------------------------------------------- #
# availability — refusing a scenario the range cannot start
# --------------------------------------------------------------------------- #
# A student who started a scenario the range could not run used to end up with a
# session row whose entire content was `template tpl-sw-app-crash is missing
# snapshot clean` — an operator's message, delivered after a slot was spent. The
# availability check is what turns that into a refusal they can act on, so it is
# tested for both the refusal and the two ways it must stay silent.
#
def test_a_built_template_is_available(manager, built_template):
    assert manager.scenario_availability(SCENARIO) == ""


def test_a_scenario_with_no_template_is_refused_by_name(manager, incus):
    reason = manager.scenario_availability("sw-app-crash")
    assert reason.startswith("sw-app-crash is not available on this range yet")
    # The repair is named, because "unavailable" without a next step is just a wall.
    assert "tpl-sw-app-crash" in reason
    assert "ontrak template build sw-app-crash" in reason


def test_a_missing_golden_image_is_named_as_the_cause(manager, incus):
    """The Windows scenarios layer differently from the Linux ones: none of them
    has a template, and building one template would not fix any of them."""
    incus.image_present = False
    reason = manager.scenario_availability("sw-app-crash")
    assert "ontrak-win-base" in reason
    assert "build-golden-image.sh" in reason


def test_a_waiting_pooled_machine_makes_a_scenario_available(manager, incus, settings):
    """No template, but a booted machine already in the pool: the session can run,
    so refusing it would be wrong."""
    incus.add_instance(settings.incus.pool_name("sw-app-crash", 1), running=True)
    assert manager.scenario_availability("sw-app-crash") == ""


def test_an_unreachable_hypervisor_is_not_a_missing_template(manager, incus):
    """An Incus outage cannot prove a scenario is unrunnable, only that we cannot
    tell — refusing every scenario would be a worse failure than the outage."""
    incus.exists = _boom  # type: ignore[method-assign]
    assert manager.scenario_availability("sw-app-crash") == ""
    assert manager.unavailable_scenarios() == {}


def test_unavailable_scenarios_lists_what_cannot_start(manager, incus, built_template):
    reasons = manager.unavailable_scenarios()
    assert SCENARIO not in reasons  # its template is built
    assert "sw-app-crash" in reasons


def _boom(*args, **kwargs):
    raise IncusError(["list", "--format=json"], 1, "The incus daemon doesn't appear to be started")


# --------------------------------------------------------------------------- #
# allocation
# --------------------------------------------------------------------------- #
def test_allocate_clones_the_template_and_reaches_ready(manager, incus, built_template, settings):
    session = manager.allocate("Alice", SCENARIO)
    assert session.state is SessionState.READY
    assert session.student == "alice"
    assert session.instance == settings.incus.session_name(SCENARIO, session.id)
    assert session.host_ip
    assert session.expires_at and parse_iso(session.expires_at) > utcnow()


def test_allocate_claims_a_prewarmed_pool_vm(manager, incus, settings):
    pooled = settings.incus.pool_name(SCENARIO, 1)
    incus.add_instance(pooled, running=True, ip="10.20.0.77")
    session = manager.allocate("bob", SCENARIO)
    assert session.state is SessionState.READY
    assert session.instance == pooled
    assert session.host_ip == "10.20.0.77"
    # no clone was needed
    assert not [c for c in incus.calls if c[0] == "copy_instance"]


def test_allocate_is_idempotent_per_student_and_scenario(manager, built_template):
    first = manager.allocate("alice", SCENARIO)
    second = manager.allocate("alice", SCENARIO)
    assert first.id == second.id


def test_allocate_enforces_one_session_per_student(manager, incus, built_template):
    manager.allocate("alice", SCENARIO)
    with pytest.raises(SessionError, match="already has a live session"):
        manager.allocate("alice", "sw-app-crash")


def test_allocate_without_a_template_reports_an_error_session(manager, incus):
    session = manager.allocate("alice", "sw-app-crash")
    assert session.state is SessionState.ERROR
    assert "missing snapshot" in session.error or "template" in session.error
    # nothing was left running
    assert not incus.live_names()


def test_allocate_marks_a_failed_guest_handshake(settings, store, repo, incus, built_template):
    class NeverReady(NullDriver):
        def wait_ready(self, session, timeout=None):  # noqa: D102 - test double
            return False

    manager = SessionManager(
        settings, store, repo=repo, incus=incus,
        driver=NeverReady(settings, responses={"setup.ps1": "ONTRAK-SETUP-OK"}),
    )
    session = manager.allocate("alice", SCENARIO)
    assert session.state is SessionState.ERROR
    assert "never became reachable" in session.error


def test_randomised_credentials_are_applied_when_enabled(settings, store, repo, incus, built_template):
    settings.session.randomize_credentials = True
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK"})
    session = manager.allocate("alice", SCENARIO)
    assert session.state is SessionState.READY
    assert session.rdp_password != settings.guest.password
    assert len(session.rdp_password) >= 16


# --------------------------------------------------------------------------- #
# warm pool
# --------------------------------------------------------------------------- #
def test_prewarm_creates_booted_unclaimed_vms(manager, incus, built_template):
    created = manager.prewarm(SCENARIO, 3)
    assert created == 3
    status = manager.pool_status(SCENARIO)[0]
    assert status.ready == 3
    assert status.total == 3
    assert status.claimed == 0
    assert all(incus.instance_status(name) == "RUNNING" for name in incus.instances if "pool" in name)


def test_prewarm_respects_max_total(manager, incus, built_template, settings):
    assert settings.pool.max_total == 4
    assert manager.prewarm(SCENARIO, 10) == 4
    assert manager.prewarm(SCENARIO, 5) == 0


def test_refill_tops_up_to_the_configured_target(settings, store, repo, incus, built_template):
    settings.pool.targets = {SCENARIO: 2}
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK"})
    created = manager.refill_pool()
    assert created == {SCENARIO: 2}
    assert manager.refill_pool() == {}  # already at target
    assert manager.pool_status(SCENARIO)[0].deficit == 0


def test_refill_does_not_over_create_while_students_hold_the_pool(settings, store, repo, incus, built_template):
    """A full class must not trigger a second wave of VMs.

    The claimed instances keep their pool names and count against the target, so
    the host never has to hold `students + target` Windows VMs at once.
    """
    settings.pool.targets = {SCENARIO: 2}
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK"})
    assert manager.prewarm(SCENARIO, 2) == 2

    alice = manager.allocate("alice", SCENARIO)
    bob = manager.allocate("bob", SCENARIO)
    assert {alice.instance, bob.instance} == {settings.incus.pool_name(SCENARIO, 1), settings.incus.pool_name(SCENARIO, 2)}

    status = manager.pool_status(SCENARIO)[0]
    assert (status.target, status.total, status.ready, status.claimed) == (2, 2, 0, 2)
    assert status.deficit == 0
    assert status.shortfall == 2  # nothing on the shelf, but that is not capacity
    assert manager.refill_pool() == {}
    assert len([name for name in incus.instances if "pool" in name]) == 2

    # once a session ends, the slot is refilled for the next student
    manager.end(alice)
    assert manager.refill_pool() == {SCENARIO: 1}


def test_pool_status_reports_missing_templates(manager, incus):
    rows = {row.scenario_id: row for row in manager.pool_status()}
    assert rows[SCENARIO].template_ready is False
    manager.ensure_template(SCENARIO)
    rows = {row.scenario_id: row for row in manager.pool_status()}
    assert rows[SCENARIO].template_ready is True


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #
def test_checks_grade_a_passing_attempt(settings, store, repo, incus, built_template):
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK", "check.ps1": full_pass()})
    session = manager.allocate("alice", SCENARIO)
    assert session.instance  # claimed the pooled entry from the fixture

    report = manager.run_checks(session)
    assert report.score == 100.0
    assert report.resolved is True
    assert session.state is SessionState.PASSED
    assert session.resolved is True
    assert session.best_score == 100.0
    assert session.checks_run == 1
    # Results-only policy: a preview check gives the student feedback but is not
    # recorded. Only Complete & End stores a result.
    assert store.attempt_counts(session.id) == 0
    assert store.latest_report(session.id) is None

    final = manager.complete(session, values=synthesise_ticket(manager.ticket_form_for(session)))
    assert final.score == 100.0
    assert final.resolved is True
    assert session.state is SessionState.PASSED
    assert store.attempt_counts(session.id) == 1
    # The machine is destroyed on submission: nothing is left to tamper with.
    assert incus.exists(session.instance) is False


def test_checks_grade_a_failing_attempt_and_keep_the_session_usable(settings, store, repo, incus, built_template):
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK", "check.ps1": partial()})
    session = manager.allocate("alice", SCENARIO)
    report = manager.run_checks(session)

    assert report.score == 40.0
    assert report.resolved is False
    assert session.state is SessionState.IN_USE  # not a dead end: they can keep working
    assert session.resolved is False
    assert [o.objective_id for o in report.failed] == ["resolve-intranet", "reach-service"]
    assert all(o.reported for o in report.outcomes)  # the script reported all three


def test_best_score_is_kept_across_attempts(settings, store, repo, incus, built_template):
    response = {"setup.ps1": "ONTRAK-SETUP-OK"}
    manager = manager_with(settings, store, repo, incus, {**response, "check.ps1": partial()})
    session = manager.allocate("alice", SCENARIO)
    manager.run_checks(session)
    assert session.best_score == 40.0

    manager.driver.responses["check.ps1"] = full_pass()
    manager.run_checks(session)
    assert session.best_score == 100.0
    assert session.resolved is True
    assert session.checks_run == 2


def test_a_broken_check_script_reports_an_error_not_a_score(settings, store, repo, incus, built_template):
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK", "check.ps1": "crashed\n"})
    session = manager.allocate("alice", SCENARIO)
    report = manager.run_checks(session)
    assert report.error
    assert report.score == 0.0
    assert session.state is SessionState.IN_USE
    assert session.best_score == 0.0


def test_grading_does_not_re_upload_the_setup_script(settings, store, repo, incus, built_template):
    manager = manager_with(settings, store, repo, incus, {"setup.ps1": "ONTRAK-SETUP-OK", "check.ps1": full_pass()})
    session = manager.allocate("alice", SCENARIO)
    uploads_before = [c for c in manager.driver.calls if "setup.ps1" in c[1]]
    manager.run_checks(session)
    uploads_after = [c for c in manager.driver.calls if "setup.ps1" in c[1]]
    assert len(uploads_after) == len(uploads_before)


# --------------------------------------------------------------------------- #
# reset, recycle, reaping
# --------------------------------------------------------------------------- #
def test_reset_destroys_the_machine_and_hands_over_a_clean_one(manager, incus, built_template, settings):
    session = manager.allocate("alice", SCENARIO)
    broken_instance = session.instance
    incus.calls.clear()

    session = manager.reset(session)
    assert session.state is SessionState.READY
    assert session.instance == broken_instance  # same deterministic name, new VM
    assert ("delete_instance", broken_instance) in incus.calls
    assert any(c[0] == "copy_instance" for c in incus.calls)
    assert incus.instance_status(session.instance) == "RUNNING"
    assert session.best_score == 0.0  # scores belong to the previous attempt


def test_reset_prefers_a_ready_pool_vm(manager, incus, built_template, settings):
    session = manager.allocate("alice", SCENARIO)
    spare = settings.incus.pool_name(SCENARIO, 9)
    incus.add_instance(spare, running=True, ip="10.20.0.99")
    session = manager.reset(session)
    assert session.instance == spare
    assert session.host_ip == "10.20.0.99"


def test_reset_refuses_terminal_sessions(manager, built_template):
    session = manager.allocate("alice", SCENARIO)
    manager.end(session)
    assert session.state is SessionState.DESTROYED
    with pytest.raises(SessionError, match="cannot be reset"):
        manager.reset(session)


def test_end_destroys_the_instance(manager, incus, built_template):
    session = manager.allocate("alice", SCENARIO)
    instance = session.instance
    manager.end(session)
    assert session.state is SessionState.DESTROYED
    assert not incus.exists(instance)


def test_reap_expires_sessions_past_their_ttl(manager, store, incus, built_template):
    session = manager.allocate("alice", SCENARIO)
    instance = session.instance
    store.update_session(session.id, expires_at=iso(utcnow() - timedelta(minutes=1)))

    result = manager.reap()
    assert session.id in result["recycled"]
    assert store.get_session(session.id).state is SessionState.DESTROYED
    assert not incus.exists(instance)  # the student's VM is gone
    assert incus.exists(settings_template(manager))  # the template survives for the next student


def test_reap_reclaims_idle_sessions(manager, store, incus, built_template):
    session = manager.allocate("alice", SCENARIO)
    store.update_session(session.id, last_activity_at=iso(utcnow() - timedelta(hours=2)))
    result = manager.reap()
    assert session.id in result["recycled"]
    assert "idle" in store.get_session(session.id).notes


def test_reap_leaves_active_sessions_alone(manager, store, built_template):
    session = manager.allocate("alice", SCENARIO)
    manager.touch(session)
    result = manager.reap()
    assert result["recycled"] == []
    assert store.get_session(session.id).state is SessionState.READY


def test_reap_clears_stuck_provisioning_rows(manager, store, incus, built_template):
    session = manager.create_session("alice", SCENARIO)
    store.update_session(
        session.id, state=SessionState.ALLOCATING, created_at=iso(utcnow() - timedelta(hours=1))
    )
    manager.reap()
    reloaded = store.get_session(session.id)
    assert reloaded.state is SessionState.ERROR
    assert "timed out" in reloaded.error


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
def test_hints_reveal_one_at_a_time(manager, built_template, repo):
    session = manager.allocate("alice", SCENARIO)
    scenario = repo.get(SCENARIO)
    assert session.hint_level == 0
    manager.reveal_hint(session, scenario)
    assert session.hint_level == 1
    for _ in range(len(scenario.hints) + 3):
        manager.reveal_hint(session, scenario)
    assert session.hint_level == len(scenario.hints)


def test_extend_pushes_the_expiry_out(manager, built_template):
    session = manager.allocate("alice", SCENARIO)
    before = parse_iso(session.expires_at)
    manager.extend(session, 30)
    assert parse_iso(session.expires_at) > before


def test_ownership_is_enforced(manager, built_template):
    session = manager.allocate("alice", SCENARIO)
    assert manager.get_owned_session("alice", session.id).id == session.id
    with pytest.raises(SessionError, match="another student"):
        manager.get_owned_session("bob", session.id)
    assert manager.get_owned_session("bob", session.id, allow_instructor=True).id == session.id


def test_stats_snapshot(manager, built_template):
    manager.allocate("alice", SCENARIO)
    stats = manager.stats()
    assert stats["sessions"][SessionState.READY.value] == 1
    assert stats["pool"][0]["scenario_id"]
    assert isinstance(stats["templates"], list)
