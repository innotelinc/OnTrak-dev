"""Talking to the guest.

Windows guests have three interchangeable drivers:

``winrm``
    Default. Needs WinRM enabled in the image, which the golden-image build
    does. Works on any Windows version and does not care about vsock support.
``incus-exec``
    Uses the Incus Windows agent over virtio-vsock (Incus 6.22+, virtio-win
    0.1.285+, ``Incus-Agent`` service set to Automatic). No network dependency,
    so it also survives scenarios that break the student's NIC.
``null``
    Does nothing and reports empty output. Used for dry runs, catalogue
    validation and CI.

Linux guests use the shell drivers, which speak the same contract in shell: the guest
still injects a fault in ``setup.sh`` and reports grading JSON from ``check.sh``.

``incus-shell``
    Default for Linux. Runs inside the container or VM through the Incus agent, so a
    lab machine needs no reachable sshd, no key material and no extra port.
``ssh``
    For machines OnTrak does not run on Incus. Key-based only.

All PowerShell is sent as a UTF-16LE ``-EncodedCommand`` so quoting, newlines and
non-ASCII characters survive every transport. Shell is sent on stdin for the same
reason. File uploads are base64 in a quoted heredoc over that channel, so no
SMB/SCP path is required and no transfer-API flags have to be guessed per Incus
version.
"""

from __future__ import annotations

import base64
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import Session

POWERSHELL = "powershell"
# Windows command lines are capped (cmd.exe 8k, WinRM envelopes larger but not
# unbounded); 32 KB of base64 per call is safely inside every transport.
UPLOAD_CHUNK = 32_000
# The same upload has to survive two very different transports, and only one of them is
# roomy. A Linux guest is handed the payload in a heredoc on stdin, where 32k is cheap.
# WinRM runs each chunk as a PowerShell `-EncodedCommand` on a Windows command line,
# which the guest refuses with "The command line is too long" long before that:
# measured against an 8197-byte post-install.ps1 whose 10,932-character base64 became
# roughly 14,600 characters of -EncodedCommand, and failed uploading at offset 0. 2,000
# holds the whole command line near a third of the limit, which is the room the
# -EncodedCommand wrapper, the file paths and Add-Content's own arguments need.
WINRM_UPLOAD_CHUNK = 2_000


@dataclass
class CommandResult:
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0

    def __bool__(self) -> bool:  # convenience: `if result:`
        return self.ok


class GuestError(RuntimeError):
    """Raised for unrecoverable transport problems."""


