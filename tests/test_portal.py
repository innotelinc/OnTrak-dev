from __future__ import annotations

import pytest

from ontrak.demo import synthesise_ticket
from ontrak.models import SessionState
from ontrak.tickets import WRITEUP_ACTION

from .conftest import csrf, login

SCENARIO = "net-dns-failure"
OBJECTIVES = ["restore-resolver", "resolve-intranet", "reach-service"]


@pytest.fixture
def app_client(app_env):
    """The shared portal fixture — this suite is its oldest and largest user."""
    return app_env


def provision(app, student: str = "alice"):
    """Wait for the background provisioning worker and return a fresh session.

    The portal deliberately provisions in a thread, so tests must not read a
    Session object captured before that thread finished. ``provision`` is
    serialised per session and re-reads the row, which makes this deterministic.
    """
    session = app.state.store.live_sessions_for(student)[0]
    return app.state.manager.provision(session)


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_health_endpoint(app_client):
    client, _ = app_client
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["scenarios"] >= 1


def test_dashboard_requires_login(app_client):
    client, _ = app_client
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_a_signed_in_student_lands_on_the_dashboard(app_client):
    client, _ = app_client
    login(client, "alice")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Your training machines" in response.text
    assert "Nothing resolves on the intranet" in response.text  # a scenario title


def test_logout_clears_the_session(app_client):
    client, _ = app_client
    login(client, "alice")
    response = client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert client.get("/dashboard").text.count("Sign in") >= 1


def test_csrf_is_enforced_on_posts(app_client):
    client, _ = app_client
    login(client, "alice")
    response = client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": "forged"})
    assert response.status_code == 400


def test_instructor_area_requires_the_role(app_client):
    client, _ = app_client
    login(client, "alice")
    assert client.get("/instructor").status_code == 403
    client.post("/logout", data={"csrf": csrf(client)})
    login(client, "teacher")
    assert client.get("/instructor").status_code == 200


# --------------------------------------------------------------------------- #
# session flow
# --------------------------------------------------------------------------- #
def test_full_student_flow(app_client):
    client, app = app_client
    login(client, "alice")

    start = client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    assert start.status_code == 200  # redirect followed

    session = provision(app)
    # `is_usable`, not bare READY: the POST above follows its redirect into the
    # session page, and a page view claims a READY machine (READY -> IN_USE) —
    # which of the two the student lands on depends on whether the provisioning
    # thread finished before that page was rendered. Both are equally usable,
    # which is what the rest of this test already asserts about the state.
    assert session.state.is_usable

    page = client.get(f"/sessions/{session.id}")
    assert page.status_code == 200
    assert "Nothing resolves on the intranet" in page.text
    assert "guac.test" in page.text  # the console iframe points at the gateway
    assert "Check my work" in page.text

    status = client.get(f"/sessions/{session.id}/status").json()
    assert status["ready"] is True
    assert status["state"] in {"ready", "in_use"}

    checked = client.post(f"/sessions/{session.id}/check", data={"csrf": csrf(client)})
    assert checked.status_code == 200
    assert "Resolved" in checked.text
    # A practice check is shown to the student and deliberately not stored.
    assert app.state.store.latest_report(session.id) is None
    assert "not recorded" in checked.text

    # Complete & End grades once, stores that result, and destroys the machine. The
    # write-up is part of the submission, so it is filled in like a student would —
    # including the submitting control the buttons carry (the ticket's own `action`
    # field is data, not a command: see tickets.WRITEUP_ACTION).
    form = app.state.manager.ticket_form_for(session)
    completed = client.post(
        f"/sessions/{session.id}/complete",
        data={**synthesise_ticket(form), WRITEUP_ACTION: "complete", "csrf": csrf(client)},
    )
    assert completed.status_code == 200
    report = app.state.store.latest_report(session.id)
    assert report is not None
    assert report.score == 100.0
    assert report.resolved is True
    assert "objective-by-objective" in completed.text or "Submitted" in completed.text
    results = client.get("/results")
    assert results.status_code == 200
    assert "100%" in results.text
    assert report.resolved is True


