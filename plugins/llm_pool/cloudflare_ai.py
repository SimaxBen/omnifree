"""Cloudflare Workers AI through the OpenAI-compatible endpoint
``https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1``.

* Auth: ``Authorization: Bearer <api_key>`` (an API token with Workers AI permissions);
  ``{account_id}`` in ``base_url`` is filled from the ``account_id`` secret alias.
* ``list_models()`` uses the native catalogue ``GET /accounts/{id}/ai/models/search``
  (``settings.models_task``, default "Text Generation").
* ``quota()`` reports today's neurons from the GraphQL Analytics API when the token may read
  account analytics. The dataset and field names are settings (``neurons_dataset``, default
  ``aiInferenceAdaptiveGroups``; ``neurons_field``, default ``totalNeurons``) because they are not
  a stable contract; any failure falls back to the default (router counters).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from core.interfaces import Capability, ModelInfo, PluginMeta, ProviderError, QuotaInfo
from plugins.llm_pool._base import PooledOpenAIAdapter

_PAGE = 100


class CloudflareWorkersAIAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cloudflare Workers AI",
        description="Workers AI models (10k neurons/day free) via the OpenAI-compatible endpoint.",
        required_secrets=("account_id", "api_key"),
        docs_url="https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/",
    )
    capabilities: ClassVar[frozenset[str]] = PooledOpenAIAdapter.capabilities | {Capability.EMBED}

    def api_root(self) -> str:
        return str(self.setting("api_root", "https://api.cloudflare.com/client/v4")).rstrip("/")

    def account_url(self, path: str) -> str:
        return f"{self.api_root()}/accounts/{self.require_secret('account_id')}{path}"

    def _json(self, response: Any) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc
        return data if isinstance(data, dict) else {}

    async def list_models(self) -> list[ModelInfo]:
        task = str(self.setting("models_task", "Text Generation"))
        out: list[ModelInfo] = []
        page = 1
        while True:
            response = await self.request(
                "GET",
                self.account_url("/ai/models/search"),
                params={"task": task, "page": page, "per_page": _PAGE},
                headers=self.auth_headers(),
            )
            data = self._json(response)
            if data.get("success") is False:
                raise ProviderError(str(data.get("errors"))[:500], provider=self.name)
            rows = data.get("result") or []
            for row in rows:
                name = row.get("name")
                if not name:
                    continue
                context = next(
                    (
                        p.get("value")
                        for p in row.get("properties") or []
                        if p.get("property_id") == "context_window"
                    ),
                    None,
                )
                out.append(
                    ModelInfo(
                        id=str(name),
                        provider=self.name,
                        owned_by=str(row["source"]) if row.get("source") is not None else None,
                        context_window=int(context) if str(context or "").isdigit() else None,
                    )
                )
            total = int((data.get("result_info") or {}).get("total_count") or 0)
            if not rows or page * _PAGE >= total:
                break
            page += 1
        return out

    async def neurons_today(self) -> float:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dataset = str(self.setting("neurons_dataset", "aiInferenceAdaptiveGroups"))
        field = str(self.setting("neurons_field", "totalNeurons"))
        query = (
            "query ($account: String!, $start: Time!, $end: Time!) "
            "{ viewer { accounts(filter: {accountTag: $account}) { "
            + dataset
            + "(limit: 10000, filter: {datetime_geq: $start, datetime_leq: $end}) { sum { "
            + field
            + " } } } } }"
        )
        variables = {
            "account": self.require_secret("account_id"),
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        response = await self.request(
            "POST",
            f"{self.api_root()}/graphql",
            json={"query": query, "variables": variables},
            headers={**self.auth_headers(), "Content-Type": "application/json"},
        )
        data = self._json(response)
        if data.get("errors"):
            raise ProviderError(str(data["errors"])[:500], provider=self.name)
        accounts = ((data.get("data") or {}).get("viewer") or {}).get("accounts") or []
        groups = (accounts[0].get(dataset) if accounts else None) or []
        return float(sum(float((g.get("sum") or {}).get(field) or 0.0) for g in groups))

    async def quota(self) -> QuotaInfo:
        limit = next(
            (lim.limit for lim in self.context.limits if lim.metric == "neurons" and lim.window == "day"),
            None,
        )
        try:
            used = await self.neurons_today()
        except ProviderError:
            return await super().quota()
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return QuotaInfo(
            provider=self.name,
            used=used,
            limit=limit,
            unit="neurons",
            window="day",
            reset_at=midnight + timedelta(days=1),
            source="provider",
        )
