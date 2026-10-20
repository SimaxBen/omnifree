"""Google Compute Engine (Always Free e2-micro) instances, Compute Engine API v1.

* list: ``GET /compute/v1/projects/{project}/zones/{zone}/instances``
* status: ``GET .../instances/{name}``
* start/stop: ``POST .../instances/{name}/start|stop`` (zonal operation). The adapter waits for the
  operation (``POST .../operations/{op}/wait``, bounded by ``settings.operation_wait_s``, default 0 =
  do not wait) and then returns the instance's current state.

Instance id = instance name. Settings: ``project_id``, ``zone``, ``instances`` (names managed by
OmniFree; when set, ``list_instances`` only returns those), ``operation_wait_s``, ``api_base``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.interfaces import Capability, ComputeAdapter, HealthStatus, PluginMeta, VMInstance
from plugins.gcp._auth import GCPAuthMixin

_API = "https://compute.googleapis.com/compute/v1"

_STATUS = {
    "PROVISIONING": "provisioning",
    "STAGING": "starting",
    "RUNNING": "running",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "SUSPENDING": "stopping",
    "SUSPENDED": "stopped",
    "REPAIRING": "error",
    "TERMINATED": "stopped",  # GCE "TERMINATED" means stopped (restartable), not deleted
}


class GCEComputeAdapter(GCPAuthMixin, ComputeAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.POWER})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Google Compute Engine",
        description="Always Free e2-micro (health monitor / uptime / background worker).",
        required_secrets=("service_account_json",),
        docs_url="https://cloud.google.com/compute/docs/reference/rest/v1/instances",
    )

    def _zone_base(self) -> str:
        zone = self.require_setting("zone")
        api = str(self.setting("api_base", _API)).rstrip("/")
        return f"{api}/projects/{self.project_id()}/zones/{zone}"

    def _to_instance(self, item: dict[str, Any]) -> VMInstance:
        nics = item.get("networkInterfaces") or []
        private_ip = public_ip = None
        if nics:
            private_ip = nics[0].get("networkIP")
            access = nics[0].get("accessConfigs") or []
            if access:
                public_ip = access[0].get("natIP")
        machine = str(item.get("machineType", "")).rsplit("/", 1)[-1] or None
        zone = str(item.get("zone", "")).rsplit("/", 1)[-1] or str(self.setting("zone"))
        return VMInstance(
            id=str(item.get("name", "")),
            name=str(item.get("name", "")),
            provider=self.name,
            state=_STATUS.get(str(item.get("status", "")), "unknown"),  # type: ignore[arg-type]
            region=zone,
            shape=machine,
            public_ip=public_ip,
            private_ip=private_ip,
            tags={str(k): str(v) for k, v in (item.get("labels") or {}).items()},
            meta={
                "gce_id": item.get("id"),
                "status": item.get("status"),
                "status_message": item.get("statusMessage"),
                "creation_timestamp": item.get("creationTimestamp"),
                "last_start_timestamp": item.get("lastStartTimestamp"),
            },
        )

    async def health(self) -> HealthStatus:
        return await self.probe(
            lambda: self.gcp_call("GET", f"{self._zone_base()}/instances", params={"maxResults": 1})
        )

    async def list_instances(self) -> list[VMInstance]:
        wanted = set(self.setting("instances") or [])
        out: list[VMInstance] = []
        params: dict[str, Any] = {"maxResults": 100}
        while True:
            body = await self.gcp_call("GET", f"{self._zone_base()}/instances", params=params)
            for item in body.get("items") or []:
                if not wanted or item.get("name") in wanted:
                    out.append(self._to_instance(item))
            token = body.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    async def status(self, instance_id: str) -> VMInstance:
        return self._to_instance(await self.gcp_call("GET", f"{self._zone_base()}/instances/{instance_id}"))

    async def _power(self, instance_id: str, action: str) -> VMInstance:
        operation = await self.gcp_call("POST", f"{self._zone_base()}/instances/{instance_id}/{action}")
        wait_s = float(self.setting("operation_wait_s", 0))
        if wait_s > 0 and operation.get("name") and operation.get("status") != "DONE":
            # operations.wait returns when DONE or after ~2 minutes, whichever is first.
            await self.gcp_call(
                "POST",
                f"{self._zone_base()}/operations/{operation['name']}/wait",
                timeout=wait_s,
            )
        return await self.status(instance_id)

    async def start(self, instance_id: str) -> VMInstance:
        return await self._power(instance_id, "start")

    async def stop(self, instance_id: str) -> VMInstance:
        return await self._power(instance_id, "stop")
