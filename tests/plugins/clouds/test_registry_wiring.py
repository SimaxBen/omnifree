"""The real config/providers.yaml entries of the cloud groups load and construct without I/O."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from core.events import EventBus
from core.interfaces import ADAPTER_BASES, NotConfigured
from core.registry import PluginRegistry

ROOT = Path(__file__).resolve().parents[3]
CLOUD_GROUPS = {"oracle", "gcp", "cloudflare", "aws"}
HEAVY_SDKS = ("boto3", "oracledb", "oci", "google.cloud")


@pytest.fixture
def registry() -> PluginRegistry:
    # No real network: every HTTP call answers 503.
    return PluginRegistry.from_file(
        ROOT / "config" / "providers.yaml",
        secret_lookup=lambda _key: None,
        bus=EventBus(),
        transport_factory=lambda _spec: httpx.MockTransport(lambda _req: httpx.Response(503)),
    )


def cloud_names(registry: PluginRegistry) -> list[str]:
    return [s.name for s in registry.list() if s.group in CLOUD_GROUPS]


def test_every_cloud_entry_has_a_matching_class(registry: PluginRegistry) -> None:
    names = cloud_names(registry)
    assert len(names) == 12
    for name in names:
        meta = registry.class_meta(name)
        assert meta["available"], meta
        spec = registry.spec(name)
        assert set(meta["required_secrets"]) == set(spec.secrets), name


async def test_cloud_adapters_construct_without_io_and_lazy_sdks(registry: PluginRegistry) -> None:
    before = {m for m in HEAVY_SDKS if m in sys.modules}
    for name in cloud_names(registry):
        spec = registry.spec(name)
        adapter = await registry.get(name, allow_disabled=True)
        assert isinstance(adapter, ADAPTER_BASES[spec.kind])
        health = await adapter.health()  # missing secrets -> ok=False, never raises
        assert not health.ok
        if spec.secrets:
            assert "NotConfigured" in health.detail
    await registry.close()
    assert {m for m in HEAVY_SDKS if m in sys.modules} == before


async def test_missing_secret_raises_not_configured(registry: PluginRegistry) -> None:
    adapter = await registry.get("cloudflare_d1")
    with pytest.raises(NotConfigured):
        await adapter.query("SELECT 1")
    await registry.close()
