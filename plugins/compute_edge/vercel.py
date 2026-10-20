"""Vercel adapter (REST API). Projects map to instances; the state is the state of the latest
production deployment (``BUILDING``/``QUEUED`` -> deploying, ``READY`` -> running,
``ERROR`` -> error, ``CANCELED`` -> stopped). No power capability.

Settings: ``projects`` (project names to show; empty = all), ``api_base``
(default ``https://api.vercel.com``). The optional ``team_id`` secret scopes every call.
Extras: ``deployments(project)``, ``redeploy(project)``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from core.interfaces import (
    ComputeAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    VMInstance,
)

_STATES = {
    "READY": "running",
    "BUILDING": "deploying",
    "QUEUED": "deploying",
    "INITIALIZING": "deploying",
    "ERROR": "error",
    "CANCELED": "stopped",
}


class VercelAdapter(ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Vercel",
        description="Vercel projects and deployments (PWA hosting).",
        required_secrets=("token",),
        optional_secrets=("team_id",),
        docs_url="https://vercel.com/docs/rest-api",
    )

    def _base(self) -> str:
        return str(self.setting("api_base", "https://api.vercel.com")).rstrip("/")

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None,
                    **kwargs: Any) -> Any:
        query = dict(params or {})
        team = self.secret("team_id")
        if team:
            query["teamId"] = team
        response = await self.request(
            method, self._base() + path, params=query,
            headers={"Authorization": f"Bearer {self.require_secret('token')}"}, **kwargs,
        )
        return response.json() if response.content else {}

    async def _latest(self, project_id: str) -> dict[str, Any] | None:
        payload = await self._call("GET", "/v6/deployments",
                                   {"projectId": project_id, "limit": 1, "target": "production"})
        deployments = payload.get("deployments") or []
        return deployments[0] if deployments else None

    def _instance(self, project: dict[str, Any], latest: dict[str, Any] | None) -> VMInstance:
        state = str((latest or {}).get("state") or (latest or {}).get("readyState") or "").upper()
        aliases = ((project.get("targets") or {}).get("production") or {}).get("alias") or []
        url = f"https://{aliases[0]}" if aliases else (
            f"https://{latest['url']}" if latest and latest.get("url") else None
        )
        return VMInstance(
            id=str(project["id"]),
            name=str(project.get("name") or project["id"]),
            provider=self.name,
            state=_STATES.get(state, "unknown") if latest else "stopped",
            url=url,
            tags={"framework": str(project.get("framework") or "")},
            meta={
                "deployment_id": (latest or {}).get("uid"),
                "deployment_state": state or None,
                "deployment_created": (latest or {}).get("created"),
                "aliases": aliases,
            },
        )

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/v9/projects", {"limit": 1}))

    async def list_instances(self) -> list[VMInstance]:
        payload = await self._call("GET", "/v9/projects", {"limit": 100})
        wanted = set(self.setting("projects", []) or [])
        projects = [p for p in payload.get("projects") or [] if not wanted or p.get("name") in wanted]
        latest = await asyncio.gather(*(self._latest(str(p["id"])) for p in projects))
        return [self._instance(p, d) for p, d in zip(projects, latest, strict=True)]

    async def status(self, instance_id: str) -> VMInstance:
        project = await self._call("GET", f"/v9/projects/{instance_id}")
        return self._instance(project, await self._latest(str(project["id"])))

    # ------------------------------------------------------------------ extras
    async def deployments(self, instance_id: str, limit: int = 10) -> list[dict[str, Any]]:
        project = await self._call("GET", f"/v9/projects/{instance_id}")
        payload = await self._call("GET", "/v6/deployments", {"projectId": project["id"], "limit": limit})
        return [
            {"id": d.get("uid"), "state": d.get("state") or d.get("readyState"), "url": d.get("url"),
             "target": d.get("target"), "created": d.get("created")}
            for d in payload.get("deployments") or []
        ]

    async def redeploy(self, instance_id: str) -> dict[str, Any]:
        """Redeploy the latest production deployment of the project."""
        project = await self._call("GET", f"/v9/projects/{instance_id}")
        latest = await self._latest(str(project["id"]))
        if not latest:
            raise ProviderError("project has no production deployment to redeploy", provider=self.name,
                                status_code=404)
        created = await self._call("POST", "/v13/deployments", json={
            "name": project.get("name"), "deploymentId": latest["uid"], "target": "production",
        })
        return {"id": created.get("id"), "state": created.get("readyState") or created.get("status"),
                "url": created.get("url")}
