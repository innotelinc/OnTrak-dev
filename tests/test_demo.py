"""Demo mode is the fastest way for someone to judge OnTrak, so it has to actually
work end to end: assign, provision, check, submit, tear down — and it has to honour
the results-only policy, since that is the promise the UI makes."""

from __future__ import annotations

import pytest

from ontrak import selection
from ontrak.demo import (
    DEMO_INSTRUCTOR,
    DemoDriver,
    account_names,
    build_demo_environment,
    run_demo,
    seed_accounts,
    seed_pool,
)
from ontrak.models import SessionState


@pytest.fixture()
def env(tmp_path):
    return build_demo_environment(state_dir=tmp_path / "demo-state", success_rate=1.0, seed=3)


def test_demo_needs_no_secrets(env):
    """Demo mode is the one mode that runs with an empty guest password and no keys."""
    assert env.settings.demo.enabled
    assert env.settings.guest.password == ""
    assert env.settings.portal.secret == ""


def test_demo_uses_an_in_memory_hypervisor(env):
    from ontrak.memory import InMemoryIncus

    assert isinstance(env.incus, InMemoryIncus)
    assert env.manager.incus is env.incus


def test_seed_accounts_is_idempotent(env):
    first = seed_accounts(env)
    second = seed_accounts(env)
    assert first == second
    # Rows with no credential: the demo door picks an account by name, and it
    # offers exactly the accounts that seeding creates.
    assert env.store.get_user(first[0])["role"] == "student"
    assert env.store.get_user(DEMO_INSTRUCTOR)["role"] == "instructor"
    assert set(account_names(env.settings)) == {*first, DEMO_INSTRUCTOR}


def test_driver_reports_every_objective_the_scenario_declares(env):
    scenario = env.repository.list()[0]
    driver = DemoDriver(env.settings, env.repository, success_rate=1.0)
    result = driver.run_powershell(f"& 'C:\\ProgramData\\OnTrak\\scenarios\\{scenario.id}\\check.ps1'")
    assert "###ONTRAK-JSON-BEGIN###" in result.stdout
    for objective in scenario.objectives:
        assert objective.id in result.stdout


def test_driver_answers_the_setup_probe(env):
    driver = DemoDriver(env.settings, env.repository)
    result = driver.run_powershell("& 'C:\\ProgramData\\OnTrak\\scenarios\\x\\setup.ps1'")
    assert "ONTRAK-SETUP-OK" in result.stdout


def test_partial_success_is_reproducible(env):
    scenario = env.repository.list()[0]
    driver = DemoDriver(env.settings, env.repository, success_rate=0.5, seed=11)
    path = f"check.ps1 {scenario.id}"
    first = driver.run_powershell(path).stdout
    again = DemoDriver(env.settings, env.repository, success_rate=0.5, seed=11).run_powershell(path).stdout
    assert first == again
    # ...and a different seed gives a different mix, so `--success-rate` is useful.
    other = DemoDriver(env.settings, env.repository, success_rate=0.5, seed=99).run_powershell(path).stdout
    assert other != first


def test_seed_pool_builds_a_template_per_scenario_and_platform(tmp_path):
    """A template is per (scenario, platform): the same fault on Ubuntu and Debian
    are different machines, so a scenario offered on both needs two templates."""
    env = build_demo_environment(state_dir=tmp_path / "s", success_rate=1.0)
    ids = [s.id for s in env.repository.list()]
    expected = {
        f"{scenario.id}@{workload}" if workload else scenario.id
        for scenario, workload in env.manager.workload_pairs()
    }
    built = seed_pool(env, ids, per_scenario=2, prewarm_ids=ids[:2])
    assert set(built) == expected
    # Every pair got a template; only the nominated scenarios got warm machines.
    assert all(row.template_ready for row in env.manager.pool_status())
    warm = {key: count for key, count in built.items() if count}
    assert warm, "the requested scenarios should have been warmed"
    assert all(count == 2 for count in warm.values())
    assert set(warm) < set(built), "only the nominated scenarios should be warm"


def test_run_demo_completes_a_whole_class(tmp_path):
    summary = run_demo(state_dir=tmp_path / "class", verbose=False, students=4)
    assert len(summary["students"]) == 4
    assert summary["completed"], "every student should have submitted a result"
    for row in summary["students"]:
        assert row["error"] == ""
        assert row["state"] in {SessionState.READY.value, SessionState.IN_USE.value, SessionState.PASSED.value}
    for row in summary["completed"]:
        assert row["score"] == 100.0
        assert row["resolved"] is True


def test_run_demo_stores_only_submitted_results(tmp_path):
    summary = run_demo(state_dir=tmp_path / "only-results", verbose=False, students=3)
    env_state = tmp_path / "only-results" / "ontrak.sqlite3"
    assert env_state.exists()

    from ontrak.store import Store

    store = Store(env_state)
    submitted = [session for session in store.list_sessions(limit=50) if session.state in {SessionState.PASSED, SessionState.FAILED}]
    assert submitted
    for session in submitted:
        # Exactly one stored result per submitted session: the preview checks left no trace.
        assert store.attempt_counts(session.id) == 1, session.id
        assert session.instance == "", "the machine is destroyed on submission"
    assert summary["results"]


def test_run_demo_can_preview_without_submitting(tmp_path):
    summary = run_demo(state_dir=tmp_path / "preview-only", verbose=False, students=2, complete_sessions=False)
    assert summary["graded"]
    assert summary["completed"] == []
    for row in summary["graded"]:
        assert row["preview_score"] == 100.0


def test_run_demo_mixes_up_the_scenarios(tmp_path):
    summary = run_demo(state_dir=tmp_path / "variety", verbose=False, students=6)
    assigned = {row["scenario_id"] for row in summary["students"]}
    assert len(assigned) >= 4, assigned
    for row in summary["students"]:
        assert row["reason"]


def test_run_demo_honours_a_fixed_scenario_list(tmp_path):
    chosen = ["net-dns-failure"]
    summary = run_demo(
        scenario_ids=chosen,
        state_dir=tmp_path / "single",
        verbose=False,
        students=3,
    )
    assert {row["scenario_id"] for row in summary["students"]} == set(chosen)


def test_run_demo_assigns_a_time_limit(tmp_path):
    summary = run_demo(state_dir=tmp_path / "limits", verbose=False, students=2)
    for row in summary["students"]:
        assert row["minutes"] in (45, 90, 180)
    assert summary["stats"]["students"] >= 2


def test_demo_summary_renders(tmp_path):
    from ontrak.demo import render_demo_summary

    summary = run_demo(state_dir=tmp_path / "render", verbose=False, students=2)
    text = render_demo_summary(summary)
    assert "OnTrak demo run" in text
    assert "student1" in text
    assert "Only the submitted grade is stored" in text


def test_workload_aware_assignment_can_be_used_in_a_demo(env):
    """The demo exposes the same selection API the portal uses."""
    entry = env.catalog.get("win11-24h2")
    choice = selection.choose(env.repository.list(), workload=entry, seed=1)
    assert choice.scenario.category in entry.scenario_families
