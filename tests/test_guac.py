from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import time
from urllib.parse import parse_qs, urlparse

import pytest

from ontrak import guac
from ontrak.models import Session

from .conftest import GUAC_KEY

KEY = bytes.fromhex(GUAC_KEY)


def make_session(**overrides) -> Session:
    data = {
        "id": 42,
        "student": "alice",
        "scenario_id": "net-dns-failure",
        "host_ip": "10.20.0.150",
        "rdp_user": "student",
        "rdp_password": "TrainMe!12345",
    }
    data.update(overrides)
    return Session(**data)


def test_payload_round_trips():
    payload = {"username": "alice", "expires": 1234567890123, "connections": {"c": {"protocol": "rdp"}}}
    data = guac.encode_payload(payload, KEY)
    assert guac.decode_payload(data, KEY) == payload


def test_wrong_key_fails_the_signature():
    data = guac.encode_payload({"username": "alice"}, KEY)
    with pytest.raises(guac.GuacError):
        guac.decode_payload(data, bytes.fromhex("ff" * 16))


def test_tampering_is_detected():
    data = guac.encode_payload({"username": "alice"}, KEY)
    raw = bytearray(base64.b64decode(data))
    raw[40] ^= 0x01  # flip a bit in the ciphertext
    with pytest.raises(guac.GuacError):
        guac.decode_payload(base64.b64encode(bytes(raw)).decode(), KEY)


def test_key_length_is_validated():
    with pytest.raises(guac.GuacError, match="16 bytes"):
        guac.encode_payload({"username": "alice"}, b"tooshort")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not available")
def test_matches_openssl_reference_implementation():
    """Cross-check against OpenSSL: HMAC-SHA256, prepend, AES-128-CBC, zero IV, PKCS#7.

    This is the format guacamole-auth-json verifies, computed here by an
    independent implementation (the openssl CLI) rather than by our own code.
    """
    payload = {
        "username": "test",
        "expires": 1446323765000,
        "connections": {"My Connection": {"protocol": "rdp", "parameters": {"hostname": "10.10.209.63"}}},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-mac", "HMAC", "-macopt", f"hexkey:{KEY.hex()}", "-binary"],
        input=raw,
        capture_output=True,
        check=True,
    ).stdout
    assert len(signature) == 32

    encrypted = subprocess.run(
        [
            "openssl", "enc", "-aes-128-cbc",
            "-K", KEY.hex(),
            "-iv", "00" * 16,
            "-nosalt",
            "-base64",
        ],
        input=signature + raw,
        capture_output=True,
        check=True,
    ).stdout
    expected = b"".join(encrypted.split()).decode("ascii")

    assert guac.encode_payload(payload, KEY) == expected


