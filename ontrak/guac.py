"""Browser console access through Apache Guacamole.

Students never receive an RDP password. The portal hands them a Guacamole URL
whose ``data`` parameter is a signed + encrypted JSON payload (per the
``guacamole-auth-json`` extension), scoped to exactly one connection with a short
expiry. Guacamole verifies the signature, decrypts, and opens the session.

Wire format, exactly as the Guacamole manual specifies:

1. ``signature = HMAC-SHA256(secret, json)`` — 32 raw bytes.
2. Prepend it to the JSON: ``signature || json``.
3. AES-128-CBC encrypt with an all-zero IV (PKCS#7 padding).
4. Base64 the ciphertext; pass as ``data``.

:func:`decode_payload` implements the inverse so the format is testable without
a Guacamole instance.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import quote

from .models import Session
from .scenarios import Scenario

AES_BLOCK = 16
SIGNATURE_LEN = 32

# How long the gateway probe waits. Generous enough for a TLS handshake on a busy
# host, short enough that `ontrak doctor` does not hang on a name that never answers.
PROBE_TIMEOUT_SECONDS = 8.0


class GuacError(RuntimeError):
    """Raised when a payload cannot be built or read."""


def _aes(key: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

    return Cipher(algorithms.AES(key), modes.CBC(b"\x00" * AES_BLOCK))


def _pkcs7():
    from cryptography.hazmat.primitives import padding  # noqa: PLC0415

    return padding.PKCS7(AES_BLOCK * 8)


def encode_payload(payload: dict, key: bytes) -> str:
    """Sign, encrypt and base64 the payload the way guacamole-auth-json expects."""
    if len(key) != AES_BLOCK:
        raise GuacError(f"secret key must be {AES_BLOCK} bytes, got {len(key)}")
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key, raw, hashlib.sha256).digest()
    blob = signature + raw

    padder = _pkcs7().padder()
    padded = padder.update(blob) + padder.finalize()
    encryptor = _aes(key).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def decode_payload(data: str, key: bytes) -> dict:
    """Inverse of :func:`encode_payload`. Used by tests and for debugging links."""
    try:
        ciphertext = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise GuacError(f"data is not valid base64: {exc}") from exc
    decryptor = _aes(key).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = _pkcs7().unpadder()
    try:
        blob = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise GuacError(f"padding is invalid (wrong key?): {exc}") from exc
    if len(blob) <= SIGNATURE_LEN:
        raise GuacError("payload is truncated")
    signature, raw = blob[:SIGNATURE_LEN], blob[SIGNATURE_LEN:]
    expected = hmac.new(key, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise GuacError("signature does not verify (wrong key or tampered payload)")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuacError(f"payload is not valid JSON: {exc}") from exc


def rdp_parameters(settings, session: Session) -> dict:
    """RDP parameters for one student's VM."""
    guac = settings.guac
    params: dict[str, str] = {
        "hostname": session.host_ip,
        "port": str(settings.guest.rdp_port),
        "username": session.rdp_user or settings.guest.user,
        "password": session.rdp_password or settings.guest.password,
        # Windows 10/11 negotiate NLA; "any" lets guacd pick what the host offers
        # while still using the credentials above.
        "security": "any",
        "ignore-cert": "true",
        "resize-method": "display-update",
        "server-layout": guac.server_layout,
        "keyboard-layout": guac.keyboard_layout,
        "color-depth": "32",
        "clipboard-encoding": "UTF-8",
        "disable-audio": "true",
        # Training quality-of-life: keep the guest visually plain so screen
        # sharing and screenshots are readable, and keep transfer off by default.
        "enable-wallpaper": "false",
        "enable-theming": "false",
        "enable-font-smoothing": "true",
        "enable-desktop-composition": "false",
        "disable-bitmap-caching": "false",
        "enable-drive": "false",
        "create-drive-path": "true",
        "autoretry": "5",
    }
    if guac.recording:
        # Session recording for instructor review / incident exercises.
        params.update(
            {
                "recording-path": guac.recording_path,
                "recording-name": (
                    f"ontrak-{session.id}-{session.student}-{session.scenario_id}"
                    "-${GUAC_DATE}-${GUAC_TIME}"
                ),
                "create-recording-path": "true",
            }
        )
    return params


