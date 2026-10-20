"""Cloudflare Workers scripts as compute instances, plus REST deployment.

* ``list_instances`` / ``status``: ``GET /accounts/{id}/workers/scripts`` (each script is a running instance).
* ``deploy_script``: ``PUT /accounts/{id}/workers/scripts/{name}`` (multipart ES-module upload),
  optionally enabling the ``workers.dev`` route. Used by ``automation/deploy.py``.
* ``delete_script``: ``DELETE /accounts/{id}/workers/scripts/{name}``.
* ``quota``: today's invocations from the GraphQL Analytics API (``workersInvocationsAdaptive``),
  compared with the configured ``requests/day`` limit. Falls back to router counters on error.

Workers cannot be powered off, so the ``power`` capability is not declared.

Settings: ``scripts`` (names managed by OmniFree), ``compatibility_date`` (default for deploys),
``api_base`` (override, tests only).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from core.interfaces import (
    ComputeAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    QuotaInfo,
    VMInstance,
)
from plugins.cloudflare._api import CloudflareAPIMixin

_GRAPHQL_QUERY = """
query WorkersRequests($accountTag: string!, $start: Time!, $end: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $start, datetime_leq: $end}) {
        sum { requests errors }
        dimensions { scriptName }
      }
    }
  }
}
"""


class CloudflareWorkersAdapter(CloudflareAPIMixin, ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cloudflare Workers",
        description="Edge routing scripts. Free 100k requests/day.",
        required_secrets=("account_id", "api_token"),
        docs_url="https://developers.cloudflare.com/api/resources/workers/subresources/scripts/",
    )

    _subdomain: str | None = None

    async def _workers_subdomain(self) -> str | None:
        if self._subdomain is None:
            try:
                result = await self.cf_call("GET", self.cf_account_url("workers/subdomain"))
            except ProviderError:
                return None
            self._subdomain = (result or {}).get("subdomain") or ""
        return self._subdomain or None

    def _to_instance(self, script: dict[str, Any], subdomain: str | None) -> VMInstance:
        name = str(script.get("id") or script.get("script_name") or "")
        meta = {
            k: script[k]
            for k in ("created_on", "modified_on", "etag", "usage_model", "compatibility_date", "handlers", "has_assets")
            if k in script
        }
        return VMInstance(
            id=name,
            name=name,
            provider=self.name,
            state="running",
            region="global",
            shape="worker",
            url=f"https://{name}.{subdomain}.workers.dev" if subdomain else None,
            tags={"managed": "true"} if name in (self.setting("scripts") or []) else {},
            meta=meta,
        )

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self.cf_call("GET", self.cf_account_url("workers/scripts")))

    async def list_instances(self) -> list[VMInstance]:
        scripts = await self.cf_call("GET", self.cf_account_url("workers/scripts")) or []
        subdomain = await self._workers_subdomain()
        return [self._to_instance(s, subdomain) for s in scripts]

    async def status(self, instance_id: str) -> VMInstance:
        for instance in await self.list_instances():
            if instance.id == instance_id:
                return instance
        raise ProviderError(f"worker script not found: {instance_id}", provider=self.name, status_code=404)

    async def deploy_script(
        self,
        script_name: str,
        code: str,
        *,
        main_module: str = "worker.js",
        compatibility_date: str | None = None,
        bindings: list[dict[str, Any]] | None = None,
        extra_modules: dict[str, str] | None = None,
        enable_workers_dev: bool = True,
    ) -> VMInstance:
        """Upload an ES-module Worker. ``bindings`` follows the Workers metadata schema
        (e.g. ``{"type": "plain_text", "name": "ROUTER_URL", "text": "..."}``)."""
        metadata: dict[str, Any] = {
            "main_module": main_module,
            "compatibility_date": compatibility_date
            or str(self.setting("compatibility_date", datetime.now(timezone.utc).date().isoformat())),
        }
        if bindings:
            metadata["bindings"] = bindings
        files: list[tuple[str, tuple[str | None, bytes | str, str]]] = [
            ("metadata", (None, json.dumps(metadata), "application/json")),
            (main_module, (main_module, code.encode("utf-8"), "application/javascript+module")),
        ]
        for filename, source in (extra_modules or {}).items():
            files.append((filename, (filename, source.encode("utf-8"), "application/javascript+module")))
        result = await self.cf_call("PUT", self.cf_account_url(f"workers/scripts/{script_name}"), files=files)
        if enable_workers_dev:
            await self.cf_call(
                "POST",
                self.cf_account_url(f"workers/scripts/{script_name}/subdomain"),
                json={"enabled": True},
            )
        script = dict(result or {})
        script.setdefault("id", script_name)
        return self._to_instance(script, await self._workers_subdomain())

    async def delete_script(self, script_name: str) -> None:
        await self.cf_call("DELETE", self.cf_account_url(f"workers/scripts/{script_name}"))

    async def quota(self) -> QuotaInfo:
        limit = next(
            (lim.limit for lim in self.context.limits if lim.metric == "requests" and lim.window == "day"),
            None,
        )
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        payload = {
            "query": _GRAPHQL_QUERY,
            "variables": {
                "accountTag": self.require_secret("account_id"),
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
        response = await self.request(
            "POST", f"{self.cf_base()}/graphql", json=payload, headers=self.cf_headers()
        )
        body = response.json()
        if body.get("errors"):
            raise ProviderError(f"GraphQL: {body['errors']}", provider=self.name)
        accounts = ((body.get("data") or {}).get("viewer") or {}).get("accounts") or []
        groups = accounts[0].get("workersInvocationsAdaptive") if accounts else []
        per_script: dict[str, int] = {}
        total = 0
        for group in groups or []:
            requests = int((group.get("sum") or {}).get("requests") or 0)
            script = (group.get("dimensions") or {}).get("scriptName") or "?"
            per_script[script] = per_script.get(script, 0) + requests
            total += requests
        return QuotaInfo(
            provider=self.name,
            used=float(total),
            limit=limit,
            unit="requests",
            window="day",
            reset_at=start + timedelta(days=1),
            source="provider",
            extra={"per_script": per_script},
        )
