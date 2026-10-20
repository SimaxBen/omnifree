"""/llm: chat (JSON), stream (SSE), models, quotas, embeddings. Sets ``X-Routed-Via``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from core.interfaces import (
    ChatChunk,
    ChatRequest,
    EmbeddingRequest,
    ProviderError,
    ProviderKind,
    QuotaInfo,
)
from hub_api.errors import error_body
from hub_api.state import HubState, get_state

__all__ = ["router", "ROUTED_VIA_HEADER"]

ROUTED_VIA_HEADER = "X-Routed-Via"

router = APIRouter(prefix="/llm", tags=["llm"])


def _quota_json(q: QuotaInfo) -> dict[str, Any]:
    return {**q.model_dump(mode="json"), "remaining": q.remaining, "fraction_used": q.fraction_used}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


@router.post("/chat")
async def chat(body: ChatRequest, response: Response, state: HubState = Depends(get_state)) -> Any:
    """Non-streaming chat through the fallback chain. ``stream: true`` is served as SSE."""
    if body.stream:
        return await stream(body, state)
    result = await state.router.chat(body)
    response.headers[ROUTED_VIA_HEADER] = result.routed_via
    return result.model_dump(mode="json")


@router.post("/stream")
async def stream(body: ChatRequest, state: HubState = Depends(get_state)) -> Response:
    """Server-Sent Events: ``event: chunk`` (ChatChunk JSON) ..., then ``event: done`` or ``event: error``.

    The first chunk is awaited before the response starts so that failover
    happens before any byte is sent and ``X-Routed-Via`` can be set as a header.
    """
    request = body.model_copy(update={"stream": True})
    iterator = state.router.stream(request).__aiter__()
    try:
        first: ChatChunk | None = await iterator.__anext__()
    except StopAsyncIteration:
        first = None
    except ProviderError:
        await iterator.aclose()
        raise
    routed_via = first.routed_via if first is not None else ""

    async def events() -> AsyncIterator[bytes]:
        chunks = 0
        try:
            if first is not None:
                chunks += 1
                yield _sse("chunk", first.model_dump(mode="json"))
                async for chunk in iterator:
                    chunks += 1
                    yield _sse("chunk", chunk.model_dump(mode="json"))
            yield _sse("done", {"routed_via": routed_via, "chunks": chunks})
        except ProviderError as exc:
            yield _sse("error", error_body(exc))
        finally:
            await iterator.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            ROUTED_VIA_HEADER: routed_via,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models")
async def models(state: HubState = Depends(get_state)) -> dict[str, Any]:
    by_provider = await state.router.list_models()
    return {
        "providers": {name: [m.model_dump(mode="json") for m in items] for name, items in by_provider.items()},
        "chain": state.registry.chain(ProviderKind.LLM),
    }


@router.get("/quotas")
async def quotas(
    kind: ProviderKind = Query(ProviderKind.LLM),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    """Quota gauges per provider: provider-reported entry first, then router counters."""
    data = await state.router.quotas(kind)
    statuses = {s.name: s for s in await state.router.status(kind)}
    out: dict[str, Any] = {}
    for name, items in data.items():
        st = statuses.get(name)
        out[name] = {
            "quotas": [_quota_json(q) for q in items],
            "exhausted_until": st.exhausted_until.isoformat() if st and st.exhausted_until else None,
            "breaker": st.breaker if st else None,
        }
    return {"kind": str(kind), "providers": out}


@router.post("/embed")
async def embed(body: EmbeddingRequest, response: Response, state: HubState = Depends(get_state)) -> Any:
    result = await state.router.embed(body)
    response.headers[ROUTED_VIA_HEADER] = result.routed_via
    return result.model_dump(mode="json")
