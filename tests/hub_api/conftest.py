from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from core.config import Settings
from hub_api.app import create_app
from hub_api.settings import HubSettings
from tests.hub_api import fakes

TOKEN = "test-hub-token"
INSTINCT_TOKEN = "test-instinct-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

F = "tests.hub_api.fakes"

DEFAULT_PLUGINS: dict[str, Any] = {
    "llm_a": {
        "module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
        "description": "first LLM",
        "limits": [{"metric": "requests", "window": "day", "limit": 1000}],
        "settings": {"default_model": "a-model", "models": ["a-model"]},
    },
    "llm_b": {
        "module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 20,
        "settings": {"default_model": "b-model", "models": ["b-model"], "upstream": "groq"},
    },
    "llm_locked": {
        "module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 30, "enabled": False,
        "secrets": {"api_key": "OMNIFREE_TEST_MISSING_KEY"},
        "settings": {"default_model": "c-model"},
    },
    "store_small": {
        "module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 10,
        "settings": {"capacity_bytes": 1000},
    },
    "store_big": {
        "module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 20,
        "settings": {"capacity_bytes": 100000},
    },
    "sqldb": {"module": f"{F}:FakeSQLDB", "kind": "db", "group": "databases", "priority": 10, "settings": {"rows": 3}},
    "pihole": {"module": f"{F}:FakeDNS", "kind": "dns", "group": "network", "priority": 10},
    "tailscale": {"module": f"{F}:FakeTailnet", "kind": "compute", "group": "network", "priority": 10},
    "vm": {"module": f"{F}:FakePowerVM", "kind": "compute", "group": "oracle", "priority": 20},
}


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    fakes.reset()


@pytest.fixture
def plugins() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_PLUGINS)


class Hub:
    def __init__(self, client: TestClient, providers_path: Path) -> None:
        self.client = client
        self.providers_path = providers_path

    @property
    def app(self) -> Any:
        return self.client.app

    @property
    def state(self) -> Any:
        return self.client.app.state.hub

    def yaml(self) -> dict[str, Any]:
        return yaml.safe_load(self.providers_path.read_text(encoding="utf-8"))

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.get(url, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.post(url, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def put(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.put(url, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def patch(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.patch(url, headers={**AUTH, **kw.pop("headers", {})}, **kw)

    def delete(self, url: str, **kw: Any) -> httpx.Response:
        return self.client.delete(url, headers={**AUTH, **kw.pop("headers", {})}, **kw)


@pytest.fixture
def make_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Hub]]:
    monkeypatch.delenv("OMNIFREE_TEST_MISSING_KEY", raising=False)
    monkeypatch.setenv("INSTINCT_API_TOKEN", INSTINCT_TOKEN)
    for var in ("OMNIFREE_HUB_API_TOKEN", "HUB_API_TOKEN", "OMNIFREE_HUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    clients: list[TestClient] = []

    def _make(
        plugin_map: dict[str, Any] | None = None,
        *,
        token: str | None = TOKEN,
        instinct_handler: Callable[[httpx.Request], httpx.Response] | None = None,
        hub_overrides: dict[str, Any] | None = None,
    ) -> Hub:
        path = tmp_path / "providers.yaml"
        doc = {
            "version": 1,
            "defaults": {"timeout_s": 2.0, "quota_cooldown_s": 120.0},
            "plugins": plugin_map if plugin_map is not None else copy.deepcopy(DEFAULT_PLUGINS),
        }
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        settings = Settings(
            root_dir=tmp_path,
            providers_file=path,
            data_dir=tmp_path / "data",
            quota_db=tmp_path / "data" / "quota.db",
            vault_path=tmp_path / "no.vault",
            hub_api_token=token,
            cors_origins=["http://localhost:5173"],
            health_interval_s=0,
        )
        hub_settings = HubSettings(
            instinct_url="http://instinct.test",
            background_health=False,
            ws_ping_interval_s=5.0,
            max_upload_bytes=50_000,
            **(hub_overrides or {}),
        )
        transport = httpx.MockTransport(instinct_handler) if instinct_handler else None
        app = create_app(settings, hub_settings, instinct_transport=transport)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Hub(client, path)

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def hub(make_hub: Callable[..., Hub]) -> Hub:
    return make_hub()
