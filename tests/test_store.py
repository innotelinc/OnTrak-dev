from __future__ import annotations

from ontrak.auth import ACCOUNT_SENTINEL
from ontrak.models import CheckOutcome, ScoreReport, Session, SessionState
from ontrak.store import Store


def make_session(student: str = "alice", scenario_id: str = "net-dns-failure") -> Session:
    return Session(
        id=None,
        student=student,
        scenario_id=scenario_id,
        state=SessionState.REQUESTED,
        rdp_user="student",
        rdp_password="secret",
    )


def test_session_round_trip(store):
    session = store.create_session(make_session())
    assert session.id and session.id > 0

    store.update_session(session.id, state=SessionState.READY, instance="tpl-x", host_ip="10.20.0.5")
    loaded = store.get_session(session.id)
    assert loaded.state is SessionState.READY
    assert loaded.instance == "tpl-x"
    assert loaded.host_ip == "10.20.0.5"


def test_update_rejects_unknown_columns(store):
    session = store.create_session(make_session())
    try:
        store.update_session(session.id, nonsense=1)
    except ValueError as exc:
        assert "nonsense" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_live_sessions_and_counts(store):
    store.create_session(make_session("alice"))
    done = store.create_session(make_session("bob"))
    store.update_session(done.id, state=SessionState.DESTROYED)

    assert [s.student for s in store.live_sessions_for("alice")] == ["alice"]
    assert store.live_sessions_for("bob") == []
    assert store.count_sessions(states=[SessionState.REQUESTED]) == 1
    assert store.count_sessions(states=[SessionState.DESTROYED]) == 1


def test_results_and_best_scores(store):
    session = store.create_session(make_session())
    for score, resolved in ((40.0, False), (90.0, True), (75.0, False)):
        report = ScoreReport(
            session_id=session.id,
            scenario_id=session.scenario_id,
            score=score,
            resolved=resolved,
            outcomes=[CheckOutcome(objective_id="a", passed=resolved, weight=10)],
        )
        store.add_result(report, session.student)

    latest = store.latest_report(session.id)
    assert latest.score == 75.0
    assert store.attempt_counts(session.id) == 3

    board = store.leaderboard()
    assert len(board) == 1
    assert board[0]["best"] == 90.0
    assert board[0]["attempts"] == 3
    assert board[0]["solved"] == 1


def test_events_are_recorded_and_ordered(store):
    session = store.create_session(make_session())
    store.log_event("ready", "first", session.id)
    store.log_event("checked", "second", session.id)
    events = store.events_for(session.id)
    assert [e["kind"] for e in events] == ["checked", "ready"]
    assert store.recent_events()[0]["kind"] == "checked"


def test_meta_round_trip(store):
    assert store.get_meta("nothing", "fallback") == "fallback"
    store.set_meta("schema", {"version": 2})
    assert store.get_meta("schema") == {"version": 2}
    store.set_meta("schema", 3)
    assert store.get_meta("schema") == 3


def test_accounts_carry_no_credential(store):
    store.upsert_user("alice", "student", "Alice A")
    store.upsert_user("teacher", "instructor", "Teacher T")

    alice = store.get_user("alice")
    assert alice["role"] == "student"
    # Every row carries the same sentinel, which is not a hash of anything — there
    # is no password path left for it to satisfy.
    assert alice["password_hash"] == ACCOUNT_SENTINEL
    assert store.get_user("nobody") is None
    assert [u["username"] for u in store.list_users("instructor")] == ["teacher"]

    store.deactivate_user("alice")
    assert store.get_user("alice") is None


def test_upsert_user_refreshes_role_and_re_enables(store):
    store.upsert_user("alice", "student")
    store.deactivate_user("alice")
    assert store.get_user("alice") is None

    store.upsert_user("alice", "instructor", "Alice A")
    alice = store.get_user("alice")
    assert alice["role"] == "instructor"
    assert alice["display_name"] == "Alice A"
    assert alice["password_hash"] == ACCOUNT_SENTINEL


def test_an_sso_sign_in_does_not_undo_a_disable(store):
    """Authentik says who exists; the range still decides who may come in."""
    store.upsert_user("alice", "student", "Alice A")
    store.deactivate_user("alice")

    store.upsert_sso_user("alice", display_name="Alice A", role="student")
    assert store.get_user("alice") is None
    assert store.get_user("ALICE") is None


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    first = Store(path)
    session = first.create_session(make_session("dave"))
    second = Store(path)
    assert second.get_session(session.id).student == "dave"
