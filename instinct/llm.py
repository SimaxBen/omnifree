"""LLM access for the agent loop: FreeLLMAPI (OpenAI-compatible) with an optional fallback endpoint.

Reuses ``core.openai_compat.OpenAICompatibleAdapter`` so HTTP errors map to the
OmniFree exception hierarchy (429 -> QuotaExhausted, 5xx -> ProviderUnavailable ...).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from core.interfaces import (
    ChatMessage,
    ChatRequest,
    NotConfigured,
    PluginContext,
    ProviderError,
    ProviderKind,
)
from core.openai_compat import OpenAICompatibleAdapter
from instinct.config import InstinctSettings, resolve_secret

__all__ = ["AgentLLM", "LLMReply", "build_agent_llm"]

log = logging.getLogger("omnifree.instinct.llm")


@dataclass
class LLMReply:
    content: str
    routed_via: str
    model: str


class AgentLLM:
    """Ordered list of OpenAI-compatible endpoints; the next one is tried on any provider error."""

    def __init__(
        self,
        endpoints: list[OpenAICompatibleAdapter],
        *,
        temperature: float = 0.2,
        max_tokens: int = 700,
        json_mode: bool = False,
    ) -> None:
        if not endpoints:
            raise ValueError("at least one LLM endpoint is required")
        self.endpoints = endpoints
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.json_mode = json_mode

    async def complete(self, messages: list[dict[str, str]], model: str | None = None) -> LLMReply:
        errors: list[str] = []
        for index, endpoint in enumerate(self.endpoints):
            request = ChatRequest(
                messages=[ChatMessage.model_validate(m) for m in messages],
                # A per-task model only applies to the primary endpoint; fallbacks use their default.
                model=model if index == 0 else None,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"} if self.json_mode else None,
            )
            try:
                resp = await endpoint.chat(request)
            except ProviderError as exc:
                log.warning("LLM endpoint %s failed: %s", endpoint.name, exc)
                errors.append(f"{endpoint.name}: {exc}")
                continue
            return LLMReply(content=resp.content or "", routed_via=resp.routed_via or endpoint.name, model=resp.model)
        raise ProviderError("all LLM endpoints failed: " + " | ".join(errors), provider="instinct")

    async def close(self) -> None:
        for endpoint in self.endpoints:
            await endpoint.close()


def _endpoint(
    name: str,
    base_url: str,
    model: str,
    api_key: str | None,
    timeout_s: float,
    routed_via_header: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> OpenAICompatibleAdapter:
    settings: dict[str, object] = {
        "base_url": base_url,
        "default_model": model,
        "auth": "bearer" if api_key else "none",
    }
    if routed_via_header:
        settings["routed_via_header"] = routed_via_header
    context = PluginContext(
        name=name,
        kind=ProviderKind.LLM,
        settings=settings,
        secrets={"api_key": api_key} if api_key else {},
        timeout_s=timeout_s,
        http_transport=transport,
    )
    return OpenAICompatibleAdapter(context)


def build_agent_llm(
    settings: InstinctSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> AgentLLM:
    if not settings.llm_base_url:
        raise NotConfigured("INSTINCT_LLM_BASE_URL is empty", provider="instinct")
    endpoints = [
        _endpoint(
            "freellmapi",
            settings.llm_base_url,
            settings.llm_model,
            resolve_secret(settings.llm_api_key_name),
            settings.llm_timeout_s,
            settings.llm_routed_via_header,
            transport,
        )
    ]
    if settings.fallback_base_url:
        endpoints.append(
            _endpoint(
                "ollama", settings.fallback_base_url, settings.fallback_model, None,
                settings.llm_timeout_s, None, transport,
            )
        )
    return AgentLLM(
        endpoints,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        json_mode=settings.llm_json_mode,
    )
