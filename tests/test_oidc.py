"""Sign-in through Authentik, and the password path it replaces.

The fake provider is only three functions deep into the flow — discovery, the
code exchange and userinfo are exactly the boundaries where a real IdP is
involved, and everything on this side of them (the state, the callback origin,
the claim-to-role mapping, the session) is what these tests are about.
"""

from __future__ import annotations

import base64
import urllib.parse

import pytest

from ontrak import oidc

from .conftest import OIDC_CALLBACKS, OIDC_ISSUER

AUTHORIZE_URL = f"{OIDC_ISSUER}authorize/"
TOKEN_URL = f"{OIDC_ISSUER}token/"
USERINFO_URL = f"{OIDC_ISSUER}userinfo/"


# --------------------------------------------------------------------------- #
# doubles and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def authentik(monkeypatch):
    """A stand-in for Authentik, recording what the portal asked it."""
    state = {"claims": {"email": "Sam.Student@innotel.us", "name": "Sam Student", "groups": []}}

    def fetch_json(url, headers=None):
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "authorization_endpoint": AUTHORIZE_URL,
                "token_endpoint": TOKEN_URL,
                "userinfo_endpoint": USERINFO_URL,
            }
        if url == USERINFO_URL:
            state["userinfo_auth"] = (headers or {}).get("Authorization")
            return state["claims"]
        raise AssertionError(f"unexpected GET {url}")

    def post_form(url, form, headers=None):
        assert url == TOKEN_URL, url
        state["token_form"] = form
        state["token_auth"] = (headers or {}).get("Authorization")
        return {"access_token": "access-1"}

    monkeypatch.setattr(oidc, "fetch_json", fetch_json)
    monkeypatch.setattr(oidc, "post_form", post_form)
    # Discovery is cached for five minutes; a cache filled by a previous test
    # would hide this IdP from the flow entirely.
    oidc._discovery_cache.update({"issuer": "", "at": 0.0, "meta": {}})
    return state


def configured(settings, **overrides):
    """The portal config as the range deploys it, with any knob overridden."""
    portal = settings.portal
    portal.oidc_issuer = OIDC_ISSUER
    portal.oidc_client_id = "ontrak"
    portal.oidc_client_secret = "ontrak-client-secret"
    portal.oidc_redirect_uri = ",".join(OIDC_CALLBACKS)
    portal.oidc_instructor_group = "range-instructors"
    for key, value in overrides.items():
        setattr(portal, key, value)
    return portal


def start(client, origin: str = "") -> str:
    """Begin the flow, returning the `state` Authentik would hand back."""
    headers = {"host": origin} if origin else {}
    response = client.get("/oidc/login", headers=headers, follow_redirects=False)
    assert response.status_code == 303, response.text
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(response.headers["location"]).query)
    return query["state"][0]


def sign_in(client, *, origin: str = "", code: str = "code-1", state: str | None = None):
    """Run the whole flow: start it, then come back to the callback."""
    return client.get(
        f"/oidc/callback?code={code}&state={state if state is not None else start(client, origin)}",
        follow_redirects=False,
    )


# --------------------------------------------------------------------------- #
# posture
# --------------------------------------------------------------------------- #
def test_the_shipped_posture_is_sso_only_and_says_so_when_unusable(settings):
    assert oidc.public_config(settings.portal) == {
        "enabled": False,
        "provider": "Authentik",
        "issuerHost": "",
        "misconfigured": True,
    }


def test_sso_needs_all_four_values(settings):
    settings.portal.oidc_issuer = OIDC_ISSUER
    settings.portal.oidc_client_id = "ontrak"
    settings.portal.oidc_client_secret = "secret"
    assert not oidc.enabled(settings.portal)  # no callback registered yet
    settings.portal.oidc_redirect_uri = OIDC_CALLBACKS[0]
    assert oidc.enabled(settings.portal)


def test_a_half_configured_range_is_misconfigured_too(settings):
    """One missing value leaves SSO off — and off with no password form is no way in."""
    configured(settings, oidc_redirect_uri="")
    assert oidc.public_config(settings.portal) == {
        "enabled": False,
        "provider": "Authentik",
        "issuerHost": "auth.cerulean.innotel.us",
        "misconfigured": True,
    }