def encode_ps(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell_argv(script: str) -> list[str]:
    return [
        POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encode_ps(script),
    ]


def quote_ps(value: str) -> str:
    """Single-quote a value for PowerShell, escaping embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def wait_for_port(host: str, port: int, timeout: int = 300, interval: float = 3.0) -> bool:
    """Poll a TCP port until it accepts connections (or we give up)."""
    if not host:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(interval)
    return False


class BaseDriver:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.guest = settings.guest

    # -- addressing ----------------------------------------------------
    def resolve_host(self, instance: str = "", host: str = "") -> str:
        if host:
            return host
        if self.guest.static_host:
            return self.guest.static_host
        raise GuestError(
            f"{self.name} driver needs a host address for instance {instance!r}; "
            "set guest.static_host or pass the session's host_ip"
        )

    # -- transport primitives -----------------------------------------
    def run_powershell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        raise NotImplementedError

    # -- generic operations (work on every transport) ------------------
    def run_script_file(
        self, remote_path: str, host: str = "", instance: str = "", timeout: int = 300
    ) -> CommandResult:
        """Run a .ps1 already inside the guest, with prefixed error output."""
        script = (
            "$ErrorActionPreference='Continue';"
            f"& {quote_ps(remote_path)} *>&1 | Out-String -Width 4096"
        )
        return self.run_powershell(script, host=host, instance=instance, timeout=timeout)

    def upload_text(
        self, text: str, remote_path: str, host: str = "", instance: str = "", timeout: int = 90
    ) -> CommandResult:
        """Write UTF-8 text to a guest path (used for scenario scripts)."""
        return self._write_bytes(
            text.encode("utf-8"), remote_path, host=host, instance=instance, timeout=timeout
        )

    def upload_file(
        self, local_path: str | Path, remote_path: str, host: str = "", instance: str = "",
        timeout: int = 120,
    ) -> CommandResult:
        return self._write_bytes(
            Path(local_path).read_bytes(), remote_path, host=host, instance=instance, timeout=timeout
        )

    # How much base64 goes on one command line. Overridden where the transport is
    # narrower than the payload: see WINRM_UPLOAD_CHUNK.
    upload_chunk = UPLOAD_CHUNK

    def _write_bytes(
        self,
        data: bytes,
        remote_path: str,
        host: str = "",
        instance: str = "",
        timeout: int = 120,
    ) -> CommandResult:
        """Chunked base64 upload. Retries the final decode once: a single flaky
        call should not fail a provisioning step."""
        b64 = base64.b64encode(data).decode("ascii")
        remote_b64 = remote_path + ".b64"
        parent = remote_path.rsplit("\\", 1)[0] if "\\" in remote_path else "."

        def run(script: str) -> CommandResult:
            return self.run_powershell(script, host=host, instance=instance, timeout=timeout)

        prep = (
            f"New-Item -ItemType Directory -Force -Path {quote_ps(parent)} | Out-Null;"
            f"Set-Content -Path {quote_ps(remote_b64)} -Value '' -NoNewline -Encoding Ascii"
        )
        result = run(prep)
        if not result.ok:
            raise GuestError(f"cannot prepare {parent}: {result.stderr or result.stdout}")

        for offset in range(0, len(b64), self.upload_chunk):
            chunk = b64[offset : offset + self.upload_chunk]
            result = run(
                f"Add-Content -Path {quote_ps(remote_b64)} -Value '{chunk}' "
                "-NoNewline -Encoding Ascii"
            )
            if not result.ok:
                raise GuestError(
                    f"upload of {remote_path} failed at offset {offset}: "
                    f"{result.stderr or result.stdout}"
                )

        decode = (
            f"$b=[Convert]::FromBase64String((Get-Content -Raw -Path {quote_ps(remote_b64)}));"
            f"[IO.File]::WriteAllBytes({quote_ps(remote_path)},$b);"
            f"Remove-Item -Path {quote_ps(remote_b64)} -Force;"
            f"'wrote {len(data)} bytes'"
        )
        result = run(decode)
        if not result.ok:
            raise GuestError(f"upload decode failed for {remote_path}: {result.stderr or result.stdout}")
        return result

    # -- readiness -----------------------------------------------------
    def wait_ready(self, session: Session, timeout: int | None = None) -> bool:
        raise NotImplementedError

    def _wait_for_powershell(self, session: Session, timeout: int | None = None) -> bool:
        deadline = time.time() + (timeout or self.guest.boot_timeout_seconds)
        while time.time() < deadline:
            result = self.run_powershell("$env:COMPUTERNAME", host=session.host_ip,
                                        instance=session.instance, timeout=30)
            if result.ok and result.stdout.strip():
                return True
            time.sleep(5)
        return False


def quote_sh(value: str) -> str:
    """Single-quote a value for a POSIX shell, escaping embedded quotes."""
    return "'" + str(value).replace("'", "'\\''") + "'"


class ShellRunner(BaseDriver):
    """Base for Linux guests: the same contract as Windows, spoken in shell.

    A scenario on Linux owes the platform exactly what a Windows scenario owes it: a
    ``setup.sh`` that injects the fault and confirms it, and a ``check.sh`` that prints
    the grading JSON between the markers. Everything downstream — scoring, the portal,
    the ticket rubric — stays platform-independent because of that.
    """

    interpreter = "bash"

    def run_shell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        raise NotImplementedError

    def run_powershell(  # noqa: ARG002 - part of the driver interface
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        raise GuestError(
            f"the {self.name} driver talks shell, not PowerShell: a Linux scenario must "
            "ship setup.sh/check.sh with `platform: linux` in scenario.yaml"
        )

    def run_script_file(
        self, remote_path: str, host: str = "", instance: str = "", timeout: int = 300
    ) -> CommandResult:
        """Run a shell script already inside the guest, stderr folded in so a crashed
        setup explains itself in the caller's error message."""
        return self.run_shell(
            f"{self.interpreter} {quote_sh(remote_path)} < /dev/null 2>&1",
            host=host,
            instance=instance,
            timeout=timeout,
        )

    def _write_bytes(
        self,
        data: bytes,
        remote_path: str,
        host: str = "",
        instance: str = "",
        timeout: int = 120,
    ) -> CommandResult:
        """Upload any file (text or binary) over stdin.

        Base64 in a quoted heredoc rather than a quoted command line: the payload can
        contain anything at all, the delimiter is never expanded, and it depends on no
        file-transfer flags that differ between Incus versions.
        """
        payload = base64.b64encode(data).decode("ascii")
        parent = str(Path(remote_path).parent)
        script = (
            f"mkdir -p {quote_sh(parent)}\n"
            f"base64 -d > {quote_sh(remote_path)} <<'ONTRAK_B64'\n"
            f"{payload}\n"
            "ONTRAK_B64\n"
            "printf 'wrote %s bytes\\n' " + quote_sh(str(len(data)))
        )
        return self.run_shell(script, host=host, instance=instance, timeout=timeout)

    def wait_ready(self, session: Session, timeout: int | None = None) -> bool:
        deadline = time.time() + (timeout or self.guest.linux_ready_timeout_seconds)
        while time.time() < deadline:
            result = self.run_shell(
                "printf 'ontrak-ready\\n'",
                host=session.host_ip,
                instance=session.instance,
                timeout=30,
            )
            if result.ok and "ontrak-ready" in (result.stdout or ""):
                return True
            time.sleep(4)
        return False


class IncusShellDriver(ShellRunner):
    """Linux guests over the Incus agent: no credentials, no open port, no keys.

    The default for Linux: a container or a cloud image with the agent is reachable
    the moment it boots, and a machine that has to be graded should not also have to
    be reachable over SSH to be gradable.
    """

    name = "incus-shell"

    def __init__(self, settings: Settings, client=None):
        super().__init__(settings)
        from .incus import IncusClient

        self.client = client or IncusClient(settings)

    def run_shell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        if not instance:
            raise GuestError("the incus-shell driver needs the instance name")
        started = time.time()
        proc = self.client.guest_shell(
            instance, script, timeout=timeout, user=self.guest.linux_user or None
        )
        return CommandResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=time.time() - started,
        )


