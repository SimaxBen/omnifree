"""Hub wired to the Upstash shared-state backend (faked in memory): overrides, quota, /events/recent."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from core.config import Settings
from core.events import EventBus, EventType
from core.quota import RedisQuotaStore
from core.shared_state import TOKEN_SECRET, URL_SECRET, UpstashRest
from hub_api.app import create_app
from hub_api.settings import HubSettings
from tests.core.fake_upstash import TOKEN, URL, FakeUpstash
from tests.hub_api.conftest import AUTH, DEFAULT_PLUGINS


@pytest.fixture
def redis() -> FakeUpstash:
    return FakeUpstash()


@pytest.fixture
def providers(tmp_path: Path) -> Path:
    path = tmp_path / "providers.yaml"
    doc = {"version": 1, "defaults": {"timeout_s": 2.0}, "plugins": copy.deepcopy(DEFAULT_PLUGINS)}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _client(tmp_path: Path, providers: Path, redis: FakeUpstash) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        providers_file=providers,
        data_dir=tmp_path / "data",
        quota_db=tmp_path / "data" / "quota.db",
        vault_path=tmp_path / "no.vault",
        hub_api_token=AUTH["Authorization"].removeprefix("Bearer "),
        health_interval_s=0,
        state_backend="upstash",
    )
    hub = HubSettings(instinct_url="http://instinct.test", background_health=False)
    return TestClient(create_app(settings, hub, state_transport=redis.transport()))


@pytest.fixture
def shared_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_SECRET, URL)
    monkeypatch.setenv(TOKEN_SECRET, TOKEN)


@pytest.mark.usefixtures("shared_env")
def test_hub_uses_shared_state_end_to_end(tmp_path: Path, providers: Path, redis: FakeUpstash) -> None:
    before = providers.read_text(encoding="utf-8")
    with _client(tmp_path, providers, redis) as client:
        state = client.app.state.hub
        assert isinstance(state.router.quota, RedisQuotaStore)
        assert client.post("/plugins/llm_a/disable", headers=AUTH).status_code == 200
        r = client.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert r.status_code == 200, r.text
    assert providers.read_text(encoding="utf-8") == before
    assert json.loads(redis.data["omnifree:registry:overrides"])["plugins"]["llm_a"] == {"enabled": False}
    assert any(k.startswith("omnifree:quota:llm_b:requests:day:") for k in redis.data)
    pushed = [json.loads(e)["type"] for e in redis.data["omnifree:events"]]
    assert "registry.disabled" in pushed

    # Another "instance" (cold start): overrides applied, events of the first instance visible.
    with _client(tmp_path, providers, redis) as client:
        assert client.app.state.hub.registry.spec("llm_a").enabled is False
        types = [e["type"] for e in client.get("/events/recent?types=registry.*", headers=AUTH).json()]
        assert "registry.disabled" in types


@pytest.mark.usefixtures("shared_env")
def test_recent_events_include_other_instances(tmp_path: Path, providers: Path, redis: FakeUpstash) -> None:
    import asyncio

    other = EventBus(shared=UpstashRest(URL, TOKEN, transport=redis.transport()))

    async def _publish() -> int:
        event = other.publish(EventType.BREAKER_OPENED, provider="llm_b")
        await other.flush()
        return event.id

    event_id = asyncio.run(_publish())
    with _client(tmp_path, providers, redis) as client:
        events = client.get("/events/recent", headers=AUTH).json()
    assert event_id in [e["id"] for e in events]


def test_missing_secrets_fall_back_to_local(
    tmp_path: Path, providers: Path, redis: FakeUpstash, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(URL_SECRET, raising=False)
    monkeypatch.delenv(TOKEN_SECRET, raising=False)
    with _client(tmp_path, providers, redis) as client:
        state = client.app.state.hub
        assert state.shared_state is None and state.bus.shared is None
        assert client.post("/plugins/llm_a/disable", headers=AUTH).status_code == 200
    assert redis.requests == []
    assert yaml.safe_load(providers.read_text(encoding="utf-8"))["plugins"]["llm_a"]["enabled"] is False
