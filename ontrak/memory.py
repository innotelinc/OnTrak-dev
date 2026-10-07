"""In-memory Incus client.

Used by the demo mode (``ontrak demo``) so the whole student flow — request,
provision, console, grade, reset, complete — runs with no hypervisor, and by the
test suite. It models the behaviour that matters:

* a template only exists once it has been built *and* snapshotted,
* cloning requires that snapshot,
* instances are gone after ``delete_instance``,
* a VM only has an address while it is running.
"""

from __future__ import annotations

from collections.abc import Sequence

from .incus import IncusError, IncusNotFound, InstanceInfo


class InMemoryIncus:
    def __init__(self, image_alias: str = "ontrak-win-base", image_present: bool = True):
        self.image_alias = image_alias
        self.image_present = image_present
        self.instances: dict[str, dict] = {}
        self.snapshots: dict[str, set[str]] = {}
        self.calls: list[tuple] = []
        self.devices: list[tuple] = []
        self.configs: list[tuple] = []
        self.images: dict[str, dict] = {}
        self._ip_counter = 100

    # -- test/demo utilities -------------------------------------------
    def add_instance(self, name: str, running: bool = True, ip: str = "", snapshots=()) -> None:
        self.instances[name] = {
            "status": "RUNNING" if running else "STOPPED",
            "ip": ip or self._next_ip(),
        }
        self.snapshots[name] = set(snapshots)

    def add_image(self, alias: str, present: bool = True) -> None:
        self.images[alias] = {"alias": alias, "present": present}
        if present:
            self.image_alias = alias
            self.image_present = True

    def _next_ip(self) -> str:
        self._ip_counter += 1
        return f"10.20.0.{self._ip_counter}"

    def live_names(self) -> set[str]:
        return set(self.instances)

    # -- IncusClient surface -------------------------------------------
    def available(self, binary: str = "incus") -> bool:
        return True

    def list_instances(self) -> list[InstanceInfo]:
        out = []
        for name, data in self.instances.items():
            out.append(
                InstanceInfo(
                    name=name,
                    status=data["status"],
                    ipv4=data["ip"] if data["status"] == "RUNNING" else "",
                )
            )
        return out

    def get_instance(self, name: str) -> InstanceInfo | None:
        for info in self.list_instances():
            if info.name == name:
                return info
        return None
        return None

    def exists(self, name: str) -> bool:
        return name in self.instances

    def instance_status(self, name: str) -> str | None:
        return self.instances.get(name, {}).get("status")

    def instance_ip(self, name: str) -> str | None:
        data = self.instances.get(name)
        if not data or data["status"] != "RUNNING":
            return None
        return data["ip"]

    def snapshot_names(self, instance: str) -> list[str]:
        return sorted(self.snapshots.get(instance, set()))

    def has_snapshot(self, instance: str, snapshot: str) -> bool:
        return snapshot in self.snapshots.get(instance, set())

    def image_exists(self, alias: str) -> bool:
        if alias in self.images:
            return bool(self.images[alias].get("present"))
        return self.image_present and alias == self.image_alias

    def image_aliases(self) -> list[str]:
        names = set(self.images)
        names.add(self.image_alias)
        return sorted(names)

    def create_instance(self, name: str, image: str, profiles: Sequence[str] | None = None) -> None:
        self.calls.append(("create_instance", name, image, tuple(profiles or ())))
        if not self.image_exists(image):
            raise IncusNotFound(["init", image, name], 1, f"image {image} not found")
        self.instances[name] = {"status": "STOPPED", "ip": self._next_ip()}
        self.snapshots.setdefault(name, set())

    def copy_instance(self, source: str, name: str, instance_only: bool = True) -> None:
        self.calls.append(("copy_instance", source, name))
        instance, _, snapshot = source.partition("/")
        if instance not in self.instances:
            raise IncusNotFound(["copy", source, name], 1, f"instance {instance} not found")
        if snapshot and snapshot not in self.snapshots.get(instance, set()):
            raise IncusNotFound(["copy", source, name], 1, f"snapshot {snapshot} not found")
        self.instances[name] = {"status": "STOPPED", "ip": self._next_ip()}
        self.snapshots.setdefault(name, set())

    def start_instance(self, name: str, wait: bool = False, timeout: int | None = None) -> None:
        self.calls.append(("start_instance", name))
        if name not in self.instances:
            raise IncusNotFound(["start", name], 1, "not found")
        self.instances[name]["status"] = "RUNNING"

    def stop_instance(self, name: str, force: bool = False, timeout: int = 120) -> None:
        self.calls.append(("stop_instance", name))
        if name in self.instances:
            self.instances[name]["status"] = "STOPPED"

    def delete_instance(self, name: str, force: bool = True) -> None:
        self.calls.append(("delete_instance", name))
        self.instances.pop(name, None)
        self.snapshots.pop(name, None)

    def create_snapshot(self, instance: str, snapshot: str) -> None:
        self.calls.append(("create_snapshot", instance, snapshot))
        if instance not in self.instances:
            raise IncusNotFound(["snapshot", "create", instance], 1, "instance not found")
        self.snapshots.setdefault(instance, set()).add(snapshot)

    def delete_snapshot(self, instance: str, snapshot: str) -> None:
        self.snapshots.get(instance, set()).discard(snapshot)

    def config_get(self, instance: str, key: str) -> str:
        for name, config_key, value in reversed(self.configs):
            if name == instance and config_key == key:
                return value
        return ""

    def set_config(self, instance: str, key: str, value) -> None:
        self.configs.append((instance, key, value))

    def set_configs(self, instance: str, values: dict) -> None:
        for key, value in values.items():
            self.set_config(instance, key, value)

    def add_device(self, instance: str, kind: str, name: str, **options) -> None:
        self.devices.append((instance, kind, name, options))

    def remove_device(self, instance: str, name: str) -> None:
        pass

    def assign_profiles(self, instance: str, profiles: Sequence[str]) -> None:
        self.calls.append(("assign_profiles", instance, tuple(profiles)))

    def exec_in(self, instance: str, command, timeout: int = 60, detach: bool = False):
        raise IncusError(list(command), 1, "the in-memory client has no guest agent")

    def guest_shell(self, instance: str, script: str, timeout: int = 120, user: str | None = None):
        """Answer the readiness probe and nothing else.

        The in-memory client has no guest to run a script in, so it does the one thing
        the lifecycle genuinely needs — report that the machine is up — and returns
        empty output otherwise. Grading then fails honestly with "no grading payload",
        which is exactly what a test that reaches for this should see; demo mode uses
        its own driver instead.
        """
        import subprocess

        self.calls.append(("guest_shell", instance, script[:120]))
        if instance not in self.instances:
            raise IncusNotFound(["exec", instance], 1, "instance not found")
        stdout = "ontrak-ready\n" if "ontrak-ready" in script else ""
        return subprocess.CompletedProcess(args=["bash", "-s"], returncode=0, stdout=stdout, stderr="")

    def wait_for_status(self, instance: str, status: str, timeout: int = 300, interval: float = 2.0) -> bool:
        return self.instance_status(instance) == status

    def server_info(self) -> dict:
        return {"environment": {"server_version": "in-memory"}}

    def storage_info(self, pool: str | None = None) -> dict:
        return {"driver": "in-memory"}

    def network_names(self) -> list[str]:
        return ["ontrak0"]

    def run_json(self, args, timeout: int | None = None):
        if list(args)[:2] == ["network", "list"]:
            return [{"name": "ontrak0", "type": "bridge", "managed": True}]
        return []