def test_starting_a_scenario_this_range_cannot_run_is_refused(app_client):
    """The refusal has to happen before a session row exists.

    The whole point of asking first is that the student does not end up holding a
    session whose only content is an operator's template error — a spent slot that
    also has to be cleaned up. So this asserts the message *and* the absence of the
    row the old path would have created.
    """
    client, app = app_client
    login(client, "alice")

    response = client.post(
        "/sessions/start", data={"scenario_id": "sw-app-crash", "csrf": csrf(client)}
    )
    assert response.status_code == 200  # the redirect was followed
    assert "not available on this range yet" in response.text
    assert not app.state.store.list_sessions(scenario_id="sw-app-crash")
    assert app.state.store.live_sessions_for("alice") == []


def test_the_dashboard_flags_a_scenario_that_cannot_start(app_client):
    client, app = app_client
    login(client, "alice")
    page = client.get("/dashboard")
    assert page.status_code == 200
    # The card is still shown (a student may want to ask for it) but it carries the
    # reason and no start button, instead of looking identical to a working one.
    assert "sw-app-crash" in page.text
    assert "not available on this range yet" in page.text
    # ...and no start form: the hidden scenario_id input is how a card submits.
    assert 'value="sw-app-crash"' not in page.text
    # The scenario that can run still has its button.
    assert f'value="{SCENARIO}"' in page.text


def test_hints_unlock_only_after_an_attempt(app_client):
    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)

    blocked = client.post(f"/sessions/{session.id}/hint", data={"csrf": csrf(client)})
    assert "hints unlock after your first attempt" in blocked.text.lower()
    assert app.state.store.get_session(session.id).hint_level == 0

    client.post(f"/sessions/{session.id}/check", data={"csrf": csrf(client)})
    revealed = client.post(f"/sessions/{session.id}/hint", data={"csrf": csrf(client)})
    assert "Hint revealed" in revealed.text
    assert app.state.store.get_session(session.id).hint_level == 1


def test_reset_hands_over_a_clean_machine(app_client):
    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)
    instance = session.instance
    assert instance, "provisioning should have produced an instance"

    response = client.post(f"/sessions/{session.id}/reset", data={"csrf": csrf(client)})
    assert response.status_code == 200
    reloaded = app.state.store.get_session(session.id)
    # The follow-up page render claims the fresh VM for use, so READY or IN_USE
    # are both correct here.
    assert reloaded.state in {SessionState.READY, SessionState.IN_USE}
    assert reloaded.instance == instance
    assert app.state.incus.exists(instance)
    assert app.state.incus.instance_status(instance) == "RUNNING"


def test_saving_the_writeup_does_not_hand_the_session_in(app_client):
    """The ticket's own ``action`` field must not shadow the submit button.

    Ten of the shipped scenarios ask "what you changed" as a field called ``action``,
    and the write-up fields share one HTML form with the three submitting buttons.
    With the control named ``action`` too, the field was serialised first, so the
    handler read the student's prose, matched neither save nor preview, and fell
    through to Complete & End: clicking *Save draft* graded the machine and destroyed
    it. The regression is invisible to a test that only posts a full submission.
    """
    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)
    instance = session.instance

    saved = client.post(
        f"/sessions/{session.id}/complete",
        data={"action": "Edited the resolver config", WRITEUP_ACTION: "save", "csrf": csrf(client)},
    )
    assert "saved as a draft" in saved.text.lower()
    still = app.state.store.get_session(session.id)
    assert still.state.is_live, f"saving a draft closed the session: {still.state}"
    assert app.state.store.latest_report(session.id) is None
    assert app.state.incus.exists(instance)

    previewed = client.post(
        f"/sessions/{session.id}/complete",
        data={"action": "Edited the resolver config", WRITEUP_ACTION: "preview", "csrf": csrf(client)},
    )
    assert "preview" in previewed.text.lower()
    assert app.state.store.get_session(session.id).state.is_live
    assert app.state.incus.exists(instance)


def test_a_submission_with_no_control_button_is_not_a_completion(app_client):
    """A POST that arrives without the submitting control must not grade and destroy.

    Browsers always send the button that was clicked; a hand-built request does not.
    Defaulting that case to Complete & End made an accidental submission fatal, so it
    defaults to the draft save instead.
    """
    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)
    instance = session.instance

    response = client.post(f"/sessions/{session.id}/complete", data={"csrf": csrf(client)})
    assert "saved as a draft" in response.text.lower()
    assert app.state.store.latest_report(session.id) is None
    assert app.state.incus.exists(instance)
    assert app.state.store.get_session(session.id).state.is_live


