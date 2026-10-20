"""Shared-state backend (Upstash Redis REST, faked in memory): quota, registry overrides, events, fallback."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.config import Settings
from core.events import EventBus, EventType
from core.interfaces import ChatMessage, ChatRequest
from core.quota import QuotaStore, RedisQuotaStore, build_quota_store
from core.registry import PluginRegistry
from core.router import SmartRouter
from core.shared_state import (
    TOKEN_SECRET,
    URL_SECRET,
    RedisOverridesStore,
    SharedStateError,
    UpstashRest,
    shared_state_from_settings,
)
from tests.core.conftest import FakeClock, llm
from tests.core.fake_upstash import TOKEN, URL, FakeUpstash

NOW = datetime(2026, 10, 20, 12, 30, 15, tzinfo=timezone.utc)


@pytest.fixture
def redis() -> FakeUpstash:
    return FakeUpstash()


@pytest.fixture
def client(redis: FakeUpstash) -> UpstashRest:
    return UpstashRest(URL, TOKEN, transport=redis.transport())


# --------------------------------------------------------------------------- client


async def test_client_command_pipeline_and_errors(client: UpstashRest, redis: FakeUpstash) -> None:
    assert await client.command("SET", "k", "v") == "OK"
    assert await client.pipeline([["GET", "k"], ["MGET", "k", "nope"]]) == ["v", ["v", None]]
    assert [r.url.path for r in redis.requests] == ["/", "/pipeline"]
    with pytest.raises(SharedStateError, match="unknown command"):
        await client.command("FLUSHALL")
    redis.fail = True
    with pytest.raises(SharedStateError, match="HTTP 500"):
        await client.command("GET", "k")
    await client.aclose()


# --------------------------------------------------------------------------- quota


async def test_redis_quota_windows_one_round_trip_per_add(client: UpstashRest, redis: FakeUpstash) -> None:
    store = await RedisQuotaStore(client).open()
    windows = ("minute", "hour", "day", "month", "total")
    await store.add("groq", "requests", 1, windows, now=NOW)
    await store.add("groq", "requests", 2, windows, now=NOW)
    assert len(redis.requests) == 2  # one /pipeline call per record
    assert all(r.url.path == "/pipeline" for r in redis.requests)
    for window in windows:
        assert await store.used("groq", "requests", window, now=NOW) == 3.0

    day_key = "omnifree:quota:groq:requests:day:2026-10-20"
    assert redis.data[day_key] == "3.0"
    assert redis.ttl[day_key] > 86400
    assert redis.ttl["omnifree:quota:groq:requests:minute:2026-10-20T12:30"] >= 3600
    assert "omnifree:quota:groq:requests:total:all" not in redis.ttl  # total never expires

    later = NOW + timedelta(minutes=1)
    assert await store.used("groq", "requests", "minute", now=later) == 0.0
    assert await store.used("groq", "requests", "hour", now=later) == 3.0
    next_day = NOW + timedelta(days=1)
    assert await store.used("groq", "requests", "day", now=next_day) == 0.0
    assert await store.used("groq", "requests", "month", now=next_day) == 3.0
    assert await store.used("groq", "requests", "total", now=next_day) == 3.0


async def test_redis_quota_snapshot_reset_and_float_amounts(client: UpstashRest) -> None:
    store = RedisQuotaStore(client)
    await store.add("groq", "tokens", 10.5, ("day",), now=NOW)
    await store.add("groq", "requests", 1, ("day", "month"), now=NOW)
    await store.add("gemini", "requests", 4, ("day",), now=NOW)

    rows = await store.snapshot("groq", now=NOW)
    assert {(r["metric"], r["window"], r["used"]) for r in rows} == {
        ("requests", "day", 1.0), ("requests", "month", 1.0), ("tokens", "day", 10.5),
    }
    assert all(r["provider"] == "groq" for r in rows)
    assert next(r for r in rows if r["window"] == "day")["reset_at"] == datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert {r["provider"] for r in await store.snapshot(now=NOW)} == {"groq", "gemini"}
    assert await store.snapshot("groq", now=NOW + timedelta(days=40)) == []  # day and month rolled over
    assert all(r["window"] != "day" for r in await store.snapshot("groq", now=NOW + timedelta(days=1)))

    await store.reset("groq", "tokens")
    assert {r["metric"] for r in await store.snapshot("groq", now=NOW)} == {"requests"}
    await store.reset("groq")
    assert await store.snapshot("groq", now=NOW) == []
    assert await store.used("gemini", "requests", "day", now=NOW) == 4.0


async def test_redis_quota_is_fail_open(client: UpstashRest, redis: FakeUpstash, caplog: pytest.LogCaptureFixture) -> None:
    store = RedisQuotaStore(client)
    redis.fail = True
    with caplog.at_level(logging.WARNING, logger="omnifree.quota"):
        await store.add("groq", "requests", 1, ("day",), now=NOW)
        assert await store.used("groq", "requests", "day", now=NOW) == 0.0
        assert await store.snapshot("groq", now=NOW) == []
    assert "quota counter update lost" in caplog.text


async def test_router_enforces_limits_from_shared_counters(write_yaml, client: UpstashRest) -> None:
    """Two routers (two serverless instances) share the daily cap through Redis."""
    path = write_yaml({"a": llm(10, limits=[{"metric": "requests", "window": "day", "limit": 2}]), "b": llm(20)})
    clock = FakeClock()
    routers = [
        SmartRouter(PluginRegistry.from_file(path, secret_lookup={"FAKE_KEY": "k"}.get), RedisQuotaStore(client), clock=clock)
        for _ in range(2)
    ]
    for r in routers:
        await r.start()
    req = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
    via = [(await routers[i % 2].chat(req)).routed_via for i in range(3)]
    assert via == ["a", "a", "b"]
    for r in routers:
        await r.close()


# --------------------------------------------------------------------------- registry overrides


async def test_overrides_persist_in_redis_not_yaml_and_apply_on_load(
    write_yaml, client: UpstashRest, redis: FakeUpstash
) -> None:
    path = write_yaml({"a": llm(10, settings={"default_model": "m1", "drop": 1}), "b": llm(20), "c": llm(30)})
    before = path.read_text(encoding="utf-8")
    store = RedisOverridesStore(client)
    reg = PluginRegistry.from_file(path, secret_lookup={"FAKE_KEY": "k"}.get, overrides=store)
    await reg.apply_overrides()  # empty store: no-op

    await reg.set_enabled("b", False)
    await reg.reorder("llm", ["c", "a"])
    await reg.update_settings("a", {"default_model": "m2", "drop": None})
    await reg.set_priority("b", 5)
    assert path.read_text(encoding="utf-8") == before  # YAML never rewritten

    doc = json.loads(redis.data["omnifree:registry:overrides"])
    assert doc["plugins"]["b"] == {"enabled": False, "priority": 5}
    assert doc["plugins"]["a"]["settings"] == {"default_model": "m2", "drop": None}

    # A fresh instance (cold start) sees the same state once overrides are applied.
    fresh = PluginRegistry.from_file(path, secret_lookup={"FAKE_KEY": "k"}.get, overrides=RedisOverridesStore(client))
    assert fresh.chain("llm") == ["a", "b", "c"]  # YAML only
    await SmartRouter(fresh, RedisQuotaStore(client)).start()  # start() applies overrides
    assert fresh.chain("llm") == ["c", "a"]
    assert fresh.spec("b").enabled is False and fresh.spec("b").priority == 5
    assert fresh.spec("a").settings == {"default_model": "m2"}

    # reload() re-reads the YAML and layers the overrides again.
    await reg.reload()
    assert reg.chain("llm") == ["c", "a"]
    assert reg.spec("a").settings == {"default_model": "m2"}


async def test_overrides_unavailable_keeps_yaml_and_mutation_reports_503(
    write_yaml, client: UpstashRest, redis: FakeUpstash
) -> None:
    path = write_yaml({"a": llm(10), "b": llm(20)})
    reg = PluginRegistry.from_file(path, secret_lookup={"FAKE_KEY": "k"}.get, overrides=RedisOverridesStore(client))
    redis.fail = True
    await reg.apply_overrides()  # logged, not raised
    assert reg.chain("llm") == ["a", "b"]
    with pytest.raises(SharedStateError):
        await reg.set_enabled("a", False)


async def test_local_backend_still_writes_yaml(write_yaml) -> None:
    path = write_yaml({"a": llm(10), "b": llm(20)})
    reg = PluginRegistry.from_file(path, secret_lookup={"FAKE_KEY": "k"}.get)
    await reg.apply_overrides()
    await reg.set_enabled("a", False)
    assert "enabled: false" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- events


async def test_recent_events_shared_between_two_buses(client: UpstashRest, redis: FakeUpstash) -> None:
    bus_a, bus_b = EventBus(shared=client), EventBus(shared=client)
    e1 = bus_a.publish(EventType.REQUEST_SUCCEEDED, provider="groq", latency_ms=12)
    e2 = bus_b.publish(EventType.QUOTA_UPDATED, provider="groq", amount=1)
    e3 = bus_a.publish(EventType.PLUGIN_DISABLED, provider="b")
    await bus_a.flush()
    await bus_b.flush()
    assert len(redis.data["omnifree:events"]) == 3

    for bus in (bus_a, bus_b):
        got = await bus.fetch_recent(10)
        assert [e.id for e in got] == [e1.id, e2.id, e3.id]  # merged, deduped, oldest first
    assert [e.id for e in await bus_b.fetch_recent(10, ["router.*"])] == [e1.id]
    assert [e.id for e in await bus_b.fetch_recent(1)] == [e3.id]
    assert (await bus_b.fetch_recent(10))[0].data == {"latency_ms": 12}
    assert bus_b.recent(10) == [e2]  # the sync view stays per instance


async def test_shared_event_list_is_capped(client: UpstashRest, redis: FakeUpstash) -> None:
    bus = EventBus(history=5, shared=client)
    for i in range(12):
        bus.publish(EventType.HEALTH_CHECKED, n=i)
    await bus.flush()
    assert len(redis.data["omnifree:events"]) == 5
    assert [e.data["n"] for e in await EventBus(history=5, shared=client).fetch_recent(100)] == [7, 8, 9, 10, 11]


async def test_event_publish_survives_redis_failure(
    client: UpstashRest, redis: FakeUpstash, caplog: pytest.LogCaptureFixture
) -> None:
    redis.fail = True
    bus = EventBus(shared=client)
    with caplog.at_level(logging.WARNING, logger="omnifree.events"):
        event = bus.publish(EventType.REQUEST_SUCCEEDED, provider="groq")
        await bus.flush()
        assert [e.id for e in await bus.fetch_recent(10)] == [event.id]  # local memory still answers
    assert "dropped 1 event" in caplog.text


def test_event_ids_are_unique_across_processes_and_js_safe() -> None:
    event = EventBus().publish(EventType.HEALTH_CHECKED)
    assert 10**15 < event.id < 2**53


# --------------------------------------------------------------------------- selection / fallback


def _settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(root_dir=tmp_path, vault_path=tmp_path / "no.vault", **kw)  # type: ignore[arg-type]


def test_backend_defaults_to_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIFREE_STATE_BACKEND", raising=False)
    settings = _settings(tmp_path)
    assert settings.state_backend == "local"
    assert shared_state_from_settings(settings) is None
    assert isinstance(build_quota_store(settings, None), QuotaStore)


def test_upstash_without_secrets_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNIFREE_STATE_BACKEND", "UpStash")
    monkeypatch.delenv(URL_SECRET, raising=False)
    monkeypatch.delenv(TOKEN_SECRET, raising=False)
    settings = _settings(tmp_path)
    assert settings.state_backend == "upstash"
    with caplog.at_level(logging.WARNING, logger="omnifree.state"):
        assert shared_state_from_settings(settings) is None
    assert "falling back to local" in caplog.text


def test_upstash_with_secrets_builds_shared_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_SECRET, URL + "/")
    monkeypatch.setenv(TOKEN_SECRET, TOKEN)
    client = shared_state_from_settings(_settings(tmp_path, state_backend="upstash"))
    assert isinstance(client, UpstashRest) and client.url == URL
    assert isinstance(build_quota_store(_settings(tmp_path), client), RedisQuotaStore)
