from __future__ import annotations

import pytest

from core.events import EventType
from core.interfaces import (
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    NoProviderAvailable,
    ProviderError,
    ProviderKind,
)
from core.quota import QuotaStore
from core.router import BreakerState, CircuitBreaker
from tests.core import fakes
from tests.core.conftest import llm, storage


def req(**kw) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content="hi")], **kw)


async def test_happy_path_routes_to_highest_priority(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    resp = await router.chat(req())
    assert resp.routed_via == "a"
    assert resp.content == "hello from a"
    assert resp.attempts == []
    assert fakes.CALLS == {"a": 1}
    usage = {(q.unit, q.window): q.used for q in await router.usage("a")}
    assert usage[("requests", "day")] == 1
    assert usage[("tokens", "month")] == 7


async def test_failover_on_quota_exhausted_and_cooldown(write_yaml, make_router, clock) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    sub = router.bus.subscribe(["router.*", "quota.exhausted"])
    fakes.SCRIPTS["a"] = ["quota", "ok"]
    resp = await router.chat(req())
    assert resp.routed_via == "b"
    assert resp.attempts and resp.attempts[0].startswith("a:quota")
    types = [e.type for e in router.bus.recent(50)]
    assert EventType.QUOTA_EXHAUSTED in types and EventType.FAILOVER in types
    # within retry_after (60s) "a" is skipped without being called
    resp2 = await router.chat(req())
    assert resp2.routed_via == "b" and fakes.CALLS["a"] == 1
    assert "cooling down" in resp2.attempts[0]
    clock.advance(61)
    assert (await router.chat(req())).routed_via == "a"
    assert sub.queue.qsize() > 0


async def test_failover_on_unavailable_timeout_and_unexpected_errors(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20), "c": llm(30), "d": llm(40)}))
    fakes.SCRIPTS.update({"a": ["down"], "b": ["slow"], "c": ["crash"]})
    resp = await router.chat(req())
    assert resp.routed_via == "d"
    outcomes = [a.split(" ")[0] for a in resp.attempts]
    assert outcomes == ["a:unavailable", "b:timeout", "c:unavailable"]


async def test_generic_error_fails_over_without_tripping_breaker(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    fakes.SCRIPTS["a"] = ["error"]
    for _ in range(3):
        assert (await router.chat(req())).routed_via == "b"
    assert router.runtime("a").breaker.state is BreakerState.CLOSED


async def test_all_failed_raises_with_trail(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20, module="tests.core.missing:X")}))
    fakes.SCRIPTS["a"] = ["down"]
    with pytest.raises(NoProviderAvailable) as info:
        await router.chat(req())
    assert info.value.attempts[0].startswith("a:unavailable")
    assert info.value.attempts[1].startswith("b:not_configured")
    assert router.bus.recent(1)[0].type == EventType.ALL_FAILED


async def test_local_daily_quota_persists_in_sqlite(write_yaml, make_router, tmp_path) -> None:
    limits = [{"metric": "requests", "window": "day", "limit": 2}]
    path = write_yaml({"a": llm(10, limits=limits), "b": llm(20)})
    router = await make_router(path)
    assert [(await router.chat(req())).routed_via for _ in range(3)] == ["a", "a", "b"]
    assert fakes.CALLS["a"] == 2
    status = {s.name: s for s in await router.status("llm")}
    day = next(q for q in status["a"].usage if q.window == "day" and q.unit == "requests")
    assert day.used == 2 and day.limit == 2 and day.reset_at is not None
    assert status["a"].exhausted_until is not None
    # a fresh router on the same DB still sees the counters
    router2 = await make_router(path)
    assert (await router2.chat(req())).routed_via == "b"
    async with QuotaStore(tmp_path / "data" / "quota.db") as store:
        rows = await store.snapshot("a")
        assert any(r["window"] == "month" and r["used"] == 2 for r in rows if r["metric"] == "requests")


async def test_circuit_breaker_opens_then_half_open_recovers(write_yaml, make_router, clock) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    fakes.SCRIPTS["a"] = ["down", "down", "ok"]
    await router.chat(req())
    await router.chat(req())  # 2nd consecutive failure -> open (threshold 2)
    assert router.runtime("a").breaker.state is BreakerState.OPEN
    assert any(e.type == EventType.BREAKER_OPENED for e in router.bus.recent(100))
    resp = await router.chat(req())
    assert resp.routed_via == "b" and resp.attempts[0].startswith("a:circuit_open")
    assert fakes.CALLS["a"] == 2
    clock.advance(31)
    assert router.runtime("a").breaker.state is BreakerState.HALF_OPEN
    assert (await router.chat(req())).routed_via == "a"
    assert router.runtime("a").breaker.state is BreakerState.CLOSED
    assert any(e.type == EventType.BREAKER_CLOSED for e in router.bus.recent(100))
    assert any(e.type == EventType.BREAKER_HALF_OPEN for e in router.bus.recent(100))


def test_circuit_breaker_backoff_doubles_on_failed_trial() -> None:
    now = [0.0]
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=10, max_cooldown_s=25, clock=lambda: now[0])
    assert cb.allow()
    assert cb.record_failure() and cb.state is BreakerState.OPEN
    assert not cb.allow()
    now[0] = 10
    assert cb.allow() and not cb.allow()  # single half-open trial
    assert cb.record_failure() and cb.cooldown_s == 20
    now[0] = 29
    assert not cb.allow()
    now[0] = 30
    assert cb.allow()
    cb.record_failure()
    assert cb.cooldown_s == 25  # capped
    now[0] = 55
    assert cb.allow()
    assert cb.record_success() and cb.state is BreakerState.CLOSED and cb.cooldown_s == 10


