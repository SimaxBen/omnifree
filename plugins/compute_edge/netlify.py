"""Netlify adapter (REST API v1). Sites map to instances; the state follows the latest deploy
(``ready`` -> running, building/enqueued/processing/... -> deploying, ``error`` -> error).
No power capability.

Settings: ``sites`` (site names to show; empty = all), ``api_base``
(default ``https://api.netlify.com/api/v1``). Extras: ``deployments(site)``, ``redeploy(site)``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from core.interfaces import ComputeAdapter, HealthStatus, PluginMeta, VMInstance

_DEPLOYING = {"new", "pending_review", "accepted", "enqueued", "building", "uploading", "uploaded",
              "preparing", "prepared", "processing", "processed"}


def _state(deploy_state: str | None) -> str:
    if deploy_state == "ready":
        return "running"
    if deploy_state in _DEPLOYING:
        return "deploying"
    if deploy_state in ("error", "rejected"):
        return "error"
    return "unknown"


class NetlifyAdapter(ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Netlify",
        description="Netlify sites and deploys (PWA mirror).",
        required_secrets=("token",),
        docs_url="https://open-api.netlify.com/",
    )

    def _base(self) -> str:
        return str(self.setting("api_base", "https://api.netlify.com/api/v1")).rstrip("/")

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.request(
            method, self._base() + path,
            headers={"Authorization": f"Bearer {self.require_secret('token')}"}, **kwargs,
        )
        return response.json() if response.content else {}

    async def _latest(self, site_id: str) -> dict[str, Any] | None:
        deploys = await self._call("GET", f"/sites/{site_id}/deploys", params={"per_page": 1})
        return deploys[0] if deploys else None

    def _instance(self, site: dict[str, Any], latest: dict[str, Any] | None) -> VMInstance:
        deploy_state = (latest or {}).get("state") or (site.get("published_deploy") or {}).get("state")
        return VMInstance(
            id=str(site["id"]),
            name=str(site.get("name") or site["id"]),
            provider=self.name,
            state=_state(deploy_state),
            url=site.get("ssl_url") or site.get("url"),
            meta={
                "deploy_id": (latest or {}).get("id"),
                "deploy_state": deploy_state,
                "error_message": (latest or {}).get("error_message"),
                "updated_at": site.get("updated_at"),
                "custom_domain": site.get("custom_domain"),
            },
        )

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/sites", params={"per_page": 1}))

    async def list_instances(self) -> list[VMInstance]:
        wanted = set(self.setting("sites", []) or [])
        sites = [s for s in await self._call("GET", "/sites", params={"per_page": 100})
                 if not wanted or s.get("name") in wanted]
        latest = await asyncio.gather(*(self._latest(str(s["id"])) for s in sites))
        return [self._instance(s, d) for s, d in zip(sites, latest, strict=True)]

    async def status(self, instance_id: str) -> VMInstance:
        site = await self._call("GET", f"/sites/{instance_id}")
        return self._instance(site, await self._latest(str(site["id"])))

    # ------------------------------------------------------------------ extras
    async def deployments(self, instance_id: str, limit: int = 10) -> list[dict[str, Any]]:
        deploys = await self._call("GET", f"/sites/{instance_id}/deploys", params={"per_page": limit})
        return [
            {"id": d.get("id"), "state": d.get("state"), "url": d.get("deploy_ssl_url"),
             "branch": d.get("branch"), "created_at": d.get("created_at"), "error": d.get("error_message")}
            for d in deploys or []
        ]

    async def redeploy(self, instance_id: str) -> dict[str, Any]:
        build = await self._call("POST", f"/sites/{instance_id}/builds", json={})
        return {"id": build.get("id"), "deploy_id": build.get("deploy_id"), "done": build.get("done")}
