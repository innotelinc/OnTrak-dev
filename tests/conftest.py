from __future__ import annotations

import os
from pathlib import Path

import pytest

from ontrak.config import ENV_PREFIX, load_settings
from ontrak.guest import NullDriver
from ontrak.scenarios import ScenarioRepository
from ontrak.sessions import SessionManager
from ontrak.store import Store

from .helpers import FakeIncus

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "scenarios"

GUAC_KEY = "0123456789abcdef0123456789abcdef"

try:  # FastAPI + httpx are optional at runtime; skip cleanly if absent
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Keep the operator's own environment out of the suite.

    ``load_settings`` falls back to ``os.environ`` whenever it is not handed an
    explicit mapping, and exporting ``.env`` is a normal way to configure a
    deployment — so a developer's shell silently reconfigures the tests. That is
    how a stale ``ONTRAK_GUAC__PUBLIC_PORT`` (a key the app no longer knows, left
    in ``.env`` by an older layout) turned every test into a ConfigError, and how
    an exported ``ONTRAK_GUEST__PASSWORD`` broke demo mode's promise that it runs
    with no secrets at all. Tests that are about environment overrides pass
    ``environ=`` themselves; everything else should see a clean one.
    """
    for key in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(key)


@pytest.fixture
def settings(tmp_path):
    """Settings pointed at a temp state dir and the real scenario catalogue."""
    return load_settings(
        overrides={
            "paths": {"scenarios": str(SCENARIOS_DIR), "state": str(tmp_path / "state")},
            "guest": {
                "driver": "null",
                "user": "student",
                "password": "TrainMe!12345",
                "boot_timeout_seconds": 1,
                "ready_timeout_seconds": 1,
            },
            "session": {
                "ttl_minutes": 90,
                "idle_recycle_minutes": 30,
                "max_per_student": 1,
                "check_timeout_seconds": 5,
            },
            "incus": {"image_alias": "ontrak-win-base", "network": "ontrak0"},
            "pool": {"default_target": 0, "targets": {}, "max_total": 4},
            "guac": {"secret_key": GUAC_KEY, "base_url": "http://guac.test/guacamole/"},
            "portal": {"secret": "test-portal-secret"},
        }
    )


@pytest.fixture
def store(settings):
    settings.ensure_dirs()
    return Store(settings.db_path)


@pytest.fixture
def repo(settings):
    return ScenarioRepository(settings.scenarios_dir)


@pytest.fixture
def incus(settings):
    return FakeIncus(image_alias=settings.incus.image_alias)


@pytest.fixture
def driver(settings):
    """Guest transport double that pretends fault injection succeeded."""
    return NullDriver(settings, responses={"setup.ps1": "ONTRAK-SETUP-OK\n"})


@pytest.fixture
def manager(settings, store, repo, incus, driver):
    return SessionManager(settings, store, repo=repo, incus=incus, driver=driver)


@pytest.fixture
def built_template(manager, incus):
    """A ready template for net-dns-failure, as if infra/build-templates.sh ran."""
    manager.ensure_template("net-dns-failure")
    return incus


# --------------------------------------------------------------------------- #
# the portal
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_env(settings, store, incus):
    """A live portal with one student, one instructor and a built template.

    Yields ``(client, app)``. Shared by the portal and admin-panel suites so both
    exercise the same wiring the entrypoint does, rather than a re-declared app.

    The two accounts are rows without a credential, which is all any account is
    now: sign-in is Authentik's, and these suites mint the session the OIDC
    callback would (see `login`).
    """
    if TestClient is None:  # pragma: no cover - exercised only without fastapi
        pytest.skip("fastapi/httpx not installed")
    from ontrak.portal.app import create_app

    store.upsert_user("alice", "student", "Alice A")
    store.upsert_user("teacher", "instructor", "Teacher T")
    # A representative range: pointed at Authentik, so the login page renders the
    # real thing rather than the "not set up" state. The sign-in itself is done
    # with `login` below; test_oidc.py drives the IdP half.
    settings.portal.oidc_issuer = OIDC_ISSUER
    settings.portal.oidc_client_id = "ontrak"
    settings.portal.oidc_client_secret = "ontrak-client-secret"
    settings.portal.oidc_redirect_uri = ",".join(OIDC_CALLBACKS)
    settings.portal.oidc_instructor_group = "range-instructors"
    driver = NullDriver(
        settings, responses={"setup.ps1": "ONTRAK-SETUP-OK", "check.ps1": _pass_payload()}
    )
    app = create_app(settings, incus=incus, driver=driver)
    app.state.manager.ensure_template("net-dns-failure")
    with TestClient(app) as client:
        yield client, app


def _pass_payload() -> str:
    from ontrak.scenarios import JSON_BEGIN, JSON_END

    checks = [
        {"objective": o, "passed": True, "detail": "ok"}
        for o in ("restore-resolver", "resolve-intranet", "reach-service")
    ]
    import json

    return f"{JSON_BEGIN}{json.dumps({'checks': checks})}{JSON_END}"


def login(client, username: str = "alice"):
    """Sign in a seeded account.

    There is no password path to exercise any more, so this mints exactly what the
    OIDC callback issues: a signed session cookie for the account row. Authentik's
    own half of the flow is covered in tests/test_oidc.py.
    """
    from ontrak import auth

    app = client.app
    user = app.state.store.get_user(username)
    assert user is not None, f"no account row for {username!r}"
    client.cookies.set(
        auth.COOKIE_NAME,
        auth.sign_cookie(
            {"username": user["username"], "role": user["role"]},
            app.state.settings.portal.secret,
        ),
        # Scoped to the test host, so the cookie the logout route clears is the
        # same one it sent — an unscoped cookie here survives a working logout.
        domain="testserver.local",
        path="/",
    )
    # The audit trail records a sign-in, exactly as the OIDC callback does.
    app.state.store.log_event("login", user["username"])
    # And a page render is what mints the CSRF cookie the POSTs below need — the
    # same way a browser picks it up on the way in.
    client.get("/dashboard")
    return client


def csrf(client) -> str:
    return client.cookies.get("ontrak_csrf", "")


def as_student(client, username: str = "alice"):
    return login(client, username)


def as_instructor(client):
    return login(client, "teacher")


# --------------------------------------------------------------------------- #
# the portal, signed in through Authentik
# --------------------------------------------------------------------------- #
# The range's own values (scripts/cerulean-provision.py registers exactly these
# callbacks). Authentik itself is stood in for in tests/test_oidc.py.
OIDC_ISSUER = "https://auth.cerulean.innotel.us/application/o/ontrak/"
OIDC_CALLBACKS = (
    "https://ontrak.innotel.us/oidc/callback",
    "https://student.ontrak.innotel.us/oidc/callback",
    "https://admin.ontrak.innotel.us/oidc/callback",
)


@pytest.fixture
def sso_env(settings, store, incus):
    """A portal wired to Authentik: sign-in through the IdP, and nothing else.

    Yields ``(client, app)``. This is the shipped posture — SSO is the only way
    in — and it is the one a sign-in has to survive.
    """
    if TestClient is None:  # pragma: no cover - exercised only without fastapi
        pytest.skip("fastapi/httpx not installed")
    from ontrak.portal.app import create_app

    settings.portal.oidc_issuer = OIDC_ISSUER
    settings.portal.oidc_client_id = "ontrak"
    settings.portal.oidc_client_secret = "ontrak-client-secret"
    settings.portal.oidc_redirect_uri = ",".join(OIDC_CALLBACKS)
    settings.portal.oidc_instructor_group = "range-instructors"
    app = create_app(settings, incus=incus, driver=NullDriver(settings, responses={}))
    with TestClient(app) as client:
        yield client, app
