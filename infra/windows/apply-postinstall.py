#!/usr/bin/env python3
"""Run infrastructure/windows/post-install.ps1 inside a running Windows VM.

    infra/windows/apply-postinstall.py <instance-name> <ip-address>

Uses the same guest driver the session manager uses (ontrak.guest), so if this
works, provisioning will work too. Writes the training password to a temporary
config file inside the guest, runs the script, verifies its marker, then deletes
the file.

TWO ACCOUNTS, NOT ONE. It connects with the *image's* own local administrator and
creates the *training* account inside. `guest.user` cannot be the connection: a
freshly built image has no training account at all -- post-install.ps1 is what
creates it -- so authenticating as it to a just-built image fails with

    the specified credentials were rejected by the server

while 5985 is open and the guest is answering, which reads like a firewall or a
WinRM fault and is neither. What a fresh incus-windows image has is the account its
own OEM/unattend.xml hands sysprep: `admin`, password `changeme` (its predecessor
in the AutoLogon is `administrator`/`vagrant`, and it is gone by then).
ONTRAK_GOLDEN_IMAGE_USER and ONTRAK_GOLDEN_IMAGE_PASSWORD override that pair for a
checkout that changes it; guest.user and guest.password remain what the training
account is created with.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ontrak.config import load_settings  # noqa: E402
from ontrak.guest import GuestError, build_driver  # noqa: E402
from ontrak.models import Session  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "post-install.ps1"
GUEST_CONFIG = r"C:\ProgramData\OnTrak\config.json"
GUEST_SCRIPT = r"C:\ProgramData\OnTrak\post-install.ps1"
MARKER = "ONTRAK-POSTINSTALL-OK"

# The account a freshly built image has, from incus-windows' own unattend.
IMAGE_USER = os.environ.get("ONTRAK_GOLDEN_IMAGE_USER", "admin")
IMAGE_PASSWORD = os.environ.get("ONTRAK_GOLDEN_IMAGE_PASSWORD", "changeme")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    instance, ip = argv[1], argv[2]

    settings = load_settings()
    if not settings.guest.password:
        print("guest.password is empty: set ONTRAK_GUEST__PASSWORD first", file=sys.stderr)
        return 2

    # What the training account is created with, from the range's own config ...
    training_user = settings.guest.user
    training_password = settings.guest.password
    # ... and what the connection *to the image* uses, which is upstream's account
    # until post-install.ps1 has created the other one. See this file's docstring.
    settings = replace(
        settings, guest=replace(settings.guest, user=IMAGE_USER, password=IMAGE_PASSWORD)
    )

    driver = build_driver(settings)
    session = Session(
        id=None,
        student="<golden-build>",
        scenario_id="<golden-build>",
        instance=instance,
        host_ip=ip,
        rdp_user=training_user,
        rdp_password=training_password,
    )

    print(f"waiting for the {driver.name} transport on {instance} ({ip}) as {IMAGE_USER} ...")
    if not driver.wait_ready(session, timeout=settings.guest.ready_timeout_seconds):
        print(
            "the guest never became reachable; check WinRM, and that "
            f"{IMAGE_USER} is the account this image has",
            file=sys.stderr,
        )
        return 1

    payload = json.dumps({"user": training_user, "password": training_password})
    driver.upload_text(payload, GUEST_CONFIG, host=ip, instance=instance)
    driver.upload_file(SCRIPT, GUEST_SCRIPT, host=ip, instance=instance)

    print("running post-install.ps1 ...")
    result = driver.run_script_file(GUEST_SCRIPT, host=ip, instance=instance, timeout=900)
    output = (result.stdout or "") + (result.stderr or "")
    print(output.strip())

    if MARKER not in output:
        print(f"\npost-install did not report {MARKER} (exit {result.exit_code})", file=sys.stderr)
        return 1

    # Belt and braces: post-install deletes the file itself, but a crash between
    # writing it and finishing would leave credentials on the image.
    driver.run_powershell(
        f"Remove-Item -Path '{GUEST_CONFIG}' -Force -ErrorAction SilentlyContinue; 'cleaned'",
        host=ip,
        instance=instance,
    )
    print(f"\nok: golden image customised on {instance}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except GuestError as exc:
        # `from exc` keeps the guest error in the traceback: this exits the
        # process, and the reason is the only thing that explains the exit.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
