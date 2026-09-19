"""Authentik (OIDC) sign-in for the portal.

OnTrak is a **relying party**, not an identity provider. An instructor and a
student are Authentik accounts; the portal only decides what a signed-in account
may do, and it keeps no password of its own — there is no local login to fall
back to. That is the platform's posture — see Cerulean's docs/authentik-setup.md
— and the same one Distro's control plane implements, which this deliberately
mirrors (callback selection and the `misconfigured` state are its shapes).

Deliberately stdlib-only, like auth.py: discovery, the authorize redirect, the
code exchange and userinfo are four HTTP calls and two JSON documents.

Identity is read from the **userinfo** endpoint the discovery document names,
reached with the access token Authentik just issued — over TLS, and only usable
against the issuer that minted it. Verifying the id_token's signature against
JWKS would be a second code path for the same answer.

Configuration lives in PortalConfig (see docs/operations.md "Sign-in"); every
one of the four `oidc_*` values must be set for the flow to be enabled.
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Holds the signed `state` between the authorize redirect and the callback. Its
# own cookie rather than a reuse of the session cookie: nothing here is a
# session yet, and a failed sign-in must not touch the one that exists.
OIDC_COOKIE = "ontrak_oidc"

# `groups` is what makes Authentik the authority on who is an instructor. The
# provider must carry the platform's groups scope mapping for this to return
# anything (Cerulean's scripts/authentik-setup.py pins it on every app).
SCOPE = "openid email profile groups"

# The state cookie is short-lived: a sign-in that took longer than this is a
# sign-in the browser abandoned, and replaying it should not work.
STATE_TTL_SECONDS = 600

DISCOVERY_TTL_SECONDS = 300
TIMEOUT_SECONDS = 15

_discovery_cache: dict[str, Any] = {"issuer": "", "at": 0.0, "meta": {}}


class OidcError(RuntimeError):
    """The flow cannot continue — phrased for an operator, since it is shown."""


# ---------------------------------------------------------------- configuration --
def redirect_uris(portal) -> list[str]:
    """Every callback registered on the provider, canonical first."""
    raw = str(getattr(portal, "oidc_redirect_uri", "") or "")
    return [uri.strip() for uri in raw.split(",") if uri.strip()]


def enabled(portal) -> bool:
    """All four values present, or SSO is off. Never partially on."""
    return bool(
        str(getattr(portal, "oidc_issuer", "") or "").strip()
        and str(getattr(portal, "oidc_client_id", "") or "").strip()
        and str(getattr(portal, "oidc_client_secret", "") or "").strip()
        and redirect_uris(portal)
    )


def public_config(portal) -> dict:
    """What the login page may know. No secret is ever part of this."""
    issuer = str(getattr(portal, "oidc_issuer", "") or "").strip()
    host = ""
    if issuer:
        try:
            host = urllib.parse.urlsplit(issuer).hostname or issuer
        except ValueError:
            host = issuer
    return {
        "enabled": enabled(portal),
        "provider": "Authentik",
        "issuerHost": host,
        # SSO is the only way in, so an unconfigured range has no way in at all:
        # the page must say so loudly rather than render a button that leads
        # nowhere.
        "misconfigured": not enabled(portal),
    }


# ------------------------------------------------------------------- callbacks --
def _first_header(headers, name: str) -> str:
    try:
        raw = headers.get(name)
    except AttributeError:
        raw = None
    return str(raw or "").split(",")[0].strip()


def _callback_key(value: str) -> str:
    """Compare callbacks by scheme, host and path (case and trailing slash aside)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.rstrip("/").lower()
    if not parts.hostname:
        return raw.rstrip("/").lower()
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def callback_for(portal, headers) -> str:
    """The callback URL for one request.

    The flow returns the browser to the redirect_uri it started with, and the
    state cookie is host-only — so a fixed callback only works on the origin it
    names. The range answers on three names (base, `student.`, `admin.`), so the
    request's own origin is used when the operator listed it among the
    registered callbacks, and the canonical (first) entry otherwise.

    The list is what makes this safe: a forged `Host`/`X-Forwarded-Host` can only
    name an origin already registered with Authentik, and an unlisted one falls
    back to the canonical URL instead of being honoured.
    """
    uris = redirect_uris(portal)
    if not uris:
        return ""
    canonical = uris[0]
    host = _first_header(headers, "x-forwarded-host") or _first_header(headers, "host")
    if not host:
        return canonical
    path = urllib.parse.urlsplit(canonical).path or "/oidc/callback"
    # The edge says which scheme the browser used; a direct LAN hit says nothing,
    # so plain HTTP is tried after TLS. Whichever is registered wins, which keeps
    # the answer a matter of the operator's list and not of a guess here.
    forwarded = _first_header(headers, "x-forwarded-proto").lower()
    schemes = list(dict.fromkeys([s for s in (forwarded, "https", "http") if s]))
    for scheme in schemes:
        candidate = f"{scheme}://{host}{path}"
        key = _callback_key(candidate)
        for uri in uris:
            if _callback_key(uri) == key:
                return uri
    return canonical


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ------------------------------------------------------------------------ HTTP --
def fetch_json(url: str, headers: dict | None = None) -> Any:
    """GET a JSON document. Module-level so tests can replace one call site."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise OidcError(f"{url} returned HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise OidcError(f"{url} is unreachable: {getattr(error, 'reason', error)}") from error
    except json.JSONDecodeError as error:
        raise OidcError(f"{url} did not return JSON") from error


def post_form(url: str, form: dict, headers: dict | None = None) -> Any:
    """POST an urlencoded form and read the JSON reply."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        with contextlib.suppress(Exception):
            detail = error.read().decode(errors="replace").strip()[:160]
        raise OidcError(f"{url} returned HTTP {error.code}{': ' + detail if detail else ''}") from error
    except (urllib.error.URLError, OSError) as error:
        raise OidcError(f"{url} is unreachable: {getattr(error, 'reason', error)}") from error
    except json.JSONDecodeError as error:
        raise OidcError(f"{url} did not return JSON") from error


