"""Cohere native v2 API (trial key: ~1000 calls/month across all endpoints).

Endpoints: ``POST /v2/chat`` (JSON or SSE stream), ``POST /v2/embed``, ``POST /v2/rerank``,
``GET /v1/models``. ``settings.base_url`` is the API root (``https://api.cohere.com``).

Mapping notes:

* OpenAI-style messages/tools are accepted by v2 nearly verbatim (roles system/user/assistant/tool,
  ``tool_calls`` and ``tool_call_id``); ``top_p`` maps to ``p`` and ``stop`` to ``stop_sequences``.
* The v2 response ``message.content`` is a list of ``{"type": "text", "text": ...}`` parts; the
  text parts are concatenated. Usage comes from ``usage.tokens`` (falling back to ``billed_units``).
* v2 stream events (``content-delta``, ``tool-call-start``, ``tool-call-delta``, ``message-end``)
  become :class:`ChatChunk` objects; tool-call deltas are emitted in OpenAI delta shape.
* ``embed`` uses ``input_type`` from the request (default ``settings.default_input_type`` or
  ``search_document``) and requests ``embedding_types: ["float"]``.
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
    PluginMeta,
    ProviderError,
    RerankRequest,
    RerankResponse,
    RerankResult,
    Usage,
    raise_for_response,
    translate_transport_error,
)
from plugins.llm_pool._base import RateLimitAwareMixin

_FINISH = {
    "COMPLETE": "stop",
    "STOP_SEQUENCE": "stop",
    "MAX_TOKENS": "length",
    "TOOL_CALL": "tool_calls",
    "ERROR": "error",
    "TIMEOUT": "error",
}


class CohereAdapter(RateLimitAwareMixin, LLMAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cohere",
        description="Cohere Command chat, Embed and Rerank through the native v2 API.",
        required_secrets=("api_key",),
        docs_url="https://docs.cohere.com/reference/chat",
    )
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {
            Capability.CHAT,
            Capability.STREAM,
            Capability.LIST_MODELS,
            Capability.TOOLS,
            Capability.EMBED,
            Capability.RERANK,
        }
    )

    # ------------------------------------------------------------------ helpers
    def root(self) -> str:
        return str(self.setting("base_url", "https://api.cohere.com")).rstrip("/")

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.require_secret('api_key')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **{str(k): str(v) for k, v in (self.setting("extra_headers") or {}).items()},
        }

    def model_for(self, requested: str | None, setting: str = "default_model") -> str:
        if requested and requested != "auto":
            return requested
        model = self.setting(setting)
        if not model:
            raise NotConfigured(f"settings.{setting} unset", provider=self.name)
        return str(model)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", self.root() + path, json=body, headers=self.headers())
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc
        if not isinstance(data, dict):
            raise ProviderError("unexpected response shape", provider=self.name)
        return data

    @staticmethod
    def _message(msg: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in msg.items() if v is not None and k != "name"}
        if out.get("role") == "assistant" and out.get("tool_calls") and not out.get("content"):
            out.pop("content", None)
        return out

    def build_body(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_for(request.model),
            "messages": [self._message(m.model_dump(exclude_none=True)) for m in request.messages],
            "stream": stream,
        }
        optional = {
            "temperature": request.temperature,
            "p": request.top_p,
            "max_tokens": request.max_tokens,
            "stop_sequences": request.stop,
            "tools": request.tools,
            "response_format": request.response_format,
        }
        body.update({k: v for k, v in optional.items() if v is not None})
        if isinstance(request.tool_choice, str) and request.tool_choice.lower() in ("required", "none"):
            body["tool_choice"] = request.tool_choice.upper()
        body.update(request.extra)
        return body

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> Usage:
        data = data or {}
        tokens = data.get("tokens") or data.get("billed_units") or {}
        prompt = int(tokens.get("input_tokens") or 0)
        completion = int(tokens.get("output_tokens") or 0)
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)

    @staticmethod
    def _text(content: Any) -> str | None:
        if content is None:
            return None
        if isinstance(content, str):
            return content
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(parts) if parts else None

    # ------------------------------------------------------------------ chat
    async def chat(self, request: ChatRequest) -> ChatResponse:
        async def call() -> ChatResponse:
            t0 = time.perf_counter()
            body = self.build_body(request, stream=False)
            data = await self._post("/v2/chat", body)
            message = data.get("message") or {}
            reason = data.get("finish_reason")
            return ChatResponse(
                id=str(data.get("id", "")),
                model=body["model"],
                content=self._text(message.get("content")),
                role=message.get("role", "assistant"),
                finish_reason=_FINISH.get(str(reason), str(reason).lower() if reason else None),
                tool_calls=message.get("tool_calls"),
                usage=self._usage(data.get("usage")),
                routed_via=self.name,
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        return await self.guarded(call)

    async def _stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        body = self.build_body(request, stream=True)
        model = body["model"]
        headers = {**self.headers(), "Accept": "text/event-stream"}
        try:
            async with self.http.stream("POST", self.root() + "/v2/chat", json=body, headers=headers) as response:
                if not response.is_success:
                    await response.aread()
                    raise_for_response(response, self.name)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    chunk = self._chunk(event, model)
                    if chunk is not None:
                        yield chunk
                    if event.get("type") == "message-end":
                        break
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.name) from exc

    def _chunk(self, event: dict[str, Any], model: str) -> ChatChunk | None:
        kind = event.get("type")
        delta = event.get("delta") or {}
        message = delta.get("message") or {}
        index = int(event.get("index") or 0)
        if kind == "content-delta":
            content = message.get("content") or {}
            return ChatChunk(delta=str(content.get("text") or ""), model=model, routed_via=self.name)
        if kind == "tool-call-start":
            call = message.get("tool_calls") or {}
            function = call.get("function") or {}
            return ChatChunk(
                model=model,
                routed_via=self.name,
                tool_calls=[
                    {
                        "index": index,
                        "id": call.get("id"),
                        "type": call.get("type", "function"),
                        "function": {"name": function.get("name"), "arguments": function.get("arguments") or ""},
                    }
                ],
            )
        if kind == "tool-call-delta":
            function = (message.get("tool_calls") or {}).get("function") or {}
            return ChatChunk(
                model=model,
                routed_via=self.name,
                tool_calls=[{"index": index, "function": {"arguments": function.get("arguments") or ""}}],
            )
        if kind == "message-end":
            reason = delta.get("finish_reason")
            return ChatChunk(
                model=model,
                routed_via=self.name,
                finish_reason=_FINISH.get(str(reason), str(reason).lower() if reason else None),
                usage=self._usage(delta.get("usage")),
            )
        if kind == "error" or (kind is None and "error" in event):
            raise ProviderError(str(event.get("error") or event)[:500], provider=self.name)
        return None

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.preflight()
        async for chunk in self.guarded_stream(self._stream(request)):
            yield chunk

    # ------------------------------------------------------------------ models
    async def list_models(self) -> list[ModelInfo]:
        async def call() -> list[ModelInfo]:
            response = await self.request(
                "GET", self.root() + "/v1/models", params={"page_size": 1000}, headers=self.headers()
            )
            data = response.json() or {}
            out: list[ModelInfo] = []
            for item in data.get("models") or []:
                name = item.get("name")
                if not name:
                    continue
                out.append(
                    ModelInfo(
                        id=str(name),
                        provider=self.name,
                        owned_by="cohere",
                        context_window=item.get("context_length"),
                        capabilities=[str(e) for e in item.get("endpoints") or []],
                    )
                )
            return out

        return await self.guarded(call)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        async def call() -> EmbeddingResponse:
            model = self.model_for(request.model, "default_embedding_model")
            body = {
                "model": model,
                "texts": request.input,
                "input_type": request.input_type or self.setting("default_input_type", "search_document"),
                "embedding_types": ["float"],
            }
            data = await self._post("/v2/embed", body)
            embeddings = data.get("embeddings") or {}
            vectors = embeddings.get("float") if isinstance(embeddings, dict) else embeddings
            billed = ((data.get("meta") or {}).get("billed_units") or {}).get("input_tokens") or 0
            return EmbeddingResponse(
                model=model,
                vectors=vectors or [],
                usage=Usage(prompt_tokens=int(billed), total_tokens=int(billed)),
                routed_via=self.name,
            )

        return await self.guarded(call)

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        async def call() -> RerankResponse:
            model = self.model_for(request.model, "default_rerank_model")
            body: dict[str, Any] = {"model": model, "query": request.query, "documents": request.documents}
            if request.top_n is not None:
                body["top_n"] = request.top_n
            data = await self._post("/v2/rerank", body)
            results = [
                RerankResult(index=int(r["index"]), relevance_score=float(r["relevance_score"]))
                for r in data.get("results") or []
            ]
            return RerankResponse(model=model, results=results, routed_via=self.name)

        return await self.guarded(call)

    async def health(self) -> HealthStatus:
        return await self.probe(self.list_models)
