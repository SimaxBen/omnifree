from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.events import EventBus
from core.quota import QuotaStore
from core.registry import PluginRegistry
from core.router import SmartRouter
from tests.core import fakes


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        # Today 12:00 UTC: recent enough for QuotaStore pruning (wall clock),
        # far from window boundaries so advancing minutes never crosses a day.
        if start is None:
            noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
            start = noon.timestamp()
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def llm(priority: int, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "module": "tests.core.fakes:FakeLLM",
        "kind": "llm",
        "group": "llm_pool",
        "priority": priority,
        "secrets": {"api_key": "FAKE_KEY"},
        "settings": {"default_model": "fake-model"},
    }
    entry.update(extra)
    return entry


def storage(priority: int, capacity: int, used: int = 0, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "module": "tests.core.fakes:FakeStorage",
        "kind": "storage",
        "group": "storage",
        "priority": priority,
        "settings": {"capacity_bytes": capacity, "used_bytes": used},
    }
    entry.update(extra)
    return entry


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    fakes.reset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def write_yaml(tmp_path: Path) -> Callable[..., Path]:
    def _write(plugins: dict[str, Any], defaults: dict[str, Any] | None = None) -> Path:
        path = tmp_path / "providers.yaml"
        doc = {
            "version": 1,
            "defaults": defaults
            or {"timeout_s": 1.0, "quota_cooldown_s": 120.0,
                "circuit_breaker": {"failure_threshold": 2, "cooldown_s": 30.0, "max_cooldown_s": 300.0}},
            "plugins": plugins,
        }
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture
async def make_router(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Callable[..., Any]]:
    created: list[SmartRouter] = []

    async def _make(path: Path, secrets: dict[str, str] | None = None) -> SmartRouter:
        values = {"FAKE_KEY": "k"} if secrets is None else secrets
        bus = EventBus()
        registry = PluginRegistry.from_file(path, secret_lookup=values.get, bus=bus)
        router = SmartRouter(registry, QuotaStore(tmp_path / "data" / "quota.db"), clock=clock)
        await router.start()
        created.append(router)
        return router

    yield _make
    for r in created:
        await r.close()
