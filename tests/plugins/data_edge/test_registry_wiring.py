"""The real providers.yaml entries owned by data_edge load through PluginRegistry."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from core.events import EventBus
from core.interfaces import ADAPTER_BASES, NotConfigured
from core.registry import PluginRegistry

ROOT = Path(__file__).resolve().parents[3]
GROUPS = {"databases", "storage", "compute_edge", "network"}


@pytest.fixture
def registry(tmp_path: Path) -> PluginRegistry:
    path = tmp_path / "providers.yaml"
    shutil.copy(ROOT / "config" / "providers.yaml", path)

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call {request.method} {request.url}")

    return PluginRegistry.from_file(path, secret_lookup=lambda _key: None, bus=EventBus(),
                                    transport_factory=lambda _spec: httpx.MockTransport(no_network))


def owned(registry: PluginRegistry) -> list[str]:
    return [spec.name for spec in registry.list() if spec.group in GROUPS]


async def test_every_owned_entry_loads_and_declares_its_secrets(registry: PluginRegistry) -> None:
    names = owned(registry)
    assert len(names) == 15
    for name in names:
        spec = registry.spec(name)
        adapter = await registry.get(name, allow_disabled=True)
        assert isinstance(adapter, ADAPTER_BASES[spec.kind])
        meta = registry.class_meta(name)
        assert meta["available"], meta
        assert set(meta["required_secrets"]) == set(spec.secrets), name
        assert set(meta["optional_secrets"]) == set(spec.optional_secrets), name


async def test_missing_secrets_make_health_fail_without_network(registry: PluginRegistry) -> None:
    for name in owned(registry):
        adapter = await registry.get(name, allow_disabled=True)
        status = await adapter.health()
        assert status.ok is False
        assert "NotConfigured" in status.detail, (name, status.detail)
    await registry.close()


async def test_upstash_kafka_is_gone_and_koyeb_is_disabled_by_default(registry: PluginRegistry) -> None:
    # Upstash Kafka was discontinued on 2025-03-11; Koyeb has no free tier for new signups since 2026.
    assert "upstash_kafka" not in {spec.name for spec in registry.list()}
    assert registry.spec("koyeb").enabled is False
    with pytest.raises(NotConfigured):
        await registry.get("koyeb")
