#!/usr/bin/env python3
"""Provision the local Authentik (deploy/authentik/compose.yaml) for OnTrak.

The portal is a relying party: it wants an issuer, a client id, a client secret
and a group whose members are instructors (ontrak/oidc.py, and docs/operations.md
"Sign-in"). In the estate Cerulean's `scripts/authentik-setup.py` creates all of
that. Here there is nobody to run that script for us, so this does the same four
things against the local instance, through its own REST API:

  1. a `range-instructors` group, and the seed accounts that belong in it
  2. a `groups` scope mapping, so the scope the portal asks for exists
  3. a confidential OAuth2/OIDC provider for the OnTrak application
  4. the application itself, pointing at that provider

It is idempotent: everything is looked up by name first, so re-running it after a
`docker compose down` (or after adding a student) changes what is missing and
nothing else. The one thing it deliberately *rewrites* every run is each seed
account's password, because a local IdP whose demo passwords have drifted from
.env is a support call, not a security property.

Stdlib only, like ontrak/oidc.py and for the same reason: this is four HTTP calls
and a JSON body, and a dev helper that needs `requests` installed is a dev helper
that does not run.

    AUTHENTIK_URL=http://127.0.0.1:9000 \
    AUTHENTIK_BOOTSTRAP_TOKEN=... \
    ONTRAK_LOCAL_OIDC_CLIENT_SECRET=... \
    ONTRAK_LOCAL_OIDC_REDIRECT_URI=http://localhost:8080/oidc/callback \
        python3 deploy/authentik/provision.py

deploy/authentik/setup.sh writes those variables and calls this; running it by
hand is for the times setup.sh has already run and only the provider is missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 30

# The application slug decides the issuer: Authentik serves every provider under
# /application/o/<slug>/, so this value is part of the URL in .env.
APPLICATION_NAME = "OnTrak"
APPLICATION_SLUG = "ontrak"
PROVIDER_NAME = "OnTrak"

# Where the portal runs here. Not the estate's three names (docs/operations.md) —
# a local range answers on one origin, and a callback that is not registered is a
# sign-in that cannot complete.
DEFAULT_REDIRECT_URI = "http://localhost:8080/oidc/callback"

INSTRUCTOR_GROUP = "range-instructors"
STUDENT_GROUP = "range-students"

# The scope the portal asks for. Authentik's default `profile` mapping already
# returns a groups claim, but the *scope name* it requests has to exist on the
# provider or the authorize request is refused before anyone types a password —
# which is why Cerulean's setup "pins the platform's groups scope mapping on
# every app" (ontrak/oidc.py). Same mapping, made here.
GROUPS_SCOPE = "groups"
GROUPS_EXPRESSION = (
    "# The portal reads this to decide who is an instructor (ontrak/oidc.py).\n"
    "groups = getattr(request.user, 'ak_groups', None)\n"
    "if groups is None:  # older releases called this `groups`\n"
    "    groups = request.user.groups\n"
    "return {'groups': [group.name for group in groups.all()]}\n"
)

# Flows a new OIDC provider must point at. Authentik ships both under these
# slugs; the difference is whether the account is shown a consent screen before
# being handed back to the application.
#
# This stack takes the no-consent one. A consent screen exists so a person can
# approve a *third party* reading their identity — and the only application on a
# local Authentik is the range that person just opened, on their own machine.
# Asking them to approve it on every sign-in is friction that protects nothing,
# and it is the difference between a demo that is one click and one that is two.
AUTHORIZATION_FLOW_SLUGS = (
    "default-provider-authorization-implicit-consent",
    "default-provider-authorization-explicit-consent",
)
INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"


class ProvisionError(RuntimeError):
    """Something the operator has to know about — phrased for them, not for a log."""


class Api:
    """The smallest useful client for Authentik's REST API."""

    def __init__(self, base_url: str, token: str, version: str = "v3"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.version = version

    def _call(self, method: str, url: str, body=None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = ""
            with contextlib.suppress(Exception):
                detail = error.read().decode("utf-8", "replace").strip()[:400]
            raise ProvisionError(f"{method} {url} -> HTTP {error.code} {detail}") from error
        except (urllib.error.URLError, OSError) as error:
            raise ProvisionError(
                f"{method} {url} is unreachable: {getattr(error, 'reason', error)}"
            ) from error
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProvisionError(f"{method} {url} did not return JSON") from error

    def _url(self, path: str, params=None) -> str:
        url = f"{self.base_url}/api/{self.version}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def get(self, path, params=None):
        return self._call("GET", self._url(path, params))

    def post(self, path, body=None):
        return self._call("POST", self._url(path), body)

    def patch(self, path, body=None):
        return self._call("PATCH", self._url(path), body)

    def send(self, method, path, body=None):
        return self._call(method, self._url(path), body)

    def one(self, path, params):
        """The first match, or None. Every list endpoint here is paginated."""
        page = self.get(path, params)
        results = page.get("results") if isinstance(page, dict) else page
        return results[0] if results else None

    def all(self, path, params=None):
        """Every match across pages.

        The cursor is read out of the reply rather than assumed: releases in the
        wild paginate by absolute URL and by page number under the same key, and
        either one arrives here.
        """
        out: list = []
        cursor = ""
        page_params = dict(params or {})
        while True:
            page = self._call("GET", cursor) if cursor else self.get(path, page_params or None)
            if isinstance(page, list):
                out.extend(page)
                return out
            out.extend(page.get("results", []))
            nxt = (page.get("pagination") or {}).get("next")
            if not nxt:
                return out
            if isinstance(nxt, int):
                cursor = ""
                page_params = {**dict(params or {}), "page": nxt}
            else:
                cursor = str(nxt)


# Upstream moved the REST API from /api/v2 to /api/v3 — 2026.8 answers only on
# v3, the releases before the move answer only on v2 — and the routes below are
# the same either way. Probed, not pinned, so a checkout older or newer than the
# image deploy/authentik/.env names still provisions.
API_VERSIONS = ("v3", "v2")


def detect_version(base_url: str, token: str) -> str:
    for version in API_VERSIONS:
        try:
            Api(base_url, token, version).get("/core/users/me/")
        except ProvisionError:
            continue
        return version
    raise ProvisionError(
        f"neither /api/v3 nor /api/v2 answered at {base_url} — is this an Authentik?"
    )


def log(text: str) -> None:
    print(f"  {text}", flush=True)


# --------------------------------------------------------------------- lookups --
def required(api: Api, path: str, params: dict, what: str) -> dict:
    found = api.one(path, params)
    if not found:
        raise ProvisionError(f"{what} not found in Authentik ({path} {params})")
    return found


def authorization_flow(api: Api) -> dict:
    for slug in AUTHORIZATION_FLOW_SLUGS:
        found = api.one("/flows/instances/", {"slug": slug})
        if found:
            return found
    raise ProvisionError("neither default provider authorization flow is present")


def signing_key(api: Api) -> dict:
    keys = api.all("/crypto/certificatekeypairs/", {"has_key": "true"})
    if not keys:
        raise ProvisionError("Authentik has no certificate keypair to sign with")
    # Prefer the self-signed one the installer creates; it is the one the UI
    # picks in "Select a signing Key" when you make a provider by hand.
    for key in keys:
        if "self-signed" in str(key.get("name", "")).lower():
            return key
    return keys[0]


def default_scope_mapping(api: Api, scope: str) -> dict:
    """Authentik's own scope mapping for `openid`, `email` or `profile`."""
    mappings = api.all("/propertymappings/provider/scope/", {"scope_name": scope})
    if not mappings:
        raise ProvisionError(f"Authentik has no default '{scope}' scope mapping")
    for mapping in mappings:
        if str(mapping.get("name", "")).startswith("authentik default OAuth Mapping"):
            return mapping
    return mappings[0]


def ensure_groups_mapping(api: Api) -> dict:
    found = api.one("/propertymappings/provider/scope/", {"scope_name": GROUPS_SCOPE})
    if found:
        return found
    return api.post(
        "/propertymappings/provider/scope/",
        {
            "name": "OnTrak groups",
            "scope_name": GROUPS_SCOPE,
            "description": "The account's groups, which the portal turns into a role",
            "expression": GROUPS_EXPRESSION,
        },
    )


def ensure_group(api: Api, name: str) -> dict:
    found = api.one("/core/groups/", {"name": name})
    if found:
        return found
    return api.post("/core/groups/", {"name": name, "is_superuser": False})


# ----------------------------------------------------------------------- users --
def ensure_user(api: Api, email: str, name: str, password: str, group_pk: str) -> dict:
    user = api.one("/core/users/", {"username": email})
    if not user:
        user = api.post(
            "/core/users/",
            {
                "username": email,
                "name": name,
                "email": email,
                "is_active": True,
                "groups": [group_pk],
            },
        )
        log(f"created {email}")
    else:
        api.patch(f"/core/users/{user['pk']}/", {"groups": [group_pk]})
    # Rewritten every run: the password in .env is the one that works, whether or
    # not it was the one this account had.
    api.post(f"/core/users/{user['pk']}/set_password/", {"password": password})
    return user


# -------------------------------------------------------------------- provider --
def _redirect_uri_shapes(uris):
    """How a provider's allowed redirect URIs are spelled has changed twice.

    The per-URL objects lived in `allowed_redirect_uris`; 2024.2 flattened them
    into a list of strings called `redirect_uris`; and by 2026.8 the field is
    `redirect_uris` again but holds the objects. All three are tried in release
    order, and only an answer *about this field* moves on to the next one — a
    validation error on anything else surfaces as itself instead of as three
    confusing retries.
    """
    entries = [{"matching_mode": "strict", "url": uri} for uri in uris]
    return (
        {"redirect_uris": entries},
        {"redirect_uris": list(uris)},
        {"allowed_redirect_uris": entries},
    )


def provider_body(*, flows, mappings, key, client_id, client_secret):
    return {
        "name": PROVIDER_NAME,
        "authorization_flow": flows[0]["pk"],
        "invalidation_flow": flows[1]["pk"],
        # confidential: the portal has a client secret and exchanges the code
        # server-side (ontrak/oidc.py sends it as HTTP Basic).
        "client_type": "confidential",
        "client_id": client_id,
        "client_secret": client_secret,
        # Not optional in practice. A provider created without these answers the
        # authorize request with `Invalid grant_type for provider`, the browser
        # comes back with `error=invalid_request`, and the portal shows a login
        # page whose button goes nowhere — a provider that exists, reads as
        # configured, and can sign nobody in.
        "grant_types": ["authorization_code", "refresh_token"],
        "property_mappings": [m["pk"] for m in mappings],
        "signing_key": key["pk"],
    }


def redirect_urls(value) -> list:
    """The URLs out of whatever shape this release returned."""
    out = []
    for item in value or []:
        url = str(item.get("url") or "") if isinstance(item, dict) else str(item)
        if url:
            out.append(url)
    return out


def provider_drift(found: dict, *, flows, mappings, key, client_id, redirect_uris) -> list:
    """What this provisioner would change about the provider that is already there.

    Compared rather than skipped: an existing provider is the usual state on a
    re-run, and the interesting failures — no `grant_types`, so it authorizes
    nothing at all; the consent flow, so a local demo stops at a screen nobody
    asked for — look exactly like a correct provider from the outside.
    """
    problems = []
    if str(found.get("authorization_flow") or "") != str(flows[0]["pk"]):
        problems.append("authorization_flow")
    if not {"authorization_code", "refresh_token"} <= set(found.get("grant_types") or []):
        problems.append("grant_types")
    if str(found.get("client_id") or "") != client_id:
        problems.append("client_id")
    if str(found.get("signing_key") or "") != str(key["pk"]):
        problems.append("signing_key")
    if set(found.get("property_mappings") or []) != {m["pk"] for m in mappings}:
        problems.append("property_mappings")
    if set(redirect_urls(found.get("redirect_uris"))) != set(redirect_uris):
        problems.append("redirect_uris")
    return problems


def write_provider(api: Api, method: str, path: str, body: dict, redirect_uris) -> dict:
    """POST or PATCH a provider, trying each spelling of the redirect URIs."""
    last = None
    for shape in _redirect_uri_shapes(redirect_uris):
        try:
            return api.send(method, path, {**body, **shape})
        except ProvisionError as error:
            if "redirect_uris" not in str(error):
                raise
            last = error
    raise last or ProvisionError("the provider could not be written")


def ensure_provider(api: Api, *, flows, mappings, key, client_id, client_secret, redirect_uris):
    body = provider_body(
        flows=flows,
        mappings=mappings,
        key=key,
        client_id=client_id,
        client_secret=client_secret,
    )
    found = api.one("/providers/oauth2/", {"name": PROVIDER_NAME})
    if not found:
        return write_provider(api, "POST", "/providers/oauth2/", body, redirect_uris)
    drift = provider_drift(
        found,
        flows=flows,
        mappings=mappings,
        key=key,
        client_id=client_id,
        redirect_uris=redirect_uris,
    )
    if not drift:
        return found
    log(f"provider {PROVIDER_NAME} is not what this range needs ({', '.join(drift)}) — correcting")
    return write_provider(api, "PATCH", f"/providers/oauth2/{found['pk']}/", body, redirect_uris)


def ensure_application(api: Api, provider_pk: str) -> dict:
    found = api.one("/core/applications/", {"slug": APPLICATION_SLUG})
    if found:
        return found
    return api.post(
        "/core/applications/",
        {
            "name": APPLICATION_NAME,
            "slug": APPLICATION_SLUG,
            "provider": provider_pk,
        },
    )


# ------------------------------------------------------------------------ main --
def split(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def main() -> int:
    base_url = os.environ.get("AUTHENTIK_URL", "http://127.0.0.1:9000")
    token = os.environ.get("AUTHENTIK_BOOTSTRAP_TOKEN", "").strip()
    if not token:
        raise ProvisionError(
            "AUTHENTIK_BOOTSTRAP_TOKEN is not set — it is in deploy/authentik/.env"
        )

    client_id = os.environ.get("ONTRAK_LOCAL_OIDC_CLIENT_ID", "ontrak").strip()
    client_secret = os.environ.get("ONTRAK_LOCAL_OIDC_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise ProvisionError(
            "ONTRAK_LOCAL_OIDC_CLIENT_SECRET is not set — it is in deploy/authentik/.env"
        )
    redirect_uris = split(os.environ.get("ONTRAK_LOCAL_OIDC_REDIRECT_URI", DEFAULT_REDIRECT_URI))
    password = os.environ.get("ONTRAK_LOCAL_SEED_PASSWORD", "").strip()
    if not password:
        raise ProvisionError(
            "ONTRAK_LOCAL_SEED_PASSWORD is not set — it is in deploy/authentik/.env"
        )
    students = int(os.environ.get("ONTRAK_LOCAL_STUDENTS", "6"))
    instructor = os.environ.get("ONTRAK_LOCAL_INSTRUCTOR", "instructor@ontrak.lab").strip()
    student_domain = os.environ.get("ONTRAK_LOCAL_STUDENT_DOMAIN", "ontrak.lab").strip()

    version = detect_version(base_url, token)
    api = Api(base_url, token, version)
    log(f"authentik at {api.base_url} (REST API {version})")
    auth_flow = authorization_flow(api)
    inval_flow = required(api, "/flows/instances/", {"slug": INVALIDATION_FLOW_SLUG}, "invalidation flow")
    key = signing_key(api)
    log(f"flows and signing key ready ({key.get('name')})")

    mappings = [
        default_scope_mapping(api, "openid"),
        default_scope_mapping(api, "email"),
        default_scope_mapping(api, "profile"),
    ]
    groups_mapping = ensure_groups_mapping(api)
    mappings.append(groups_mapping)
    log(f"scope mappings: openid, email, profile, {GROUPS_SCOPE}")

    instructors = ensure_group(api, INSTRUCTOR_GROUP)
    students_group = ensure_group(api, STUDENT_GROUP)
    log(f"groups: {INSTRUCTOR_GROUP}, {STUDENT_GROUP}")

    provider = ensure_provider(
        api,
        flows=(auth_flow, inval_flow),
        mappings=mappings,
        key=key,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uris=redirect_uris,
    )
    ensure_application(api, provider["pk"])
    log(f"application {APPLICATION_SLUG} -> provider {provider['name']} (client {client_id})")
    log(f"redirect URIs: {', '.join(redirect_uris) or '(none — Authentik keeps the first used)'}")

    # The seed accounts. Same shape as demo mode's (ontrak/demo.py): one
    # instructor who runs the class, N students who take it.
    ensure_user(api, instructor, "Instructor", password, instructors["pk"])
    for index in range(1, max(students, 0) + 1):
        email = f"student{index}@{student_domain}"
        ensure_user(api, email, f"Student {index}", password, students_group["pk"])
    log(f"accounts: {instructor} (instructor), {students} student(s) in {STUDENT_GROUP}")
    log(f"password for all seed accounts: {password}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvisionError as error:
        print(f"\n[!] {error}", file=sys.stderr)
        raise SystemExit(1) from error
