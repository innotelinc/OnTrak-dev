"""Incus client.

We drive the ``incus`` CLI rather than a Python binding on purpose: operators
already have the CLI, version skew between the daemon and a client library is a
common source of breakage, and every call here maps 1:1 onto a command you can
run by hand while debugging. ``--format=json`` gives us structured output.

For a large class, point ``incus.remote`` at an **Incus cluster** endpoint and
the daemon handles placement across hosts; nothing in this module needs to know
how many machines are behind it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import Settings

DEFAULT_TIMEOUT = 120


class IncusError(RuntimeError):
    """Raised when an incus command fails."""

    def __init__(self, args: Sequence[str], code: int, stderr: str):
        self.args_list = list(args)
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"incus {' '.join(args)} failed ({code}): {self.stderr}")


class IncusNotFound(IncusError):
    """Raised when an instance/image/snapshot does not exist."""


def _is_snapshot(source: str) -> bool:
    """Whether an instance reference names a snapshot (``tpl-x/clean``).

    Incus writes an instance and one of its snapshots as ``instance/snapshot``, so
    the slash is the distinction the CLI itself draws when it refuses a flag. Any
    ``remote:`` prefix is dropped first, so a cluster-qualified reference reads the
    same way.
    """
    body = source.split(":", 1)[1] if ":" in source else source
    return "/" in body


@dataclass
class InstanceInfo:
    name: str
    status: str
    kind: str = "virtual-machine"
    ipv4: str = ""
    cpu: int = 0
    memory_mb: int = 0
    os: str = ""
    raw: dict | None = None

    @property
    def running(self) -> bool:
        return self.status.upper() == "RUNNING"


class IncusClient:
    def __init__(self, settings: Settings, binary: str = "incus"):
        self.settings = settings
        self.incus = settings.incus
        self.binary = binary
        self.timeout = self.incus.operation_timeout_seconds
        # Account name -> uid, per instance. Resolving one costs a command, and
        # the answer cannot change while a disposable lab machine lives.
        self._uid_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _base(self, project: bool = True) -> list[str]:
        cmd = [self.binary]
        if self.incus.remote and self.incus.remote != "local":
            cmd += ["--remote", self.incus.remote]
        # `incus query` is a raw API call and refuses `--project` outright
        # (`Error: --project cannot be used with the query command`), because the
        # project belongs in the path instead. Callers that use it say so.
        if project and self.incus.project:
            cmd += ["--project", self.incus.project]
        return cmd

    def run(
        self,
        args: Sequence[str],
        timeout: int | None = None,
        check: bool = True,
        capture: bool = True,
        input_data: str | None = None,
        project: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = self._base(project) + list(args)
        # The configured operation timeout is the ceiling for *every* call, not just
        # the mutating ones that ask for it explicitly. Reads are not cheap: `incus
        # list` reports each instance's state, agent status and address, so on a busy
        # host it blocks for minutes on a VM that is still booting — and a bare
        # DEFAULT_TIMEOUT failed a template build at exactly that point ("incus list
        # --format=json failed (124): timed out after 120s") even with the operator's
        # ONTRAK_INCUS__OPERATION_TIMEOUT_SECONDS raised.
        effective_timeout = timeout or self.timeout or DEFAULT_TIMEOUT
        try:
            proc = subprocess.run(  # noqa: S603 - arguments are internal, never user input
                cmd,
                capture_output=capture,
                text=True,
                input=input_data,
                timeout=effective_timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise IncusError(args, 127, f"{self.binary} not found on PATH: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise IncusError(args, 124, f"timed out after {effective_timeout}s") from exc
        if check and proc.returncode != 0:
            stderr = proc.stderr or ""
            if "not found" in stderr.lower() or "No such" in stderr:
                raise IncusNotFound(args, proc.returncode, stderr)
            raise IncusError(args, proc.returncode, stderr)
        return proc

    def run_json(
        self, args: Sequence[str], timeout: int | None = None, project: bool = True
    ) -> Any:
        proc = self.run(args, timeout=timeout, project=project)
        out = (proc.stdout or "").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise IncusError(args, proc.returncode, f"unparseable JSON output: {exc}") from exc

    @staticmethod
    def available(binary: str = "incus") -> bool:
        return shutil.which(binary) is not None

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list_instances(self) -> list[InstanceInfo]:
        data = self.run_json(["list", "--format=json"]) or []
        return [self._to_info(entry) for entry in data]

    def get_instance(self, name: str) -> InstanceInfo | None:
        for info in self.list_instances():
            if info.name == name:
                return info
        return None

    def instance_status(self, name: str) -> str | None:
        info = self.get_instance(name)
        return info.status if info else None

    def exists(self, name: str) -> bool:
        return self.get_instance(name) is not None

    def instance_ip(self, name: str) -> str | None:
        info = self.get_instance(name)
        return info.ipv4 if info and info.ipv4 else None

    def image_exists(self, alias: str) -> bool:
        """Whether an image alias resolves — locally, or on the image server.

        Asked with `image info` and its exit code, because there is no JSON form to
        ask for: `incus image info` has no `--format` (see the note on
        :meth:`server_info`). The alias resolving *is* the question, and a
        server-side alias like `images:ubuntu/24.04` resolves without being cached
        — which is what lets a container workload launch straight from the image
        server, as the catalog planner promises.
        """
        try:
            proc = self.run(["image", "info", alias], check=False)
        except IncusError:
            return False
        return proc.returncode == 0

    def image_aliases(self) -> list[str]:
        try:
            data = self.run_json(["image", "list", "--format=json"]) or []
        except IncusError:
            return []
        aliases: list[str] = []
        for image in data:
            for alias in image.get("aliases", []) or []:
                if alias.get("name"):
                    aliases.append(alias["name"])
        return sorted(aliases)

    def snapshot_names(self, instance: str) -> list[str]:
        try:
            data = self.run_json(["snapshot", "list", instance, "--format=json"]) or []
        except IncusError:
            return []
        return [entry.get("name", "") for entry in data if entry.get("name")]

    def has_snapshot(self, instance: str, snapshot: str) -> bool:
        return snapshot in self.snapshot_names(instance)

    # The CLI's `--format` is a flag of the *list* commands. `image info`, `info`
    # and `storage info` do not have it, and asking for it is a hard failure rather
    # than a fallback: `Error: unknown flag: --format`, exit 1. Every call below is
    # therefore either a `* list --format=json` or a raw `query`, which is the
    # machine-readable form of the same answer and has existed in every version.
    def server_info(self) -> dict:
        """The server's own record: version, API extensions, environment.

        Not project-scoped, and `query` will not take a project, so the flag is
        dropped. Asking for it is what made this answer `{}`, and `doctor` print
        "incus server unknown" on a host whose server was answering fine.
        """
        try:
            return self.run_json(["query", "/1.0"], project=False) or {}
        except IncusError:
            return {}

    def storage_info(self, pool: str | None = None) -> dict:
        """One pool's record, from the list the CLI *can* render as JSON.

        `storage info` has no `--format`, and `query` refuses `--project`, so this
        is the same answer in a form that exists and stays project-aware.
        """
        name = pool or self.incus.storage_pool
        try:
            pools = self.run_json(["storage", "list", "--format=json"]) or []
        except IncusError:
            return {}
        for record in pools:
            if record.get("name") == name:
                return record
        return {}

    # ------------------------------------------------------------------
    # mutation
    # ------------------------------------------------------------------
    def create_instance(self, name: str, image: str, profiles: Sequence[str] | None = None) -> None:
        args = ["init", image, name]
        for profile in profiles or []:
            if profile:
                args += ["-p", profile]
        self.run(args, timeout=self.timeout)

    def copy_instance(self, source: str, name: str, instance_only: bool = True) -> None:
        """Copy an instance, or one of its snapshots (``tpl-x/clean``), to ``name``.

        ``instance_only`` means "the instance, without its snapshots", so it is an
        argument about an *instance* source and the CLI refuses it on a snapshot:

          Error: --instance-only can't be passed when the source is a snapshot

        Handing a student their machine is exactly that second case — the clean
        snapshot is cloned — so this flag made `session start` fail every time,
        and the pool never filled.
        """
        args = ["copy", source, name]
        if instance_only and not _is_snapshot(source):
            args.append("--instance-only")
        self.run(args, timeout=self.timeout)

    def start_instance(self, name: str, wait: bool = False, timeout: int | None = None) -> None:
        self.run(["start", name], timeout=self.timeout)
        if wait:
            self.wait_for_status(name, "RUNNING", timeout or max(self.timeout, 300))

    def stop_instance(self, name: str, force: bool = False, timeout: int = 120) -> None:
        args = ["stop", name, "--timeout", str(timeout)]
        if force:
            args.append("--force")
        self.run(args, timeout=self.timeout + timeout, check=not force)

    def delete_instance(self, name: str, force: bool = True) -> None:
        args = ["delete", name]
        if force:
            args.append("--force")
        self.run(args, timeout=self.timeout)

    def create_snapshot(self, instance: str, snapshot: str) -> None:
        self.run(["snapshot", "create", instance, snapshot], timeout=self.timeout)

    def delete_snapshot(self, instance: str, snapshot: str) -> None:
        self.run(["snapshot", "delete", instance, snapshot], timeout=self.timeout)

    def set_config(self, instance: str, key: str, value: Any) -> None:
        self.run(["config", "set", instance, f"{key}={value}"])

    def set_configs(self, instance: str, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set_config(instance, key, value)

    def add_device(self, instance: str, kind: str, name: str, **options: Any) -> None:
        args = ["config", "device", "add", instance, name, kind]
        args += [f"{k}={v}" for k, v in options.items()]
        self.run(args)

    def remove_device(self, instance: str, name: str) -> None:
        self.run(["config", "device", "remove", instance, name], check=False)

    def assign_profiles(self, instance: str, profiles: Sequence[str]) -> None:
        if not profiles:
            return
        self.run(["profile", "assign", instance, ",".join(profiles)])

    def rename_instance(self, instance: str, new_name: str) -> None:
        self.run(["rename", instance, new_name], timeout=self.timeout)

    def exec_in(
        self,
        instance: str,
        command: Sequence[str],
        timeout: int = 60,
        detach: bool = False,
        check: bool | None = None,
        input_data: str | None = None,
        user: str | None = None,
    ):
        """Run a command inside a guest (containers always; VMs need the agent).

        ``input_data`` is fed to the command's stdin, which is how shell scripts are
        executed without quoting them into a command line, and ``check`` lets a caller
        read a non-zero exit code instead of having it raised (grading does exactly
        that: a failing check is data, not an incident).
        """
        args = ["exec", instance, "-T"]
        uid = self._uid_argument(instance, user)
        if uid:
            args += ["--user", uid]
        if detach:
            args.append("--mode=detach")
        args.append("--")
        args += list(command)
        return self.run(
            args,
            timeout=timeout,
            check=(not detach) if check is None else check,
            input_data=input_data,
        )

    def _uid_argument(self, instance: str, user: str | None) -> str | None:
        """The uid to pass to ``exec --user``, or None to leave the flag off.

        `incus exec --user` takes a *numeric* uid, and refuses an account name
        outright: `invalid argument "root" for "--user" flag:
        strconv.ParseUint: parsing "root": invalid syntax`. The platform's default
        Linux account is the name `root`, so passing it through made every shell
        call fail — readiness probes never succeeded, and no template could be
        built, on any host.

        Root is what `incus exec` already uses, so uid 0 (and the name for it)
        needs no flag at all. Any other name is resolved in the guest once and
        remembered; a name that does not exist there is a misconfiguration worth
        failing on, because the alternative is running a graded check as the wrong
        account.
        """
        value = str(user or "").strip()
        if not value or value == "0" or value == "root":
            return None
        if value.isdigit():
            return value

        key = (instance, value)
        if key not in self._uid_cache:
            proc = self.run(
                ["exec", instance, "-T", "--", "id", "-u", value],
                check=False,
                timeout=30,
            )
            uid = (proc.stdout or "").strip()
            self._uid_cache[key] = uid if proc.returncode == 0 and uid.isdigit() else ""
        uid = self._uid_cache[key]
        if not uid:
            raise IncusError(
                ["exec", instance, "--user", value],
                1,
                f"no account {value!r} in {instance} — guest.linux_user must be a "
                "user that exists in the guest, or root",
            )
        # uid 0 needs no flag: it is what exec does anyway.
        return uid if uid != "0" else None

    def guest_shell(self, instance: str, script: str, timeout: int = 120, user: str | None = None):
        """Run a shell script inside a guest verbatim. Never raises on exit code."""
        return self.exec_in(
            instance,
            ["bash", "-s"],
            timeout=timeout,
            check=False,
            input_data=script,
            user=user,
        )

    def wait_for_status(
        self, instance: str, status: str, timeout: int = 300, interval: float = 2.0
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.instance_status(instance)
            if current and current.upper() == status.upper():
                return True
            time.sleep(interval)
        return False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_info(entry: dict) -> InstanceInfo:
        state = entry.get("state") or {}
        network = state.get("network") or {}
        ipv4 = ""
        for _, iface in sorted(network.items(), key=lambda kv: 0 if kv[0] == "eth0" else 1):
            for addr in iface.get("addresses", []) or []:
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    ipv4 = addr.get("address", "")
                    break
            if ipv4:
                break
        config = entry.get("config") or {}
        memory = str(config.get("limits.memory") or "")
        cpu_raw = str(config.get("limits.cpu") or "").split(",")[0].strip()
        return InstanceInfo(
            name=entry.get("name", ""),
            status=entry.get("status", "Unknown"),
            kind=entry.get("type", "virtual-machine"),
            ipv4=ipv4,
            cpu=int(cpu_raw) if cpu_raw.isdigit() else 0,
            memory_mb=_parse_memory_mb(memory),
            os=str(config.get("image.os") or ""),
            raw=entry,
        )


def _parse_memory_mb(value: str) -> int:
    text = (value or "").strip().upper()
    if not text:
        return 0
    multipliers = {"KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "TIB": 1024 * 1024}
    for suffix, factor in multipliers.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * factor)
            except ValueError:
                return 0
    try:
        return int(text)
    except ValueError:
        return 0
