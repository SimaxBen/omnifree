"""Reusable base for OpenAI-compatible chat APIs (Groq, OpenRouter, Cerebras, NVIDIA NIM,
SambaNova, Mistral, Z.AI, Gemini's OpenAI endpoint, Cloudflare Workers AI /v1,
FreeLLMAPI, Ollama /v1 ...).

Subclasses usually only set ``meta``/``capabilities`` and maybe override
``resolve_base_url`` / ``auth_headers`` / ``build_body``. Settings consumed:

=====================  =========================================================
base_url               required, e.g. https://api.groq.com/openai/v1
                       may contain ``{placeholders}`` filled from secrets/settings
default_model          model used when ChatRequest.model is None/"auto"
default_embedding_model
chat_path              default "/chat/completions"
models_path            default "/models"
embeddings_path        default "/embeddings"
extra_headers          dict of static headers (e.g. OpenRouter HTTP-Referer)
auth                   "bearer" (default, secret alias ``api_key``) | "none"
stream_usage           send stream_options.include_usage (default false)
routed_via_header      response header holding the upstream provider name
=====================  =========================================================
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx

from core.interfaces import (
    Capability,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    LLMAdapter,
    ModelInfo,
    NotConfigured,
    ProviderError,
    Usage,
    raise_for_response,
    translate_transport_error,
)

__all__ = ["OpenAICompatibleAdapter"]


class _SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


class OpenAICompatibleAdapter(LLMAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {Capability.CHAT, Capability.STREAM, Capability.LIST_MODELS, Capability.TOOLS}
    )

    # ------------------------------------------------------------------ hooks
    def resolve_base_url(self) -> str:
        template = str(self.require_setting("base_url"))
        values = _SafeFormat({**self.settings, **self.context.secrets})
        try:
            return template.format_map(values).rstrip("/")
        except KeyError as exc:
            raise NotConfigured(f"base_url needs '{exc.args[0]}'", provider=self.name) from exc

    def auth_headers(self) -> dict[str, str]:
        if str(self.setting("auth", "bearer")).lower() == "none":
            return {}
        return {"Authorization": f"Bearer {self.require_secret('api_key')}"}

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            **{str(k): str(v) for k, v in (self.setting("extra_headers") or {}).items()},
            **self.auth_headers(),
        }

    def model_for(self, request_model: str | None) -> str:
        if request_model and request_model != "auto":
            return request_model
        model = self.setting("default_model")
        if not model:
            raise NotConfigured("no model requested and settings.default_model unset", provider=self.name)
        return str(model)

    def build_body(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_for(request.model),
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": stream,
        }
        optional = {
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
            "stop": request.stop,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
            "user": request.user,
        }
        body.update({k: v for k, v in optional.items() if v is not None})
        if stream and self.setting("stream_usage", False):
            body["stream_options"] = {"include_usage": True}
        body.update(request.extra)
        return body

    def routed_via_from(self, response: httpx.Response) -> str:
        header = self.setting("routed_via_header")
        if header and response.headers.get(header):
            return f"{self.name}:{response.headers[header]}"
        return self.name

    # ------------------------------------------------------------------ helpers
    def url(self, path_setting: str, default: str) -> str:
        return self.resolve_base_url() + str(self.setting(path_setting, default))

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> Usage:
        if not data:
            return Usage()
        prompt = int(data.get("prompt_tokens") or 0)
        completion = int(data.get("completion_tokens") or 0)
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(data.get("total_tokens") or prompt + completion),
        )

    async def _post_json(self, path_setting: str, default: str, body: dict[str, Any]) -> tuple[dict[str, Any], httpx.Response]:
        response = await self.request("POST", self.url(path_setting, default), json=body, headers=self.headers())
        try:
            return response.json(), response
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc

    # ------------------------------------------------------------------ LLMAdapter
    async def chat(self, request: ChatRequest) -> ChatResponse:
        t0 = time.perf_counter()
        data, response = await self._post_json("chat_path", "/chat/completions", self.build_body(request, stream=False))
        if "error" in data and not data.get("choices"):
            raise ProviderError(str(data["error"])[:500], provider=self.name)
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        return ChatResponse(
            id=str(data.get("id", "")),
            model=str(data.get("model") or self.model_for(request.model)),
            content=message.get("content"),
            role=message.get("role", "assistant"),
            finish_reason=choices[0].get("finish_reason"),
            tool_calls=message.get("tool_calls"),
            usage=self._usage(data.get("usage")),
            routed_via=self.routed_via_from(response),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        body = self.build_body(request, stream=True)
        url = self.url("chat_path", "/chat/completions")
        try:
            async with self.http.stream("POST", url, json=body, headers=self.headers()) as response:
                if not response.is_success:
                    await response.aread()
                    raise_for_response(response, self.name)
                routed_via = self.routed_via_from(response)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if "error" in data and not data.get("choices"):
                        raise ProviderError(str(data["error"])[:500], provider=self.name)
                    choice = (data.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    usage = self._usage(data["usage"]) if data.get("usage") else None
                    yield ChatChunk(
                        delta=delta.get("content") or "",
                        model=str(data.get("model") or body["model"]),
                        finish_reason=choice.get("finish_reason"),
                        tool_calls=delta.get("tool_calls"),
                        usage=usage,
                        routed_via=routed_via,
                    )
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.name) from exc

    async def list_models(self) -> list[ModelInfo]:
        response = await self.request("GET", self.url("models_path", "/models"), headers=self.headers())
        data = response.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        out: list[ModelInfo] = []
        for item in items or []:
            if isinstance(item, str):
                out.append(ModelInfo(id=item, provider=self.name))
                continue
            model_id = item.get("id") or item.get("name")
            if not model_id:
                continue
            out.append(
                ModelInfo(
                    id=str(model_id),
                    provider=self.name,
                    owned_by=item.get("owned_by"),
                    context_window=item.get("context_window") or item.get("context_length"),
                )
            )
        return out

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        if not self.supports(Capability.EMBED):
            raise self._unsupported(Capability.EMBED)
        model = request.model or self.setting("default_embedding_model")
        if not model:
            raise NotConfigured("settings.default_embedding_model unset", provider=self.name)
        data, response = await self._post_json(
            "embeddings_path", "/embeddings", {"model": model, "input": request.input}
        )
        rows = sorted(data.get("data", []), key=lambda r: r.get("index", 0))
        return EmbeddingResponse(
            model=str(data.get("model") or model),
            vectors=[r["embedding"] for r in rows],
            usage=self._usage(data.get("usage")),
            routed_via=self.routed_via_from(response),
        )

    async def health(self) -> HealthStatus:
        return await self.probe(self.list_models)
