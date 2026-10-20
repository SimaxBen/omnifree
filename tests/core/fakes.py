"""In-test fake adapters (test code only). Behaviour is scripted per provider name."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    ComputeAdapter,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    LLMAdapter,
    ModelInfo,
    PluginMeta,
    ProviderError,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
    Usage,
    VMInstance,
)

# provider name -> list of outcomes consumed one per call; last one repeats.
# outcome: "ok" | "quota" | "down" | "slow" | "error" | "crash"
SCRIPTS: dict[str, list[str]] = {}
CALLS: dict[str, int] = defaultdict(int)


def reset() -> None:
    SCRIPTS.clear()
    CALLS.clear()


def _next(name: str) -> str:
    CALLS[name] += 1
    script = SCRIPTS.get(name) or ["ok"]
    return script.pop(0) if len(script) > 1 else script[0]


async def _apply(name: str, outcome: str) -> None:
    if outcome == "quota":
        raise QuotaExhausted("rate limited", provider=name, status_code=429, retry_after=60)
    if outcome == "down":
        raise ProviderUnavailable("503 upstream", provider=name, status_code=503)
    if outcome == "error":
        raise ProviderError("bad request", provider=name, status_code=400)
    if outcome == "crash":
        raise RuntimeError("adapter bug")
    if outcome == "slow":
        await asyncio.sleep(5)


class FakeLLM(LLMAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(display_name="Fake LLM", required_secrets=("api_key",))
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {Capability.CHAT, Capability.STREAM, Capability.LIST_MODELS, Capability.EMBED}
    )
    closed = 0

    async def chat(self, request: ChatRequest) -> ChatResponse:
        await _apply(self.name, _next(self.name))
        return ChatResponse(
            model=request.model or self.setting("default_model", "fake-model"),
            content=f"hello from {self.name}",
            usage=Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        await _apply(self.name, _next(self.name))
        for word in ("a", "b", "c"):
            yield ChatChunk(delta=word, model="fake-model")
        yield ChatChunk(finish_reason="stop", usage=Usage(total_tokens=5))

    async def list_models(self) -> list[ModelInfo]:
        live = self.setting("live_models", self.setting("models", []))
        return [ModelInfo(id=m, provider=self.name) for m in live]

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        await _apply(self.name, _next(self.name))
        return EmbeddingResponse(model="fake-embed", vectors=[[0.1, 0.2] for _ in request.input])

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: _apply(self.name, (SCRIPTS.get(self.name) or ["ok"])[0]))

    async def close(self) -> None:
        FakeLLM.closed += 1
        await super().close()


class ChatOnlyLLM(FakeLLM):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.CHAT})


class FakeStorage(StorageAdapter):
    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str | None = None, metadata: dict[str, str] | None = None) -> StoredObject:
        await _apply(self.name, _next(self.name))
        self.objects[key] = data
        return StoredObject(key=key, size=len(data), content_type=content_type, backend=self.name)

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ProviderError("not found", provider=self.name, status_code=404)
        return self.objects[key]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        return [StoredObject(key=k, size=len(v)) for k, v in self.objects.items() if k.startswith(prefix)][:limit]

    async def stat(self, key: str) -> StoredObject | None:
        data = self.objects.get(key)
        return None if data is None else StoredObject(key=key, size=len(data))

    async def quota(self) -> QuotaInfo:
        return QuotaInfo(
            provider=self.name, used=float(self.setting("used_bytes", 0)), limit=self.setting("capacity_bytes"),
            unit="bytes", window="total", source="provider",
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True, latency_ms=1.0)


class FakeCompute(ComputeAdapter):
    async def list_instances(self) -> list[VMInstance]:
        return [VMInstance(id="i-1", name="vm", provider=self.name, state="running")]

    async def status(self, instance_id: str) -> VMInstance:
        return VMInstance(id=instance_id, name="vm", provider=self.name, state="running")

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


NOT_AN_ADAPTER = object()