def ssh_parameters(settings, session: Session) -> dict:
    """SSH parameters for one student's Linux guest.

    Only used when the Linux images actually run an sshd (``guac.linux_ssh``); the
    default container driver runs commands through the Incus agent and has no sshd
    at all, which is why this is off by default rather than the natural choice.
    """
    guac = settings.guac
    params: dict[str, str] = {
        "hostname": session.host_ip,
        "port": str(settings.guest.ssh_port),
        # The *Linux* account, not the Windows one. `create_session` fills `rdp_user`
        # with `guest.user` (the training account on a Windows guest), while the
        # console transport a Linux template is built with sets the password for
        # `guest.linux_user` (root by default) and nothing else. Borrowing `rdp_user`
        # here pointed every Linux console at an account the image does not have, so
        # guacd's login was refused and the student got a console that never opened.
        "username": settings.guest.linux_user or "root",
        # The password the transport baked into the image: `randomize_credentials`
        # rotates a Windows local account only, so nothing else can move this.
        "password": settings.guest.password,
        "color-depth": "32",
        "font-size": "14",
        "clipboard-encoding": "UTF-8",
        "server-layout": guac.server_layout,
        "read-only": "false",
        "autoretry": "5",
    }
    if guac.recording:
        params.update(
            {
                "recording-path": guac.recording_path,
                "recording-name": (
                    f"ontrak-{session.id}-{session.student}-{session.scenario_id}"
                    "-${GUAC_DATE}-${GUAC_TIME}"
                ),
                "create-recording-path": "true",
            }
        )
    return params


def protocol_for(settings, scenario: Scenario | None) -> str:
    """The console protocol this scenario's guest can actually answer.

    A Windows VM brokers RDP. A Linux *container* does not: it runs no RDP server,
    so an RDP console for it is a page that reports the remote desktop server as
    unreachable, which says nothing about the scenario being broken. It answers
    SSH instead, but only where the image runs sshd — the default container driver
    works through the Incus agent and needs no daemon, so this returns "" (no
    browser console) unless ``guac.linux_ssh`` says otherwise. An empty answer is
    the honest one: the portal then explains the situation instead of embedding a
    console that cannot connect.
    """
    # `getattr`, because this runs while a student's session page is being built and
    # the caller only catches GuacError/ScenarioError: a scenario-like object that
    # lacks the property must not turn the page into a 500, and the safe reading of
    # "I cannot tell" is the pre-existing behaviour.
    if scenario is not None and getattr(scenario, "is_linux", False):
        return "ssh" if settings.guac.linux_ssh else ""
    return "rdp"


def build_payload(settings, session: Session, scenario: Scenario | None = None, now: float | None = None) -> dict:
    """Full Guacamole auth payload for one session."""
    if not session.host_ip:
        raise GuacError(f"session {session.id} has no host address yet")
    protocol = protocol_for(settings, scenario)
    if not protocol:
        raise GuacError(
            f"scenario {session.scenario_id} runs a Linux guest, which has no remote "
            "desktop: set guac.linux_ssh once the image runs sshd (guac.ssh_port), "
            "or hand the student a shell another way"
        )
    ttl_seconds = settings.guac.link_ttl_minutes * 60
    expires_ms = int(((now if now is not None else time.time()) + ttl_seconds) * 1000)
    title = scenario.title if scenario else session.scenario_id
    return {
        "username": session.student,
        "expires": expires_ms,
        "connections": {
            f"OnTrak #{session.id} - {title}": {
                "id": f"ontrak-session-{session.id}",
                "protocol": protocol,
                "parameters": (
                    ssh_parameters(settings, session) if protocol == "ssh"
                    else rdp_parameters(settings, session)
                ),
            }
        },
    }


