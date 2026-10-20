"""Test doubles for the automation tests (test code only)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from automation.browser_provisioner import PageSnapshot
from automation.remote import CommandResult


class ScriptedConsole:
    """Answers prompts from a list; records everything printed and asked."""

    def __init__(self, answers: Sequence[str] = (), secrets: Sequence[str] = ()) -> None:
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.printed: list[str] = []
        self.prompts: list[str] = []

    def print(self, message: str = "") -> None:
        self.printed.append(message)

    def ask(self, prompt: str, default: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        return answer or default

    def secret(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.secrets:
            raise AssertionError(f"unexpected secret prompt: {prompt}")
        return self.secrets.pop(0)

    @property
    def output(self) -> str:
        return "\n".join(self.printed)


class FakeBrowser:
    """Serves a sequence of snapshots; each goto/snapshot call is recorded."""

    def __init__(self, snapshots: Sequence[PageSnapshot], selector_texts: Sequence[str] = ()) -> None:
        self.snapshots = list(snapshots)
        self.visited: list[str] = []
        self.texts = list(selector_texts)
        self.snapshot_calls = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def goto(self, url: str) -> None:
        self.visited.append(url)

    async def snapshot(self) -> PageSnapshot:
        self.snapshot_calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    async def selector_texts(self, selectors: Sequence[str]) -> list[str]:
        return list(self.texts)


class FakeRemote:
    def __init__(self, host: str = "fake", responses: dict[str, CommandResult] | None = None,
                 tailscale_ip: str = "100.64.0.7") -> None:
        self.host = host
        self.commands: list[tuple[str, str | None]] = []
        self.files: dict[str, tuple[bytes, int]] = {}
        self.responses = responses or {}
        self.tailscale_ip = tailscale_ip
        self.closed = False

    async def run(self, command: str, *, input_text: str | None = None, check: bool = True) -> CommandResult:
        self.commands.append((command, input_text))
        for prefix, result in self.responses.items():
            if command.startswith(prefix):
                return result
        if command == "tailscale ip -4":
            return CommandResult(0, self.tailscale_ip + "\n", "")
        return CommandResult(0, "", "")

    async def put_bytes(self, data: bytes, remote_path: str, mode: int = 0o644) -> None:
        self.files[remote_path] = (data, mode)

    async def close(self) -> None:
        self.closed = True

    def all_text(self) -> str:
        return "\n".join(c + "\n" + (i or "") for c, i in self.commands)


class FakeRunner:
    """CommandRunner stand-in: records argv, returns scripted outputs."""

    def __init__(self, outputs: dict[str, str] | None = None, failing: Sequence[str] = (),
                 side_effect: Callable[[list[str]], None] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outputs = outputs or {}
        self.failing = list(failing)
        self.side_effect = side_effect
        self.secrets_to_mask: list[str] = []

    def add_secret(self, *values: str | None) -> None:
        self.secrets_to_mask.extend(v for v in values if v)

    def run(self, cmd: Sequence[str], *, cwd: Any = None, env: Any = None, input_text: str | None = None,
            capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [str(c) for c in cmd]
        joined = " ".join(argv)
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env or {}), "input": input_text})
        if self.side_effect:
            self.side_effect(argv)
        code = 1 if any(joined.startswith(f) for f in self.failing) else 0
        out = ""
        for prefix, value in self.outputs.items():
            if joined.startswith(prefix):
                out = value
        if check and code:
            from automation.deploy_common import DeployError

            raise DeployError(f"failed: {joined}")
        return subprocess.CompletedProcess(argv, code, out, "")

    def joined(self) -> list[str]:
        return [" ".join(c["argv"]) for c in self.calls]


# ---------------------------------------------------------------------- fake OCI SDK


class FakeServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _Models:
    def __getattr__(self, name: str) -> Callable[..., SimpleNamespace]:
        return lambda **kw: SimpleNamespace(_type=name, **kw)


def _resp(data: Any) -> SimpleNamespace:
    return SimpleNamespace(data=data)


class FakeCloud:
    """Just enough of OCI's compute/network/identity/blockstorage clients."""

    def __init__(self, capacity_failures: int = 0, existing: Sequence[str] = ()) -> None:
        self.capacity_failures = capacity_failures
        self.vcns: list[Any] = []
        self.subnets: list[Any] = []
        self.igws: list[Any] = []
        self.instances: list[Any] = [SimpleNamespace(id=f"ocid1.instance.{n}", display_name=n,
                                                     lifecycle_state="RUNNING", availability_domain="AD-1",
                                                     shape="X") for n in existing]
        self.volumes: list[Any] = []
        self.attachments: list[Any] = []
        self.launched: list[Any] = []
        self.security_updates: list[Any] = []
        self.route_rules: list[Any] = []
        self.budgets: list[Any] = []
        self.alert_rules: list[Any] = []

    # identity
    def list_availability_domains(self, compartment_id: str) -> Any:
        return _resp([SimpleNamespace(name="AD-1"), SimpleNamespace(name="AD-2")])

    # network
    def list_vcns(self, compartment_id: str, display_name: str) -> Any:
        return _resp([v for v in self.vcns if v.display_name == display_name])

    def create_vcn(self, details: Any) -> Any:
        vcn = SimpleNamespace(id="ocid1.vcn.1", display_name=details.display_name, lifecycle_state="AVAILABLE",
                              default_route_table_id="rt1", default_security_list_id="sl1")
        self.vcns.append(vcn)
        return _resp(vcn)

    def get_vcn(self, vcn_id: str) -> Any:
        return _resp(next(v for v in self.vcns if v.id == vcn_id))

    def list_internet_gateways(self, compartment_id: str, vcn_id: str, display_name: str) -> Any:
        return _resp(list(self.igws))

    def create_internet_gateway(self, details: Any) -> Any:
        igw = SimpleNamespace(id="igw1")
        self.igws.append(igw)
        return _resp(igw)

    def get_route_table(self, rt_id: str) -> Any:
        return _resp(SimpleNamespace(route_rules=list(self.route_rules)))

    def update_route_table(self, rt_id: str, details: Any) -> Any:
        self.route_rules = list(details.route_rules)
        return _resp(None)

    def update_security_list(self, sl_id: str, details: Any) -> Any:
        self.security_updates.append(details)
        return _resp(None)

    def get_security_list(self, sl_id: str) -> Any:
        return _resp(SimpleNamespace(ingress_security_rules=self.security_updates[-1].ingress_security_rules))

    def list_subnets(self, compartment_id: str, vcn_id: str, display_name: str) -> Any:
        return _resp(list(self.subnets))

    def create_subnet(self, details: Any) -> Any:
        sn = SimpleNamespace(id="subnet1", lifecycle_state="AVAILABLE")
        self.subnets.append(sn)
        return _resp(sn)

    def get_subnet(self, sid: str) -> Any:
        return _resp(self.subnets[0])

    def get_vnic(self, vnic_id: str) -> Any:
        return _resp(SimpleNamespace(public_ip="203.0.113.10", private_ip="10.42.0.10"))

    # compute
    def list_instances(self, compartment_id: str, display_name: str) -> Any:
        return _resp([i for i in self.instances if i.display_name == display_name])

    def list_images(self, **kw: Any) -> Any:
        return _resp([SimpleNamespace(id=f"img-{kw['shape']}", display_name="Canonical-Ubuntu-24.04-2026")])

    def launch_instance(self, details: Any) -> Any:
        if self.capacity_failures > 0:
            self.capacity_failures -= 1
            raise FakeServiceError(500, "InternalError", "Out of host capacity.")
        self.launched.append(details)
        inst = SimpleNamespace(id=f"ocid1.instance.{details.display_name}", display_name=details.display_name,
                               lifecycle_state="PROVISIONING", availability_domain=details.availability_domain,
                               shape=details.shape)
        self.instances.append(inst)
        return _resp(inst)

    def get_instance(self, iid: str) -> Any:
        inst = next(i for i in self.instances if i.id == iid)
        inst.lifecycle_state = "RUNNING"
        return _resp(inst)

    def list_vnic_attachments(self, compartment_id: str, instance_id: str) -> Any:
        return _resp([SimpleNamespace(vnic_id="vnic1")])

    def list_volume_attachments(self, compartment_id: str, instance_id: str, volume_id: str) -> Any:
        return _resp([a for a in self.attachments if a.volume_id == volume_id])

    def attach_volume(self, details: Any) -> Any:
        att = SimpleNamespace(id="att1", volume_id=details.volume_id, lifecycle_state="ATTACHING", device=details.device)
        self.attachments.append(att)
        return _resp(att)

    def get_volume_attachment(self, aid: str) -> Any:
        return _resp(self.attachments[0])

    # block storage
    def list_volumes(self, compartment_id: str, display_name: str) -> Any:
        return _resp(list(self.volumes))

    def create_volume(self, details: Any) -> Any:
        vol = SimpleNamespace(id="vol1", lifecycle_state="AVAILABLE", size_in_gbs=details.size_in_gbs)
        self.volumes.append(vol)
        return _resp(vol)

    def get_volume(self, vid: str) -> Any:
        return _resp(self.volumes[0])

    # budget
    def list_budgets(self, compartment_id: str, display_name: str) -> Any:
        return _resp(list(self.budgets))

    def create_budget(self, details: Any) -> Any:
        b = SimpleNamespace(id="budget1", details=details)
        self.budgets.append(b)
        return _resp(b)

    def list_alert_rules(self, budget_id: str) -> Any:
        return _resp(list(self.alert_rules))

    def create_alert_rule(self, budget_id: str, details: Any) -> Any:
        self.alert_rules.append(details)
        return _resp(details)


def fake_oci_module(cloud: FakeCloud) -> SimpleNamespace:
    def factory(_config: dict[str, str]) -> FakeCloud:
        return cloud

    def wait_until(client: Any, response: Any, attr: str, state: str, max_wait_seconds: int = 0) -> Any:
        setattr(response.data, attr, state)
        return response

    return SimpleNamespace(
        core=SimpleNamespace(models=_Models(), ComputeClient=factory, VirtualNetworkClient=factory,
                             BlockstorageClient=factory),
        identity=SimpleNamespace(IdentityClient=factory),
        budget=SimpleNamespace(models=_Models(), BudgetClient=factory),
        exceptions=SimpleNamespace(ServiceError=FakeServiceError),
        wait_until=wait_until,
    )


def write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
