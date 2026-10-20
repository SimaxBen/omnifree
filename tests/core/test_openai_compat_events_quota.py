from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from core.events import EventBus
from core.interfaces import (
    ChatMessage,
    ChatRequest,
    NotConfigured,
    PluginContext,
    ProviderAuthError,
    ProviderKind,
    ProviderUnavailable,
    QuotaExhausted,
)
from core.openai_compat import OpenAICompatibleAdapter
from core.quota import QuotaStore, window_reset_at, window_start


class _Compat(OpenAICompatibleAdapter):
    pass


def _adapter(handler, **settings) -> _Compat:
    ctx = PluginContext(
        name="acme",
        kind=ProviderKind.LLM,
        settings={"base_url": "https://api.acme.test/{region}/v1", "region": "eu", "default_model": "m1", **settings},
        secrets={"api_key": "sk-test"},
        http_transport=httpx.MockTransport(handler),
    )
    return _Compat(ctx)


def _req() -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content="hi")], temperature=0.2)


async def test_chat_body_headers_and_response() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-upstream": "groq"},
            json={"id": "c1", "model": "m1", "choices": [{"message": {"role": "assistant", "content": "yo"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
        )

    adapter = _adapter(handler, routed_via_header="x-upstream")
    resp = await adapter.chat(_req())
    assert seen["url"] == "https://api.acme.test/eu/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "m1" and seen["body"]["temperature"] == 0.2
    assert resp.content == "yo" and resp.usage.total_tokens == 5 and resp.routed_via == "acme:groq"
    await adapter.close()


async def test_stream_parses_sse() -> None:
    sse = (
        'data: {"model":"m1","choices":[{"delta":{"content":"he"}}]}\n\n'
        'data: {"model":"m1","choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}],"usage":{"total_tokens":9}}\n\n'
        "data: [DONE]\n\n"
    )
    adapter = _adapter(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    chunks = [c async for c in adapter.stream(_req())]
    assert "".join(c.delta for c in chunks) == "hello"
    assert chunks[-1].usage is not None and chunks[-1].usage.total_tokens == 9


async def test_error_mapping() -> None:
    adapter = _adapter(lambda r: httpx.Response(429, headers={"retry-after": "12"}, text="slow down"))
    with pytest.raises(QuotaExhausted) as info:
        await adapter.chat(_req())
    assert info.value.retry_after == 12 and info.value.status_code == 429
    with pytest.raises(ProviderAuthError):
        await _adapter(lambda r: httpx.Response(401, text="bad key")).chat(_req())
    with pytest.raises(ProviderUnavailable):
        await _adapter(lambda r: httpx.Response(503)).chat(_req())
    with pytest.raises(QuotaExhausted):
        _s = _adapter(lambda r: httpx.Response(429))
        async for _ in _s.stream(_req()):
            pass

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderUnavailable):
        await _adapter(boom).chat(_req())
    with pytest.raises(NotConfigured):
        await _adapter(lambda r: httpx.Response(200), base_url="https://x/{missing}").chat(_req())


async def test_list_models_and_health() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json={"data": [{"id": "m1", "owned_by": "acme"}, {"id": "m2"}]}))
    assert [m.id for m in await adapter.list_models()] == ["m1", "m2"]
    health = await adapter.health()
    assert health.ok and health.latency_ms is not None
    down = await _adapter(lambda r: httpx.Response(500)).health()
    assert not down.ok and "ProviderUnavailable" in down.detail


async def test_event_bus_patterns_history_and_overflow() -> None:
    bus = EventBus(history=3)
    all_sub = bus.subscribe()
    router_sub = bus.subscribe(["router.*"], maxsize=2)
    exact = bus.subscribe(["quota.exhausted"])
    for i in range(4):
        bus.publish("router.success", provider="a", i=i)
    bus.publish("quota.exhausted", provider="a")
    assert all_sub.queue.qsize() == 5
    assert router_sub.queue.qsize() == 2 and router_sub.dropped == 2
    assert (await router_sub.get(1)).data["i"] == 2
    assert (await exact.get(1)).type == "quota.exhausted"
    assert len(bus.recent(10)) == 3 and len(bus.recent(10, ["router.*"])) == 2
    async with bus.subscribe() as tmp:
        assert bus.subscriber_count == 4
    assert bus.subscriber_count == 3 and tmp.closed


async def test_quota_windows_and_store(tmp_path) -> None:
    t = datetime(2026, 1, 31, 23, 59, 30, tzinfo=timezone.utc)
    assert window_start("minute", t) == "2026-01-31T23:59"
    assert window_start("day", t) == "2026-01-31" and window_start("month", t) == "2026-01"
    assert window_reset_at("day", t) == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert window_reset_at("month", t) == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert window_reset_at("minute", t) == datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    assert window_reset_at("total", t) is None
    with pytest.raises(ValueError):
        window_start("week", t)
    now = datetime.now(timezone.utc)
    async with QuotaStore(tmp_path / "q.db") as store:
        await store.add("p", "requests", 1, ["day", "month", "minute"], now=now)
        await store.add("p", "requests", 2, ["day", "month"], now=now)
        assert await store.used("p", "requests", "day", now=now) == 3
        assert await store.used("p", "requests", "minute", now=now) == 1
        assert len(await store.snapshot("p", now=now)) == 3
        await store.reset("p", "requests")
        assert await store.used("p", "requests", "day", now=now) == 0
