from __future__ import annotations

import re

import pytest
import yaml

from ontrak.config import ConfigError, load_settings, require_secrets

from .conftest import GUAC_KEY, SCENARIOS_DIR


def test_defaults_load_and_resolve_paths(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text("guest:\n  user: alice\n")
    settings = load_settings(path=config, environ={})
    assert settings.guest.user == "alice"
    assert settings.incus.image_alias == "ontrak-win-base"  # from the repo defaults
    assert settings.scenarios_dir == SCENARIOS_DIR
    assert settings.db_path.name == "ontrak.sqlite3"


def test_section_key_env_override(settings):
    settings = load_settings(
        path=None,
        environ={
            "ONTRAK_GUEST__PASSWORD": "from-env",
            "ONTRAK_SESSION__TTL_MINUTES": "15",
            "ONTRAK_POOL__TARGETS": "{net-dns-failure: 30}",
            "IGNORED_KEY": "nope",
            "ONTRAK_NOT_SCOPED": "nope",
        },
    )
    assert settings.guest.password == "from-env"
    assert settings.session.ttl_minutes == 15
    assert settings.pool.target_for("net-dns-failure") == 30
    assert settings.pool.target_for("other") == settings.pool.default_target


def test_the_shipped_env_template_is_loadable():
    """`.env` is generated from `.env.example`, so the app must be able to read it.

    The first run copies the template into `.env` and both the container stack and
    the host CLI read that file. A key in it that `load_settings` does not know is
    therefore not a typo warning — it is a first run that fails at boot, which is
    how `ONTRAK_GUAC__PUBLIC_PORT` (read by compose, rejected by GuacConfig) got
    caught: `docker compose up` was fine, the portal container was fine, and every
    host command died on a setting the documentation told operators to set. That
    key is gone now — the console is a path on the stack's single published port,
    not a service with a port of its own — and this test is what keeps its
    replacement honest.
    """
    from pathlib import Path

    template = Path(__file__).resolve().parent.parent / ".env.example"
    # Values may legitimately be blank (the secrets are filled in by the first
    # run), but every key must be one the app can read.
    listed = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", template.read_text(), re.MULTILINE))
    for secret in ("ONTRAK_GUAC__SECRET_KEY", "ONTRAK_PORTAL__SECRET"):
        assert secret in listed, f"the template stopped listing {secret}"

    filled = {key: value for key, value in listed.items() if value.strip()}
    settings = load_settings(path=None, environ=dict(filled))

    # Spot-check that the values arrived rather than being swallowed. The console
    # address is the one the stack's gateway is configured against: a template
    # that listed it under a name the app could not read would send students to a
    # path that answers nothing.
    assert settings.guac.base_url.endswith("/guacamole/")
    assert settings.guac.recording is False
    assert settings.session.ttl_minutes == 90
    assert settings.pool.targets == {}


def test_bad_key_is_rejected(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text("guest:\n  nope: 1\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(path=config, environ={})


def test_a_spent_env_key_names_the_variable_that_carries_it():
    """A leftover `ONTRAK_...` in `.env` must point at the line to delete.

    This is the `ONTRAK_GUAC__PUBLIC_PORT` failure exactly: the value sat in the
    operator's `.env`, every command that loaded config died, and the error named
    `GuacConfig` and a bare field — nothing said which variable, or which file, to
    go and fix. `.env` is exported wholesale, so one spent line is not a stray
    value the loader can ignore: naming it is the difference between a self-
    diagnosing upgrade and a checkout that looks broken for no visible reason.
    """
    with pytest.raises(ConfigError, match=r"unknown setting ONTRAK_GUAC__PUBLIC_PORT"):
        load_settings(path=None, environ={"ONTRAK_GUAC__PUBLIC_PORT": "8081"})


def test_a_section_the_app_does_not_model_is_still_ignored():
    """`ONTRAK_FOO__BAR` addresses nothing the app knows, so it stays ignored.

    Only the app's own sections are held to the strict check. Other tooling shares
    this environment — a name that was never a setting must not become a boot
    failure, or the check would be worse than the problem it reports.
    """
    settings = load_settings(path=None, environ={"ONTRAK_FOO__BAR": "1"})
    assert settings.guac.base_url


def test_absolute_and_relative_state_paths(tmp_path):
    settings = load_settings(overrides={"paths": {"state": str(tmp_path / "s")}}, environ={})
    assert settings.state_dir == tmp_path / "s"
    settings.ensure_dirs()
    assert settings.state_dir.is_dir()


def test_guac_secret_parsing():
    settings = load_settings(overrides={"guac": {"secret_key": GUAC_KEY}}, environ={})
    assert settings.guac.secret_bytes() == bytes.fromhex(GUAC_KEY)


def test_guac_secret_must_be_32_hex():
    settings = load_settings(overrides={"guac": {"secret_key": "short"}}, environ={})
    with pytest.raises(ConfigError, match="32 hex"):
        settings.guac.secret_bytes()


def test_require_secrets_reports_each_problem():
    settings = load_settings(
        overrides={"guest": {"password": ""}, "portal": {"secret": ""}, "guac": {"secret_key": ""}},
        environ={},
    )
    problems = require_secrets(settings)
    assert len(problems) == 3
    assert any("guest.password" in p for p in problems)
    assert any("portal.secret" in p for p in problems)


def test_instance_name_helpers():
    settings = load_settings(environ={})
    incus = settings.incus
    assert incus.template_name("Net DNS Failure") == f"{incus.template_prefix}-net-dns-failure"
    assert incus.pool_name("sw-app-crash", 3) == f"{incus.pool_prefix}-sw-app-crash-3"
    assert incus.session_name("hw_driver_device", 12).startswith(f"{incus.session_prefix}-hw-driver-device-")


def test_shipped_yaml_is_parseable():
    from pathlib import Path

    from ontrak.config import DEFAULT_CONFIG

    data = yaml.safe_load(Path(DEFAULT_CONFIG).read_text())
    for section in ("incus", "guest", "session", "pool", "guac", "portal", "paths"):
        assert section in data, f"{section} missing from config/ontrak.yaml"