# ------------------------------------------------------------------- the flow --
def discovery(portal, *, now: float | None = None) -> dict:
    """The issuer's discovery document, cached briefly.

    Cached because every sign-in would otherwise spend two round-trips before the
    browser has even left — but only briefly, so a re-provisioned provider is
    picked up without a restart.
    """
    issuer = str(getattr(portal, "oidc_issuer", "") or "").strip().rstrip("/")
    if not issuer:
        raise OidcError("SSO is not configured on this range")
    at = time.monotonic() if now is None else now
    cached = _discovery_cache
    if cached["issuer"] == issuer and cached["meta"] and at - cached["at"] < DISCOVERY_TTL_SECONDS:
        return cached["meta"]
    url = issuer if issuer.endswith("/.well-known/openid-configuration") else f"{issuer}/.well-known/openid-configuration"
    meta = fetch_json(url)
    if not isinstance(meta, dict):
        raise OidcError(f"{url} did not return a discovery document")
    cached.update({"issuer": issuer, "at": at, "meta": meta})
    return meta


def _endpoint(portal, name: str) -> str:
    meta = discovery(portal)
    endpoint = str(meta.get(name) or "").strip()
    if not endpoint:
        raise OidcError(f"the issuer's discovery document has no {name}")
    return endpoint


def authorize_url(portal, state: str, redirect_uri: str) -> str:
    """Where to send the browser to sign in."""
    url = urllib.parse.urlsplit(_endpoint(portal, "authorization_endpoint"))
    query = urllib.parse.parse_qsl(url.query)
    query += [
        ("client_id", str(getattr(portal, "oidc_client_id", "") or "")),
        ("redirect_uri", redirect_uri),
        ("response_type", "code"),
        ("scope", SCOPE),
        ("state", state),
    ]
    return urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query)))


def exchange_code(portal, code: str, redirect_uri: str) -> str:
    """Trade the authorization code for an access token."""
    client_id = str(getattr(portal, "oidc_client_id", "") or "")
    secret = str(getattr(portal, "oidc_client_secret", "") or "")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    tokens = post_form(
        _endpoint(portal, "token_endpoint"),
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        {"Authorization": f"Basic {basic}"},
    )
    access_token = str((tokens or {}).get("access_token") or "")
    if not access_token:
        raise OidcError("the token exchange returned no access_token")
    return access_token


def userinfo(portal, access_token: str) -> dict:
    claims = fetch_json(
        _endpoint(portal, "userinfo_endpoint"), {"Authorization": f"Bearer {access_token}"}
    )
    if not isinstance(claims, dict):
        raise OidcError("userinfo did not return a claims document")
    return claims


# ------------------------------------------------------------ claims -> portal --
def groups_of(claims: dict) -> list[str]:
    raw = claims.get("groups")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(group) for group in raw]
    return []


def username_for(claims: dict) -> str:
    """The portal's key for this account: the Authentik email, lowercased.

    The email rather than `preferred_username` or `sub`: `sub` is
    `hashed_user_id` on this platform (an opaque digest), and the email is the
    identity Authentik, Cerulean and every other relying party already agree on.
    An account Authentik authenticates but withholds an email for is refused
    rather than given a made-up name — the instructor fixes it in Authentik.
    """
    for key in ("email", "preferred_username"):
        value = str(claims.get(key) or "").strip().lower()
        if value and " " not in value:
            return value
    return ""


def display_name_for(claims: dict, username: str) -> str:
    for key in ("name", "preferred_username"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return username


def role_for(claims: dict, portal) -> str:
    """`instructor` or `student`.

    Authentik is the authority, and it is re-read on every sign-in: adding
    someone to the instructor group grants the class view, removing them takes it
    away, with no local edit and no second place to keep in step.
    """
    groups = groups_of(claims)
    wanted = str(getattr(portal, "oidc_instructor_group", "") or "").strip()
    if wanted and wanted in groups:
        return "instructor"
    # A platform admin (Authentik superuser) is the estate's break-glass and owns
    # the range, so they are never locked out of the class view.
    if claims.get("is_superuser") is True:
        return "instructor"
    return "student"


def entitled(portal, claims: dict) -> bool:
    """Whether this account may enter the range at all.

    The required group is absolute on purpose: a range gated to a class cohort
    stays gated for everyone, and the only door that remains is the operator's —
    Authentik group membership.
    """
    wanted = str(getattr(portal, "oidc_required_group", "") or "").strip()
    if not wanted:
        return True
    return wanted in groups_of(claims)