# --------------------------------------------------------------------------- #
# which callback a request gets
# --------------------------------------------------------------------------- #
def test_the_callback_follows_the_origin_the_sign_in_started_on(settings):
    portal = configured(settings)
    assert oidc.callback_for(portal, {"host": "student.ontrak.innotel.us"}) == OIDC_CALLBACKS[1]


def test_the_forwarded_host_and_proto_are_honoured(settings):
    portal = configured(settings)
    headers = {"x-forwarded-host": "admin.ontrak.innotel.us, proxy", "x-forwarded-proto": "https"}
    assert oidc.callback_for(portal, headers) == OIDC_CALLBACKS[2]


def test_an_unregistered_origin_falls_back_to_the_canonical_callback(settings):
    """A forged Host can only ever name an origin already registered."""
    portal = configured(settings)
    assert oidc.callback_for(portal, {"host": "attacker.example"}) == OIDC_CALLBACKS[0]
    assert oidc.callback_for(portal, {}) == OIDC_CALLBACKS[0]


def test_no_registered_callback_means_no_callback(settings):
    settings.portal.oidc_redirect_uri = ""
    assert oidc.callback_for(settings.portal, {"host": "anything"}) == ""


# --------------------------------------------------------------------------- #
# claims -> a portal identity
# --------------------------------------------------------------------------- #
def test_username_is_the_email_lowercased():
    assert oidc.username_for({"email": "Sam.Student@Innotel.US"}) == "sam.student@innotel.us"


def test_username_falls_back_to_the_preferred_username():
    assert oidc.username_for({"preferred_username": "sam"}) == "sam"


def test_an_account_with_no_identity_is_never_invented_one():
    """`sub` is an opaque digest on this platform, so it is not an identity."""
    assert oidc.username_for({"sub": "hashed-user-id"}) == ""
    assert oidc.username_for({"email": "   "}) == ""


def test_role_comes_from_the_instructor_group(settings):
    portal = configured(settings)
    assert oidc.role_for({"groups": ["range-instructors"]}, portal) == "instructor"
    assert oidc.role_for({"groups": ["students"]}, portal) == "student"
    assert oidc.role_for({}, portal) == "student"


def test_a_platform_admin_is_an_instructor(settings):
    portal = configured(settings, oidc_instructor_group="")
    assert oidc.role_for({"is_superuser": True}, portal) == "instructor"
    assert oidc.role_for({"is_superuser": False}, portal) == "student"


def test_a_cohort_group_gates_the_range(settings):
    portal = configured(settings, oidc_required_group="class-2026")
    assert oidc.entitled(portal, {"groups": ["class-2026", "students"]})
    assert not oidc.entitled(portal, {"groups": ["students"]})
    assert not oidc.entitled(portal, {})
    # Ungated by default: anyone Authentik vouches for may enter.
    assert oidc.entitled(configured(settings, oidc_required_group=""), {})


# --------------------------------------------------------------------------- #
# the flow, as the portal drives it
# --------------------------------------------------------------------------- #
def test_the_authorize_redirect_carries_the_state_and_the_callback(settings, authentik):
    portal = configured(settings)
    url = urllib.parse.urlsplit(oidc.authorize_url(portal, "state-1", OIDC_CALLBACKS[1]))
    query = urllib.parse.parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTHORIZE_URL
    assert query["client_id"] == ["ontrak"]
    assert query["redirect_uri"] == [OIDC_CALLBACKS[1]]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-1"]
    assert "groups" in query["scope"][0].split()


