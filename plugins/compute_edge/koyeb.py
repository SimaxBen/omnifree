"""Koyeb adapter (REST API v1). Services map to instances; pause/resume is ``power``.

Settings: ``services`` (service names to show; empty = all services),
``api_base`` (default ``https://app.koyeb.com/v1``).
Extra operations used by automation/hub: ``deployments(service)``, ``redeploy(service)``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ComputeAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    VMInstance,
)

_STATES = {
    "HEALTHY": "running",
    "STARTING": "starting",
    "RESUMING": "starting",
    "PAUSING": "stopping",
    "PAUSED": "stopped",
    "DEGRADED": "error",
    "UNHEALTHY": "error",
    "DELETING": "terminated",
    "DELETED": "terminated",
}


class KoyebAdapter(ComputeAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.POWER})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Koyeb",
        description="Koyeb services (worker / health monitor); pause and resume.",
        required_secrets=("api_token",),
        docs_url="https://www.koyeb.com/docs/reference/api",
    )

    def _base(self) -> str:
        return str(self.setting("api_base", "https://app.koyeb.com/v1")).rstrip("/")

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.request(
            method, self._base() + path,
            headers={"Authorization": f"Bearer {self.require_secret('api_token')}"}, **kwargs,
        )
        return response.json() if response.content else {}

    def _instance(self, service: dict[str, Any]) -> VMInstance:
        status = str(service.get("status") or "").upper()
        return VMInstance(
            id=str(service["id"]),
            name=str(service.get("name") or service["id"]),
            provider=self.name,
            state=_STATES.get(status, "unknown"),
            region=",".join(service.get("regions") or []) or None,
            tags={"app_id": str(service.get("app_id") or ""), "type": str(service.get("type") or "")},
            meta={
                "status": status,
                "messages": service.get("messages") or [],
                "active_deployment_id": service.get("active_deployment_id"),
                "latest_deployment_id": service.get("latest_deployment_id"),
                "updated_at": service.get("updated_at"),
            },
        )

    async def _services(self) -> list[dict[str, Any]]:
        services: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._call("GET", "/services", params={"limit": 100, "offset": offset})
            batch = page.get("services") or []
            services.extend(batch)
            if len(batch) < 100 or len(services) >= int(page.get("count") or 0):
                return services
            offset += 100

    async def _resolve(self, instance_id: str) -> str:
        """Accept a service id or a service name."""
        for service in await self._services():
            if instance_id in (service.get("id"), service.get("name")):
                return str(service["id"])
        raise ProviderError(f"service {instance_id} not found", provider=self.name, status_code=404)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/services", params={"limit": 1}))

    async def list_instances(self) -> list[VMInstance]:
        wanted = set(self.setting("services", []) or [])
        return [self._instance(s) for s in await self._services() if not wanted or s.get("name") in wanted]

    async def status(self, instance_id: str) -> VMInstance:
        try:
            payload = await self._call("GET", f"/services/{instance_id}")
        except ProviderError as exc:
            if exc.status_code not in (400, 404):
                raise
            payload = await self._call("GET", f"/services/{await self._resolve(instance_id)}")
        return self._instance(payload.get("service") or payload)

    async def start(self, instance_id: str) -> VMInstance:
        service_id = await self._resolve(instance_id)
        await self._call("POST", f"/services/{service_id}/resume")
        return await self.status(service_id)

    async def stop(self, instance_id: str) -> VMInstance:
        service_id = await self._resolve(instance_id)
        await self._call("POST", f"/services/{service_id}/pause")
        return await self.status(service_id)

    # ------------------------------------------------------------------ extras
    async def deployments(self, instance_id: str, limit: int = 10) -> list[dict[str, Any]]:
        service_id = await self._resolve(instance_id)
        payload = await self._call("GET", "/deployments", params={"service_id": service_id, "limit": limit})
        return [
            {"id": d.get("id"), "status": d.get("status"), "created_at": d.get("created_at"),
             "messages": d.get("messages") or []}
            for d in payload.get("deployments") or []
        ]

    async def redeploy(self, instance_id: str) -> dict[str, Any]:
        service_id = await self._resolve(instance_id)
        payload = await self._call("POST", f"/services/{service_id}/redeploy", json={})
        deployment = payload.get("deployment") or {}
        return {"id": deployment.get("id"), "status": deployment.get("status")}

    async def wait_for(self, instance_id: str, states: set[str], timeout_s: float = 300.0,
                       interval_s: float = 5.0) -> VMInstance:
        """Poll ``status()`` until the instance reaches one of ``states``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            instance = await self.status(instance_id)
            if instance.state in states or loop.time() >= deadline:
                return instance
            await asyncio.sleep(interval_s)