class SSHDriver(ShellRunner):
    """Linux guests over SSH, for machines OnTrak does not run on Incus.

    Key-based only: an sshd accepting passwords is one more credential to rotate in
    every image, and the lab already has a credential story. Use the Incus transport
    whenever the guest can run the agent.
    """

    name = "ssh"

    def _argv(self, target: str, command: str) -> list[str]:
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-p", str(self.guest.ssh_port),
        ]
        if self.guest.ssh_key:
            argv += ["-i", self.guest.ssh_key]
        argv += [f"{self.guest.linux_user or 'root'}@{target}", command]
        return argv

    def run_shell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        target = self.resolve_host(instance=instance, host=host)
        argv = self._argv(target, f"{self.interpreter} -s")
        started = time.time()
        try:
            proc = subprocess.run(  # noqa: S603 - argv is built here, never user input
                argv,
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GuestError(f"the ssh client is not installed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GuestError(f"ssh {target} timed out after {timeout}s") from exc
        return CommandResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=time.time() - started,
        )


class WinRMDriver(BaseDriver):
    name = "winrm"
    # A Windows command line is the narrow one; a scenario's setup.ps1 or this
    # repository's own post-install.ps1 is what makes it matter.
    upload_chunk = WINRM_UPLOAD_CHUNK

    def _session(self, host: str):
        try:
            import winrm  # noqa: PLC0415 - optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover - dependency path
            raise GuestError(
                "pywinrm is not installed; `pip install pywinrm` or set guest.driver"
            ) from exc
        scheme = "https" if self.guest.winrm_use_ssl else "http"
        return winrm.Session(
            f"{scheme}://{host}:{self.guest.winrm_port}/wsman",
            auth=(self.guest.user, self.guest.password),
            transport=self.guest.winrm_transport,
            server_cert_validation="ignore",
        )

    def run_powershell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        target = self.resolve_host(instance, host)
        started = time.time()
        try:
            result = self._session(target).run_cmd(POWERSHELL, powershell_argv(script)[1:])
        except Exception as exc:  # winrm raises a wide variety of transport errors
            return CommandResult(False, 1, "", str(exc), time.time() - started)
        return CommandResult(
            ok=result.status_code == 0,
            exit_code=result.status_code,
            stdout=result.std_out.decode("utf-8", "replace"),
            stderr=result.std_err.decode("utf-8", "replace"),
            duration=time.time() - started,
        )

    def wait_ready(self, session: Session, timeout: int | None = None) -> bool:
        host = self.resolve_host(session.instance, session.host_ip)
        if not wait_for_port(host, self.guest.rdp_port, timeout or self.guest.boot_timeout_seconds):
            return False
        return self._wait_for_powershell(session, timeout or self.guest.ready_timeout_seconds)


class IncusExecDriver(BaseDriver):
    """Runs commands through the Incus Windows agent (virtio-vsock)."""

    name = "incus-exec"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        from .incus import IncusClient  # local import: avoids an import cycle

        self.client = IncusClient(settings)

    def _exec(self, instance: str, args: list[str], timeout: int) -> CommandResult:
        started = time.time()
        try:
            proc = self.client.exec_in(instance, args, timeout=timeout)
        except Exception as exc:
            return CommandResult(False, 1, "", str(exc), time.time() - started)
        return CommandResult(
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=time.time() - started,
        )

    def run_powershell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        if not instance:
            raise GuestError("incus-exec driver requires an instance name")
        return self._exec(instance, powershell_argv(script), timeout)

    def wait_ready(self, session: Session, timeout: int | None = None) -> bool:
        if not session.instance:
            return False
        deadline = time.time() + (timeout or self.guest.boot_timeout_seconds)
        while time.time() < deadline:
            result = self.run_powershell("$env:COMPUTERNAME", instance=session.instance, timeout=30)
            if result.ok and result.stdout.strip():
                return True
            time.sleep(5)
        return False


class NullDriver(BaseDriver):
    """No-op driver: everything succeeds with canned/empty output.

    Lets you validate the catalogue, exercise the portal and run the test suite
    without any Windows infrastructure. ``responses`` maps a substring of the
    script to output, which is how tests simulate a passing or failing guest.
    """

    name = "null"

    def __init__(self, settings: Settings, responses: dict[str, str] | None = None):
        super().__init__(settings)
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def run_powershell(
        self, script: str, host: str = "", instance: str = "", timeout: int = 120
    ) -> CommandResult:
        self.calls.append((instance, script[:200]))
        for needle, response in self.responses.items():
            if needle in script:
                return CommandResult(True, 0, response)
        return CommandResult(True, 0, "")

    def wait_ready(self, session: Session, timeout: int | None = None) -> bool:
        return True

    def _write_bytes(self, data: bytes, remote_path: str, host: str = "", instance: str = "",
                     timeout: int = 120) -> CommandResult:
        self.calls.append((instance, f"upload:{remote_path}:{len(data)}B"))
        return CommandResult(True, 0, "wrote bytes")


def build_shell_driver(settings: Settings, client=None) -> BaseDriver:
    """Pick the Linux transport.

    ``guest.linux_driver`` decides; ``incus-shell`` is the default because it needs no
    credentials and no open port. A scenario that names a Linux workload is graded
    through this driver whatever ``guest.driver`` says, since a Windows transport
    cannot speak shell.
    """
    kind = (settings.guest.linux_driver or "incus-shell").strip().lower()
    if kind in {"ssh"}:
        return SSHDriver(settings)
    return IncusShellDriver(settings, client=client)


def build_driver(settings: Settings, responses: dict[str, str] | None = None) -> BaseDriver:
    driver = (settings.guest.driver or "winrm").strip().lower()
    if driver == "winrm":
        return WinRMDriver(settings)
    if driver in {"incus-exec", "incus", "agent"}:
        return IncusExecDriver(settings)
    if driver in {"null", "none", "dry-run"}:
        return NullDriver(settings, responses)
    if driver in {"incus-shell", "shell", "posix"}:
        return IncusShellDriver(settings)
    if driver == "ssh":
        return SSHDriver(settings)
    raise GuestError(
        f"unknown guest.driver {settings.guest.driver!r} "
        "(winrm | incus-exec | incus-shell | ssh | null)"
    )


__all__ = [
    "BaseDriver",
    "CommandResult",
    "GuestError",
    "IncusExecDriver",
    "IncusShellDriver",
    "NullDriver",
    "SSHDriver",
    "ShellRunner",
    "WinRMDriver",
    "build_driver",
    "build_shell_driver",
    "encode_ps",
    "powershell_argv",
    "quote_ps",
    "quote_sh",
    "wait_for_port",
]