def _post_form(url: str, fields: dict[str, str], timeout: float) -> tuple[int, str]:
    """POST an urlencoded form, exactly as the Guacamole webapp does for a token.

    Kept separate from :func:`probe_gateway` so the interpretation below can be tested
    against a stub, and so the one place that touches the network is this small.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - an operator-set URL
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def probe_gateway(settings, *, timeout: float = PROBE_TIMEOUT_SECONDS, post=None):
    """Ask the console gateway whether it accepts a link we signed. ``(state, detail)``.

    Googleable symptom this exists for: **the console iframe never opens**. The portal
    signs every link with ``guac.secret_key`` and Guacamole verifies it with its own
    ``JSON_SECRET_KEY``; when the two disagree — a stack recreated from an older `.env`,
    or a gateway deployed on its own with a freshly generated key — Guacamole answers
    *every* student with "Permission denied". The portal cannot see that: it hands over
    a correctly-signed link, the iframe is blank, and nothing in either log says why.

    States: ``ok`` (accepted), ``refused`` (reached, but rejected our key — a mismatch,
    or the JSON auth extension is off), ``unreachable`` (no answer: split-horizon DNS
    and a stopped gateway are both normal here, so this is a warning), and ``skipped``
    (no console configured at all).
    """
    base = (settings.guac.base_url or "").strip()
    if not base:
        return "skipped", "guac.base_url is not set, so this range has no console to check"
    try:
        key = settings.guac.secret_bytes()
    except Exception as exc:  # noqa: BLE001 - ConfigError's job is to be reported, not raised
        return "skipped", f"guac.secret_key is unusable, so there is no console link to test: {exc}"

    # A payload for a machine that does not have to exist: the gateway answers the
    # *signature* question at token time and only dials the VM when the console opens.
    payload = {
        "username": "ontrak-doctor",
        "expires": int((time.time() + 60) * 1000),
        "connections": {
            "OnTrak doctor": {
                "id": "ontrak-doctor",
                "protocol": "rdp",
                "parameters": {"hostname": "127.0.0.1", "port": str(settings.guest.rdp_port)},
            }
        },
    }
    url = base if base.endswith("/") else f"{base}/"
    url = f"{url}api/tokens"
    sender = post or _post_form
    try:
        status, body = sender(url, {"data": encode_payload(payload, key)}, timeout)
    except (OSError, ValueError) as exc:
        return "unreachable", f"could not reach the console gateway at {base}: {exc}"

    if status == 200 and "authToken" in body:
        return "ok", f"the console gateway at {base} accepted a payload signed with guac.secret_key"
    if status in (401, 403):
        return "refused", (
            f"the console gateway at {base} rejected a payload signed with guac.secret_key "
            f"(HTTP {status}). Its JSON_SECRET_KEY differs from this key, or its JSON auth "
            "extension is not enabled — so every student's console link is refused and the "
            "console never opens. Make JSON_SECRET_KEY equal ONTRAK_GUAC__SECRET_KEY "
            "(config: guac.secret_key) and recreate the gateway: "
            "`docker compose up -d --force-recreate guacamole`"
        )
    return "unreachable", f"the console gateway at {base} answered HTTP {status}: {body[:200]}"


def build_link(
    settings, session: Session, scenario: Scenario | None = None, now: float | None = None
) -> str:
    """Return the URL to embed in the portal's console iframe."""
    if not settings.guac.base_url:
        raise GuacError("guac.base_url is not configured")
    payload = build_payload(settings, session, scenario, now=now)
    data = encode_payload(payload, settings.guac.secret_bytes())
    base = settings.guac.base_url
    if not base.endswith("/"):
        base += "/"
    return f"{base}#/?data={quote(data, safe='')}"