def test_the_exchange_is_authenticated_and_names_the_same_callback(settings, authentik):
    portal = configured(settings)
    assert oidc.exchange_code(portal, "code-1", OIDC_CALLBACKS[2]) == "access-1"
    assert authentik["token_form"]["redirect_uri"] == OIDC_CALLBACKS[2]
    assert authentik["token_form"]["grant_type"] == "authorization_code"
    scheme, _, credential = authentik["token_auth"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(credential).decode() == "ontrak:ontrak-client-secret"


def test_discovery_is_cached(settings, authentik, monkeypatch):
    portal = configured(settings)
    calls: list[str] = []
    real = oidc.fetch_json

    def counting(url, headers=None):
        calls.append(url)
        return real(url, headers)

    monkeypatch.setattr(oidc, "fetch_json", counting)
    oidc.discovery(portal)
    oidc.discovery(portal)
    assert len(calls) == 1


def test_userinfo_uses_the_access_token_as_a_bearer(settings, authentik):
    claims = oidc.userinfo(configured(settings), "access-1")
    assert claims["email"] == "Sam.Student@innotel.us"
    assert authentik["userinfo_auth"] == "Bearer access-1"


def test_a_token_reply_without_an_access_token_is_an_error(settings, authentik, monkeypatch):
    monkeypatch.setattr(oidc, "post_form", lambda url, form, headers=None: {})
    with pytest.raises(oidc.OidcError):
        oidc.exchange_code(configured(settings), "code-1", OIDC_CALLBACKS[0])


def test_an_unconfigured_portal_cannot_start_the_flow(settings, monkeypatch):
    monkeypatch.setattr(oidc, "fetch_json", lambda *a, **k: pytest.fail("must not call out"))
    with pytest.raises(oidc.OidcError):
        oidc.discovery(settings.portal)


# --------------------------------------------------------------------------- #
# the portal itself
# --------------------------------------------------------------------------- #
def test_the_login_page_offers_authentik_and_no_password_form(sso_env):
    client, _ = sso_env
    page = client.get("/login")
    assert page.status_code == 200
    assert "Sign in with Authentik" in page.text
    assert 'name="password"' not in page.text


def test_there_is_no_password_route_at_all(sso_env):
    """Not merely refused: the route does not exist, so there is nothing to guess."""
    client, _ = sso_env
    client.get("/login")
    token = client.cookies.get("ontrak_csrf")
    posted = client.post(
        "/login",
        data={"username": "alice", "password": "alice-pw", "csrf": token},
        follow_redirects=False,
    )
    assert posted.status_code == 405
    assert client.cookies.get("ontrak_session") is None


def test_a_student_signs_in_through_authentik(sso_env, authentik):
    client, app = sso_env
    done = sign_in(client)
    assert done.status_code == 303
    assert done.headers["location"] == "/dashboard"
    assert client.cookies.get(oidc.OIDC_COOKIE) is None  # the state never survives
    row = app.state.store.get_user("sam.student@innotel.us")
    assert row["role"] == "student"
    assert row["display_name"] == "Sam Student"
    assert client.get("/dashboard").status_code == 200


def test_the_instructor_group_decides_the_role(sso_env, authentik):
    client, app = sso_env
    authentik["claims"].update({"email": "teacher@innotel.us", "groups": ["range-instructors"]})
    sign_in(client)
    assert app.state.store.get_user("teacher@innotel.us")["role"] == "instructor"
    assert client.get("/instructor").status_code == 200


def test_a_group_change_takes_effect_on_the_next_sign_in(sso_env, authentik):
    """Authentik is the authority, so nothing local has to be kept in step."""
    client, app = sso_env
    authentik["claims"].update({"email": "teacher@innotel.us", "groups": ["range-instructors"]})
    sign_in(client)
    assert app.state.store.get_user("teacher@innotel.us")["role"] == "instructor"

    client.cookies.clear()
    authentik["claims"]["groups"] = []
    sign_in(client)
    assert app.state.store.get_user("teacher@innotel.us")["role"] == "student"
    assert client.get("/instructor", follow_redirects=False).status_code == 403


def test_a_cohort_group_keeps_everyone_else_out(sso_env, authentik):
    client, app = sso_env
    app.state.settings.portal.oidc_required_group = "class-2026"

    refused = sign_in(client)
    assert refused.headers["location"] == "/login"
    assert client.cookies.get("ontrak_session") is None
    assert app.state.store.get_user("sam.student@innotel.us") is None

    authentik["claims"]["groups"] = ["class-2026"]
    assert sign_in(client).headers["location"] == "/dashboard"
    assert app.state.store.get_user("sam.student@innotel.us") is not None


def test_a_deactivated_account_stays_deactivated(sso_env):
    """Local deactivation is the range's own control; SSO must not undo it."""
    client, app = sso_env
    sign_in(client)
    app.state.store.deactivate_user("sam.student@innotel.us")
    client.cookies.clear()

    refused = sign_in(client)
    assert refused.headers["location"] == "/login"
    assert client.cookies.get("ontrak_session") is None


def test_a_forged_or_replayed_state_never_reaches_the_exchange(sso_env, authentik):
    client, _ = sso_env
    state = start(client)

    forged = client.get("/oidc/callback?code=code-1&state=not-the-state", follow_redirects=False)
    assert forged.headers["location"] == "/login"
    assert client.cookies.get("ontrak_session") is None
    assert client.cookies.get(oidc.OIDC_COOKIE) is None
    assert "token_form" not in authentik, "a bad state must not spend the code"

    client.cookies.clear()  # a callback that arrived cold, with no state cookie
    cold = client.get(f"/oidc/callback?code=code-1&state={state}", follow_redirects=False)
    assert cold.headers["location"] == "/login"
    assert client.cookies.get("ontrak_session") is None


def test_authentik_refusing_is_reported_not_swallowed(sso_env, authentik):
    client, _ = sso_env
    start(client)
    refused = client.get("/oidc/callback?error=access_denied&state=x", follow_redirects=False)
    assert refused.headers["location"] == "/login"
    assert client.cookies.get("ontrak_session") is None
    assert "token_form" not in authentik


def test_the_exchange_uses_the_callback_the_sign_in_started_on(sso_env, authentik):
    client, _ = sso_env
    assert sign_in(client, origin="admin.ontrak.innotel.us").headers["location"] == "/dashboard"
    assert authentik["token_form"]["redirect_uri"] == OIDC_CALLBACKS[2]


def test_the_authorize_redirect_sends_the_browser_to_authentik(sso_env, authentik):
    client, _ = sso_env
    started = client.get("/oidc/login", follow_redirects=False)
    location = urllib.parse.urlsplit(started.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE_URL
    assert urllib.parse.parse_qs(location.query)["client_id"] == ["ontrak"]
    assert client.cookies.get(oidc.OIDC_COOKIE)


def test_a_range_with_no_sign_in_at_all_says_so(sso_env):
    client, app = sso_env
    app.state.settings.portal.oidc_redirect_uri = ""
    page = client.get("/login")
    assert "not set up" in page.text
    assert 'name="password"' not in page.text
    assert client.get("/oidc/login", follow_redirects=False).headers["location"] == "/login"


def test_the_demo_door_is_closed_on_a_real_range(sso_env):
    """The pick-an-account door exists for `ontrak demo serve`; a real range 404s it."""
    client, app = sso_env
    app.state.store.upsert_user("student1", "student", "Student One")
    refused = client.get("/demo/login/student1", follow_redirects=False)
    assert refused.status_code == 404
    assert client.cookies.get("ontrak_session") is None


def test_the_demo_door_signs_in_an_account_with_no_password(settings, incus):
    """Demo mode has no IdP, so the portal opens a passwordless door for itself."""
    from fastapi.testclient import TestClient

    from ontrak.portal.app import create_app

    if TestClient is None:  # pragma: no cover - exercised only without fastapi
        pytest.skip("fastapi/httpx not installed")
    settings.demo.enabled = True
    settings.demo.students = 2
    app = create_app(settings, incus=incus, driver=None)
    app.state.store.upsert_user("student1", "student")
    app.state.store.upsert_user("student2", "student")
    app.state.store.upsert_user("instructor", "instructor")
    with TestClient(app) as client:
        assert "Demo mode" in client.get("/login").text
        entered = client.get("/demo/login/student1", follow_redirects=False)
        assert entered.status_code == 303
        assert entered.headers["location"] == "/dashboard"
        assert client.get("/dashboard").status_code == 200
        # An account that is not on the demo roster is refused.
        stranger = client.get("/demo/login/nobody", follow_redirects=False)
        assert stranger.headers["location"] == "/login"