def test_wire_format_is_signature_then_json_before_encryption():
    """Pin the layout the Guacamole manual specifies: signature || json, AES-CBC, zero IV."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    payload = {"username": "test"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = base64.b64decode(guac.encode_payload(payload, KEY))

    decryptor = Cipher(algorithms.AES(KEY), modes.CBC(b"\x00" * 16)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    blob = unpadder.update(padded) + unpadder.finalize()

    assert blob[:32] == hmac.new(KEY, raw, hashlib.sha256).digest()
    assert blob[32:] == raw


def test_rdp_parameters_use_the_session_credentials(settings):
    session = make_session()
    params = guac.rdp_parameters(settings, session)
    assert params["hostname"] == "10.20.0.150"
    assert params["username"] == "student"
    assert params["password"] == "TrainMe!12345"
    assert params["ignore-cert"] == "true"
    assert params["port"] == str(settings.guest.rdp_port)


def test_recording_is_opt_in(settings):
    session = make_session()
    settings.guac.recording = False
    assert "recording-path" not in guac.rdp_parameters(settings, session)
    settings.guac.recording = True
    params = guac.rdp_parameters(settings, session)
    assert params["recording-path"] == settings.guac.recording_path
    assert str(session.id) in params["recording-name"]


def test_link_is_a_browser_url_with_an_encrypted_payload(settings, repo):
    session = make_session()
    scenario = repo.get("net-dns-failure")
    now = time.time()
    link = guac.build_link(settings, session, scenario, now=now)

    parsed = urlparse(link)
    assert parsed.scheme == "http" and parsed.netloc == "guac.test"
    assert parsed.path == "/guacamole/"
    data = parse_qs(parsed.fragment.lstrip("/?"))["data"][0]

    payload = guac.decode_payload(data, KEY)
    assert payload["username"] == "alice"
    expected_expiry = int((now + settings.guac.link_ttl_minutes * 60) * 1000)
    assert abs(payload["expires"] - expected_expiry) <= 1000
    connections = payload["connections"]
    assert len(connections) == 1
    (name, connection), = connections.items()
    assert scenario.title in name
    assert connection["parameters"]["hostname"] == "10.20.0.150"
    # The payload is encrypted, so the RDP password is not readable in the URL.
    assert "TrainMe" not in link


def test_link_requires_an_address(settings):
    with pytest.raises(guac.GuacError, match="no host address"):
        guac.build_link(settings, make_session(host_ip=""), None)


def test_link_requires_a_configured_gateway(settings):
    settings.guac.base_url = ""
    with pytest.raises(guac.GuacError, match="base_url"):
        guac.build_link(settings, make_session(), None)


@pytest.mark.skipif(
    not __import__("os").environ.get("ONTRAK_GUAC_INTEROP_URL"),
    reason="set ONTRAK_GUAC_INTEROP_URL to POST the payload to a live Guacamole",
)
def test_live_guacamole_accepts_the_payload(settings):  # pragma: no cover - needs a gateway
    import urllib.parse
    import urllib.request

    url = __import__("os").environ["ONTRAK_GUAC_INTEROP_URL"]
    data = guac.encode_payload(guac.build_payload(settings, make_session()), KEY)
    body = urllib.parse.urlencode({"data": data}).encode()
    with urllib.request.urlopen(url.rstrip("/") + "/api/tokens", data=body, timeout=10) as response:
        assert response.status == 200


class _Scenario:
    """The fields the protocol decision reads."""

    def __init__(self, platform: str, scenario_id: str = "s") -> None:
        self.platform = platform
        self.id = scenario_id
        self.title = f"{platform} scenario"

    @property
    def is_linux(self) -> bool:
        return self.platform == "linux"


def test_a_windows_guest_gets_rdp(settings):
    scenario = _Scenario("windows")
    assert guac.protocol_for(settings, scenario) == "rdp"
    payload = guac.build_payload(settings, make_session(), scenario)
    connection = next(iter(payload["connections"].values()))
    assert connection["protocol"] == "rdp"
    assert connection["parameters"]["port"] == str(settings.guest.rdp_port)


def test_a_linux_container_gets_no_browser_console_by_default(settings):
    # The reported failure: the console iframe was an RDP session pointed at a
    # Linux container, which has no RDP server, so every container scenario showed
    # "the remote desktop server is currently unreachable". With no sshd in the
    # image either (the default Incus-agent driver), the honest answer is no
    # console at all — the portal says so instead of embedding one that cannot
    # connect.
    scenario = _Scenario("linux")
    assert guac.protocol_for(settings, scenario) == ""
    with pytest.raises(guac.GuacError, match="no remote desktop"):
        guac.build_payload(settings, make_session(), scenario)


def test_a_linux_guest_gets_ssh_when_the_image_runs_sshd(settings):
    settings.guac.linux_ssh = True
    scenario = _Scenario("linux")
    assert guac.protocol_for(settings, scenario) == "ssh"
    payload = guac.build_payload(settings, make_session(rdp_user="", rdp_password=""), scenario)
    connection = next(iter(payload["connections"].values()))
    assert connection["protocol"] == "ssh"
    assert connection["parameters"]["port"] == str(settings.guest.ssh_port)
    assert connection["parameters"]["username"] == settings.guest.linux_user
    # An SSH console has no desktop to resize; asking for one would be noise.
    assert "resize-method" not in connection["parameters"]


def test_a_linux_console_logs_in_as_the_account_the_template_provisioned(settings):
    """A real Linux session carries the Windows RDP user; the console must not use it.

    `create_session` sets ``rdp_user = guest.user`` (the training account on a Windows
    guest), and the old code preferred it here too — but the SSH console transport a
    Linux template is built with sets the password for ``guest.linux_user`` and nothing
    else. So every Linux console asked guacd to log in as an account the image did not
    have, and the shell refused: a console that never opened, on a scenario that was
    fine. The test above passes ``rdp_user=""``, which is why it never caught this.
    """
    settings.guac.linux_ssh = True
    settings.guest.user = "student"
    settings.guest.password = "TrainMe!12345"
    assert settings.guest.linux_user == "root"  # the shipped default
    scenario = _Scenario("linux")
    session = make_session(rdp_user="student", rdp_password="TrainMe!12345")
    payload = guac.build_payload(settings, session, scenario)
    connection = next(iter(payload["connections"].values()))
    assert connection["parameters"]["username"] == "root"
    assert connection["parameters"]["password"] == "TrainMe!12345"
    # ...and the RDP half still gets the training account.
    rdp = guac.rdp_parameters(settings, session)
    assert rdp["username"] == "student"


def test_the_gateway_probe_signs_with_the_portals_own_key(settings):
    """The probe must test the agreement, not merely reach the gateway.

    It sends a payload signed with `guac.secret_key` — the same thing a student's link
    carries — so a gateway that accepts it is a gateway that will open the console.
    """
    sent = {}

    def post(url, fields, timeout):
        sent.update(url=url, fields=fields, timeout=timeout)
        return 200, '{"authToken":"abc","dataSource":"json"}'

    state, detail = guac.probe_gateway(settings, post=post)
    assert state == "ok", detail
    assert sent["url"].endswith("/guacamole/api/tokens")
    payload = guac.decode_payload(sent["fields"]["data"], settings.guac.secret_bytes())
    assert payload["connections"]["OnTrak doctor"]["protocol"] == "rdp"


def test_the_gateway_probe_names_a_key_mismatch(settings):
    """A refused payload is the "console never opens" state, and it must be blocking.

    Guacamole answers every student with this exact response when JSON_SECRET_KEY and
    guac.secret_key disagree — or when the JSON extension is off — and the portal has
    no way to notice, because it never sees the gateway's answer.
    """
    state, detail = guac.probe_gateway(
        settings, post=lambda url, fields, timeout: (403, '{"message":"Permission denied."}')
    )
    assert state == "refused"
    assert "JSON_SECRET_KEY" in detail
    assert "console never opens" in detail


def test_the_gateway_probe_warns_rather_than_fails_when_nothing_answers(settings):
    """Split-horizon DNS is normal: the browser's URL need not resolve on the host."""

    def dead(url, fields, timeout):
        raise OSError("name or service not known")

    state, detail = guac.probe_gateway(settings, post=dead)
    assert state == "unreachable"
    assert "could not reach" in detail


def test_the_gateway_probe_skips_when_there_is_no_console(settings):
    settings.guac.base_url = ""
    state, detail = guac.probe_gateway(settings, post=lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert state == "skipped"
    assert "guac.base_url" in detail


def test_an_unknown_scenario_still_gets_rdp(settings):
    # No scenario means the payload cannot be classified; RDP is the pre-existing
    # behaviour and the safer guess (a Windows VM is what the pool holds).
    assert guac.protocol_for(settings, None) == "rdp"
