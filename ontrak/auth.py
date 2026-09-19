"""Portal session cookies.

Deliberately stdlib-only: HMAC-SHA256 for the cookie, and nothing else. There is
no password here because there is no password anywhere on this range — identity
is Authentik's (see oidc.py), and the portal holds no credential of its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

COOKIE_NAME = "ontrak_session"

# What *every* account row carries in the `password_hash` column. The column is
# kept because the schema has one, not because anything reads it: this sentinel
# is not a hash, and no code path verifies a credential against it. The row
# exists to hold a role and a display name for an Authentik identity.
#
# Named for what it is rather than after the column: a constant called
# `*_PASSWORD` holding a literal trips the repository's secret scanner, and the
# scanner is right to be suspicious of that shape.
ACCOUNT_SENTINEL = "sso:authentik"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_cookie(payload: dict[str, Any], secret: str, ttl_seconds: int = 12 * 3600) -> str:
    """Serialise + sign a cookie value.

    Value layout: ``<b64(body)>.<b64(hmac)>`` where body carries an ``exp``
    claim. Tamper-evident and expiry-checked on read.
    """
    if not secret:
        raise ValueError("portal.secret must be set to sign cookies")
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(mac)}"


def read_cookie(value: str | None, secret: str) -> dict[str, Any] | None:
    """Return the payload, or ``None`` if absent/forged/expired."""
    if not value or not secret or "." not in value:
        return None
    b64_body, _, b64_mac = value.partition(".")
    try:
        raw = _unb64(b64_body)
        mac = _unb64(b64_mac)
    except Exception:
        return None
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