def test_a_refused_console_key_is_explained_to_the_student(app_client, monkeypatch):
    """A gateway that refuses our key must not present as a blank iframe.

    The portal signs every console link and never sees the gateway's answer, so a
    gateway with a different key — or one without the JSON auth extension — left the
    student looking at an empty frame, and nothing in either log said why. The page
    says so now, in words a student can hand to an instructor.
    """
    from ontrak.portal import app as portal_app

    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)

    monkeypatch.setattr(
        portal_app.guac,
        "probe_gateway",
        lambda settings, **kwargs: (
            "refused",
            "the console gateway rejected a payload signed with guac.secret_key "
            "(HTTP 403). Its JSON_SECRET_KEY differs from this key",
        ),
    )
    # The page render that followed the POST already cached a verdict; make this
    # render ask again rather than reusing it.
    app.state.console_gateway = None

    page = client.get(f"/sessions/{session.id}")
    assert "The console cannot open right now" in page.text
    assert "JSON_SECRET_KEY" in page.text
    assert "not a fault in your virtual machine" in page.text
    # The console is still offered: a stale verdict must not remove a working one.
    assert "guac.test" in page.text


def test_a_healthy_console_key_shows_no_warning(app_client, monkeypatch):
    from ontrak.portal import app as portal_app

    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)
    monkeypatch.setattr(
        portal_app.guac, "probe_gateway", lambda settings, **kwargs: ("ok", "accepted")
    )
    app.state.console_gateway = None

    page = client.get(f"/sessions/{session.id}")
    assert "The console cannot open right now" not in page.text
    assert "guac.test" in page.text


def test_a_student_cannot_open_someone_elses_session(app_client):
    client, app = app_client
    other = app.state.manager.allocate("bob", SCENARIO)
    login(client, "alice")

    response = client.get(f"/sessions/{other.id}")
    assert response.status_code == 200
    assert "belongs to another student" in response.text
    assert other.host_ip not in response.text  # no console, no address leak
    assert client.get(f"/sessions/{other.id}/status").status_code == 404

    # and a student cannot reset or end someone else's machine either
    assert app.state.incus.instance_status(other.instance) == "RUNNING"
    client.post(f"/sessions/{other.id}/reset", data={"csrf": csrf(client)})
    client.post(f"/sessions/{other.id}/end", data={"csrf": csrf(client)})
    assert app.state.incus.exists(other.instance)
    assert app.state.store.get_session(other.id).state is not SessionState.DESTROYED


def test_end_session_destroys_the_vm(app_client):
    client, app = app_client
    login(client, "alice")
    client.post("/sessions/start", data={"scenario_id": SCENARIO, "csrf": csrf(client)})
    session = provision(app)
    instance = session.instance

    response = client.post(f"/sessions/{session.id}/end", data={"csrf": csrf(client)})
    assert response.status_code == 200
    assert app.state.store.get_session(session.id).state is SessionState.DESTROYED
    assert not app.state.incus.exists(instance)


# --------------------------------------------------------------------------- #
# instructor
# --------------------------------------------------------------------------- #
def test_instructor_page_shows_pool_and_results(app_client):
    client, app = app_client
    app.state.manager.allocate("bob", SCENARIO)
    login(client, "teacher")
    page = client.get("/instructor")
    assert page.status_code == 200
    assert "Capacity: templates and warm pool" in page.text
    assert "bob" in page.text
    assert "Build template" in page.text


def test_results_csv_export(app_client):
    client, app = app_client
    session = app.state.manager.allocate("bob", SCENARIO)
    app.state.manager.complete(session)
    login(client, "teacher")
    response = client.get("/instructor/results.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "bob" in response.text
    assert "scenario_id" in response.text


def test_instructor_can_prewarm_and_rebuild_templates(app_client):
    client, app = app_client
    login(client, "teacher")
    prewarm = client.post(
        "/instructor/prewarm",
        data={"scenario_id": SCENARIO, "count": 2, "csrf": csrf(client)},
    )
    assert prewarm.status_code == 200
    assert app.state.manager.pool_status(SCENARIO)[0].ready == 2

    rebuild = client.post(
        "/instructor/template",
        data={"scenario_id": SCENARIO, "csrf": csrf(client)},
    )
    assert rebuild.status_code == 200


def test_students_cannot_reach_instructor_actions(app_client):
    client, _ = app_client
    login(client, "alice")
    response = client.post(
        "/instructor/prewarm", data={"scenario_id": SCENARIO, "count": 5, "csrf": csrf(client)}
    )
    assert response.status_code == 403