async def test_pinned_provider_and_no_fallback(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    assert (await router.chat(req(provider="b"))).routed_via == "b"
    fakes.SCRIPTS["b"] = ["down"]
    assert (await router.chat(req(provider="b"))).routed_via == "a"
    with pytest.raises(NoProviderAvailable):
        await router.chat(req(provider="b", allow_fallback=False))


async def test_model_routing_prefers_hosting_provider(write_yaml, make_router) -> None:
    path = write_yaml(
        {
            "a": llm(10, settings={"default_model": "a-default", "models": ["a-model"]}),
            "b": llm(20, settings={"default_model": "b-default", "models": ["special"]}),
        }
    )
    router = await make_router(path)
    resp = await router.chat(req(model="special"))
    assert resp.routed_via == "b" and resp.model == "special"
    fakes.SCRIPTS["b"] = ["quota"]
    resp = await router.chat(req(model="special"))
    assert resp.routed_via == "a" and resp.model == "a-default"  # fell back to a's default model
    models = await router.list_models()
    assert [m.id for m in models["a"]] == ["a-model"]


async def test_live_model_list_extends_yaml_fallback_for_routing(write_yaml, make_router) -> None:
    path = write_yaml(
        {
            "a": llm(10, settings={"default_model": "a-default", "models": ["a-model"]}),
            "b": llm(20, settings={"default_model": "b-default", "models": ["old:free"],
                                   "live_models": ["old:free", "brand-new:free"]}),
        }
    )
    router = await make_router(path)
    resp = await router.chat(req(model="brand-new:free"))
    assert resp.routed_via == "a"  # unknown before the live list was fetched
    models = await router.list_models()
    assert [m.id for m in models["b"]] == ["old:free", "brand-new:free"]
    resp = await router.chat(req(model="brand-new:free"))
    assert resp.routed_via == "b" and resp.model == "brand-new:free"


async def test_stream_fails_over_before_first_chunk(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    fakes.SCRIPTS["a"] = ["quota"]
    chunks = [c async for c in router.stream(req(stream=True))]
    assert "".join(c.delta for c in chunks) == "abc"
    assert {c.routed_via for c in chunks} == {"b"}
    tokens = {(q.unit, q.window): q.used for q in await router.usage("b")}
    assert tokens[("tokens", "day")] == 5


async def test_capability_filter_skips_unsupported(write_yaml, make_router) -> None:
    path = write_yaml({"a": llm(10, module="tests.core.fakes:ChatOnlyLLM"), "b": llm(20)})
    router = await make_router(path)
    resp = await router.embed(EmbeddingRequest(input=["x", "y"]))
    assert resp.routed_via == "b" and len(resp.vectors) == 2


async def test_routed_via_keeps_upstream_suffix() -> None:
    from core.router import SmartRouter

    assert SmartRouter._routed_via("freellmapi", "freellmapi:groq") == "freellmapi:groq"
    assert SmartRouter._routed_via("freellmapi", "groq") == "freellmapi:groq"
    assert SmartRouter._routed_via("groq", "") == "groq"


async def test_storage_routes_by_free_capacity(write_yaml, make_router) -> None:
    path = write_yaml(
        {
            "small": storage(10, capacity=100, used=90),
            "big": storage(20, capacity=1000, used=100),
            "full": storage(5, capacity=50, used=50),
        }
    )
    router = await make_router(path)
    obj = await router.storage_put("docs/a.txt", b"hello", "text/plain")
    assert obj.backend == "big"
    assert await router.storage_get("docs/a.txt", "big") == b"hello"
    located = await router.storage_locate("docs/a.txt")
    assert located is not None and located.backend == "big"
    listing = await router.storage_list("docs/")
    assert [o.key for o in listing["big"]] == ["docs/a.txt"] and listing["small"] == []
    fakes.SCRIPTS["big"] = ["down"]
    obj2 = await router.storage_put("x", b"12345", provider=None)
    assert obj2.backend == "small"  # next most-free that fits; "full" never tried
    with pytest.raises(NoProviderAvailable):
        await router.storage_put("too-big", b"x" * 2000)
    await router.storage_delete("docs/a.txt", "big")
    with pytest.raises(ProviderError):
        await router.storage_get("docs/a.txt", "big")


async def test_health_and_status(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20, module="tests.core.nope:X"), "s": storage(1, 10)}))
    fakes.SCRIPTS["a"] = ["down"]
    health = {h.provider: h for h in await router.health_all()}
    assert health["a"].ok is False and health["s"].ok is True
    assert health["b"].ok is False and "PluginLoadError" in health["b"].detail
    statuses = {s.name: s for s in await router.status()}
    assert statuses["a"].last_health is not None
    assert statuses["b"].load_error
    quotas = await router.quotas(ProviderKind.STORAGE)
    assert quotas["s"][0].source == "provider"


async def test_registry_changes_apply_to_router_immediately(write_yaml, make_router) -> None:
    router = await make_router(write_yaml({"a": llm(10), "b": llm(20)}))
    await router.registry.set_enabled("a", False)
    assert (await router.chat(req())).routed_via == "b"
    await router.registry.set_enabled("a", True)
    await router.registry.reorder("llm", ["b", "a"])
    assert (await router.chat(req())).routed_via == "b"
