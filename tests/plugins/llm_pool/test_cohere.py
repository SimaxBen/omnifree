from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.interfaces import (
    Capability,
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    NotConfigured,
    QuotaExhausted,
    RerankRequest,
)
from plugins.llm_pool.cohere import CohereAdapter
from tests.plugins.llm_pool.helpers import make, req

SETTINGS = {
    "base_url": "https://api.cohere.com",
    "default_model": "command-r-plus",
    "default_embedding_model": "embed-v4.0",
    "default_rerank_model": "rerank-v3.5",
}


def cohere() -> CohereAdapter:
    return make(CohereAdapter, "cohere", dict(SETTINGS))


async def test_chat_v2_mapping(mock: respx.MockRouter) -> None:
    route = mock.post("https://api.cohere.com/v2/chat").respond(
        200,
        json={
            "id": "c1",
            "finish_reason": "COMPLETE",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}]},
            "usage": {
                "billed_units": {"input_tokens": 5, "output_tokens": 2},
                "tokens": {"input_tokens": 70, "output_tokens": 2},
            },
        },
    )
    adapter = cohere()
    assert adapter.supports(Capability.RERANK) and adapter.supports(Capability.EMBED)
    request = ChatRequest(
        messages=[ChatMessage(role="system", content="be brief"), ChatMessage(role="user", content="hi")],
        top_p=0.9, stop=["\n\n"], max_tokens=50, tool_choice="required",
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
    )
    resp = await adapter.chat(request)
    assert resp.content == "Hi there" and resp.finish_reason == "stop" and resp.routed_via == "cohere"
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens, resp.usage.total_tokens) == (70, 2, 72)
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "command-r-plus" and body["p"] == 0.9 and body["stop_sequences"] == ["\n\n"]
    assert body["max_tokens"] == 50 and body["tool_choice"] == "REQUIRED" and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"


async def test_chat_tool_calls(mock: respx.MockRouter) -> None:
    calls = [{"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    mock.post("https://api.cohere.com/v2/chat").respond(
        200, json={"id": "c2", "finish_reason": "TOOL_CALL",
                   "message": {"role": "assistant", "tool_plan": "call f", "tool_calls": calls}}
    )
    resp = await cohere().chat(req())
    assert resp.tool_calls == calls and resp.finish_reason == "tool_calls" and resp.content is None


async def test_stream_events(mock: respx.MockRouter) -> None:
    events = [
        ("message-start", {"type": "message-start", "id": "s1", "delta": {"message": {"role": "assistant"}}}),
        ("content-start", {"type": "content-start", "index": 0, "delta": {"message": {"content": {"type": "text", "text": ""}}}}),
        ("content-delta", {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"text": "Bon"}}}}),
        ("content-delta", {"type": "content-delta", "index": 0, "delta": {"message": {"content": {"text": "jour"}}}}),
        ("tool-call-start", {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
            "id": "t1", "type": "function", "function": {"name": "f", "arguments": ""}}}}}),
        ("tool-call-delta", {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {
            "function": {"arguments": "{\"a\":1}"}}}}}),
        ("message-end", {"type": "message-end", "delta": {"finish_reason": "MAX_TOKENS",
                                                          "usage": {"tokens": {"input_tokens": 3, "output_tokens": 4}}}}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()
    route = mock.post("https://api.cohere.com/v2/chat").respond(200, content=body, headers={"content-type": "text/event-stream"})
    chunks = [c async for c in cohere().stream(req("command-a-03-2025"))]
    assert "".join(c.delta for c in chunks) == "Bonjour"
    tool_chunks = [c.tool_calls[0] for c in chunks if c.tool_calls]
    assert tool_chunks[0]["function"]["name"] == "f" and tool_chunks[1]["function"]["arguments"] == "{\"a\":1}"
    assert chunks[-1].finish_reason == "length" and chunks[-1].usage.total_tokens == 7
    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True and sent["model"] == "command-a-03-2025"


async def test_stream_429(mock: respx.MockRouter) -> None:
    mock.post("https://api.cohere.com/v2/chat").respond(429, headers={"retry-after": "60"}, text="trial limit")
    with pytest.raises(QuotaExhausted) as exc:
        [c async for c in cohere().stream(req())]
    assert exc.value.retry_after == 60


async def test_embed_and_rerank(mock: respx.MockRouter) -> None:
    embed = mock.post("https://api.cohere.com/v2/embed").respond(
        200, json={"id": "e", "embeddings": {"float": [[0.1, 0.2], [0.3, 0.4]]}, "meta": {"billed_units": {"input_tokens": 6}}}
    )
    rerank = mock.post("https://api.cohere.com/v2/rerank").respond(
        200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}
    )
    adapter = cohere()
    emb = await adapter.embed(EmbeddingRequest(input=["a", "b"], input_type="search_query"))
    assert emb.vectors == [[0.1, 0.2], [0.3, 0.4]] and emb.usage.total_tokens == 6 and emb.model == "embed-v4.0"
    sent = json.loads(embed.calls.last.request.content)
    assert sent == {"model": "embed-v4.0", "texts": ["a", "b"], "input_type": "search_query", "embedding_types": ["float"]}
    rr = await adapter.rerank(RerankRequest(query="q", documents=["x", "y"], top_n=2))
    assert [r.index for r in rr.results] == [1, 0] and rr.model == "rerank-v3.5"
    assert json.loads(rerank.calls.last.request.content)["top_n"] == 2


async def test_models_and_health(mock: respx.MockRouter) -> None:
    mock.get("https://api.cohere.com/v1/models").respond(
        200, json={"models": [{"name": "command-r-plus", "endpoints": ["chat"], "context_length": 128000}]}
    )
    adapter = cohere()
    models = await adapter.list_models()
    assert models[0].id == "command-r-plus" and models[0].context_window == 128000 and models[0].capabilities == ["chat"]
    assert (await adapter.health()).ok


async def test_errors(mock: respx.MockRouter) -> None:
    mock.post("https://api.cohere.com/v2/chat").mock(
        side_effect=[httpx.Response(429, text="You are using a Trial key, which is limited to 1000 API calls / month")]
    )
    with pytest.raises(QuotaExhausted):
        await cohere().chat(req())
    with pytest.raises(NotConfigured):
        await make(CohereAdapter, "cohere", dict(SETTINGS), secrets={}).chat(req())
    no_rerank = make(CohereAdapter, "cohere", {"base_url": "https://api.cohere.com"})
    with pytest.raises(NotConfigured):
        await no_rerank.rerank(RerankRequest(query="q", documents=["a"]))
