"""OpenRouter (OpenAI-compatible) restricted to ``:free`` models.

With ``settings.free_only`` (default true) a request for a non-``:free`` model is refused locally
with ``ProviderError`` (HTTP 400 semantics) so no paid credit can ever be spent, and
``list_models()`` only returns ``:free`` models. OpenRouter's generic ``X-RateLimit-*`` headers
(reset in epoch milliseconds) feed ``quota()``; before any header has been seen, ``quota()``
reads the key's credit usage from ``GET /key``.

The ``:free`` catalog changes often (2026-10: the deepseek/llama/qwen ``:free`` variants are gone),
so models are always fetched live from ``GET /models`` and filtered to ids ending in ``:free``;
``settings.models`` is only the fallback used when that call fails (the router then serves the
YAML list). Free caps: 20 RPM, 50 requests/day (1,000/day after $10 of lifetime credit purchases).
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.interfaces import ChatRequest, ModelInfo, PluginMeta, ProviderError, QuotaInfo
from plugins.llm_pool._base import PooledOpenAIAdapter


class OpenRouterAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="OpenRouter (free models)",
        description="OpenRouter ':free' model variants only.",
        required_secrets=("api_key",),
        docs_url="https://openrouter.ai/docs/api-reference/limits",
    )

    @property
    def free_only(self) -> bool:
        return bool(self.setting("free_only", True))

    @staticmethod
    def is_free(model_id: str) -> bool:
        return model_id.endswith(":free")

    def build_body(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super().build_body(request, stream=stream)
        model = str(body.get("model", ""))
        if self.free_only and not self.is_free(model):
            raise ProviderError(
                f"model {model!r} is not a ':free' model and settings.free_only is set",
                provider=self.name,
                status_code=400,
            )
        return body

    async def list_models(self) -> list[ModelInfo]:
        models = await super().list_models()
        if self.free_only:
            models = [m for m in models if self.is_free(m.id)]
        return models

    async def quota(self) -> QuotaInfo:
        if self.rate_limits:
            return await super().quota()
        response = await self.request("GET", self.url("key_path", "/key"), headers=self.headers())
        try:
            data = (response.json() or {}).get("data") or {}
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc
        rate = data.get("rate_limit") or {}
        return QuotaInfo(
            provider=self.name,
            used=float(data.get("usage") or 0.0),
            limit=float(data["limit"]) if data.get("limit") is not None else None,
            unit="credits",
            window="total",
            source="provider",
            extra={
                "is_free_tier": data.get("is_free_tier"),
                "rate_limit_requests": rate.get("requests"),
                "rate_limit_interval": rate.get("interval"),
            },
        )
