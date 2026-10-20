"""Google Cloud Run services as compute instances (Cloud Run Admin API v2).

``GET https://run.googleapis.com/v2/projects/{project}/locations/{region}/services``.
Instance id = service id (last path segment). Cloud Run scales to zero on its own and has
no start/stop, so the ``power`` capability is not declared.

Settings: ``project_id`` (defaults to the service account's project), ``region``,
``services`` (ids managed by OmniFree, tagged ``managed=true``), ``api_base`` (override).
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.interfaces import ComputeAdapter, HealthStatus, PluginMeta, VMInstance
from plugins.gcp._auth import GCPAuthMixin

_API = "https://run.googleapis.com/v2"

_CONDITION_STATE = {
    "CONDITION_SUCCEEDED": "running",
    "CONDITION_FAILED": "error",
    "CONDITION_PENDING": "deploying",
    "CONDITION_RECONCILING": "deploying",
}


class CloudRunAdapter(GCPAuthMixin, ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Google Cloud Run",
        description="Serverless containers hosting hub_api (2M requests/month free).",
        required_secrets=("service_account_json",),
        docs_url="https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services",
    )

    def _base(self) -> str:
        region = self.require_setting("region")
        api = str(self.setting("api_base", _API)).rstrip("/")
        return f"{api}/projects/{self.project_id()}/locations/{region}/services"

    def _to_instance(self, svc: dict[str, Any]) -> VMInstance:
        full_name = str(svc.get("name", ""))
        service_id = full_name.rsplit("/", 1)[-1]
        condition = svc.get("terminalCondition") or {}
        if svc.get("reconciling"):
            state = "deploying"
        else:
            state = _CONDITION_STATE.get(str(condition.get("state", "")), "unknown")
        scaling = (svc.get("template") or {}).get("scaling") or svc.get("scaling") or {}
        return VMInstance(
            id=service_id,
            name=service_id,
            provider=self.name,
            state=state,  # type: ignore[arg-type]
            region=str(self.setting("region")),
            shape="cloud-run-service",
            url=svc.get("uri"),
            tags={"managed": "true"} if service_id in (self.setting("services") or []) else {},
            meta={
                "resource": full_name,
                "latest_ready_revision": svc.get("latestReadyRevision"),
                "latest_created_revision": svc.get("latestCreatedRevision"),
                "update_time": svc.get("updateTime"),
                "condition": condition.get("message") or condition.get("reason"),
                "scaling": scaling,
                "ingress": svc.get("ingress"),
            },
        )

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self.gcp_call("GET", self._base(), params={"pageSize": 1}))

    async def list_instances(self) -> list[VMInstance]:
        out: list[VMInstance] = []
        params: dict[str, Any] = {"pageSize": 100}
        while True:
            body = await self.gcp_call("GET", self._base(), params=params)
            out.extend(self._to_instance(s) for s in body.get("services") or [])
            token = body.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token

    async def status(self, instance_id: str) -> VMInstance:
        body = await self.gcp_call("GET", f"{self._base()}/{instance_id}")
        return self._to_instance(body)
