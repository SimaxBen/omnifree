"""Mistral La Plateforme (OpenAI-compatible ``/v1``) with optional dedicated Codestral endpoint.

When ``settings.codestral_base_url`` and the optional secret ``codestral_api_key`` are both set,
requests for ``codestral-*`` models go to the Codestral endpoint with that key; otherwise every
model (Codestral included) goes to ``base_url`` with ``api_key``.
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator
from typing import ClassVar

from core.interfaces import Capability, ChatChunk, ChatRequest, ChatResponse, PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter

_CODESTRAL: contextvars.ContextVar[bool] = contextvars.ContextVar("omnifree_mistral_codestral", default=False)


class MistralAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Mistral AI",
        description="Mistral Large/Small, Codestral and mistral-embed (Experiment tier).",
        required_secrets=("api_key",),
        optional_secrets=("codestral_api_key",),
        docs_url="https://docs.mistral.ai/deployment/laplateforme/tier/",
    )
    capabilities: ClassVar[frozenset[str]] = PooledOpenAIAdapter.capabilities | {Capability.EMBED}

    def uses_codestral(self, request_model: str | None) -> bool:
        model = self.model_for(request_model)
        return (
            model.startswith("codestral")
            and bool(self.setting("codestral_base_url"))
            and bool(self.secret("codestral_api_key"))
        )

    def resolve_base_url(self) -> str:
        if _CODESTRAL.get():
            return str(self.setting("codestral_base_url")).rstrip("/")
        return super().resolve_base_url()

    def auth_headers(self) -> dict[str, str]:
        if _CODESTRAL.get():
            return {"Authorization": f"Bearer {self.require_secret('codestral_api_key')}"}
        return super().auth_headers()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        token = _CODESTRAL.set(self.uses_codestral(request.model))
        try:
            return await super().chat(request)
        finally:
            _CODESTRAL.reset(token)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        codestral = self.uses_codestral(request.model)
        token = _CODESTRAL.set(codestral)
        try:
            gen = super().stream(request)
            # Prime the generator inside the context so the HTTP request targets the right endpoint.
            first = await anext(gen, None)
        finally:
            _CODESTRAL.reset(token)
        if first is None:
            return
        yield first
        async for chunk in gen:
            yield chunk
