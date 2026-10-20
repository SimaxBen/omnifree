"""Every entry of config/providers.yaml must resolve to a concrete adapter class.

For each plugin entry this checks that:

* ``module`` imports and names a class that subclasses the ABC for its ``kind``
  and is not abstract (via ``PluginRegistry.load_class``);
* the class's own ``kind`` matches the YAML ``kind`` and the module lives in
  ``plugins.<group>``;
* every secret alias the class declares as required is mapped in the YAML;
* the adapter constructs with no secrets and no I/O, and ``health()`` never
  raises (returns ok=False); adapters with required secrets must not touch
  the network at all before failing;
* importing every plugin module pulls in none of the heavy optional SDKs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from core.interfaces import ADAPTER_BASES, HealthStatus, ProviderAdapter
from core.registry import PluginRegistry

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_YAML = ROOT / "config" / "providers.yaml"

_REGISTRY = PluginRegistry.from_file(PROVIDERS_YAML)
PLUGIN_NAMES = sorted(_REGISTRY.specs)

HEAVY_SDKS = (
    "boto3",
    "botocore",
    "oracledb",
    "oci",
    "google.cloud",
    "pymongo",
    "motor",
    "asyncpg",
    "libsql_client",
    "playwright",
)


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Fails every request (no real network) and records that it was attempted."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("network disabled in tests", request=request)


def test_providers_yaml_is_not_empty() -> None:
    assert len(PLUGIN_NAMES) >= 39  # 40 spec entries minus upstash_kafka (discontinued 2025-03-11)


@pytest.mark.parametrize("name", PLUGIN_NAMES)
def test_entry_resolves_to_concrete_adapter(name: str) -> None:
    spec = _REGISTRY.spec(name)
    cls = _REGISTRY.load_class(spec)

    base = ADAPTER_BASES[spec.kind]
    assert issubclass(cls, base), f"{spec.module} is not a {base.__name__}"
    assert issubclass(cls, ProviderAdapter)
    assert not getattr(cls, "__abstractmethods__", None), cls.__abstractmethods__
    assert cls.kind == spec.kind, f"{spec.module}.kind={cls.kind} but YAML says {spec.kind}"
    assert spec.module_path.startswith(f"plugins.{spec.group}."), (
        f"{spec.module} is not inside plugins/{spec.group}/"
    )

    if cls.meta is not None:
        declared = set(cls.meta.required_secrets)
        mapped = set(spec.secrets)
        assert declared <= mapped, f"required secrets not mapped in YAML: {declared - mapped}"


@pytest.mark.parametrize("name", PLUGIN_NAMES)
async def test_constructs_and_health_without_secrets(name: str) -> None:
    transport = _RecordingTransport()
    registry = PluginRegistry.from_file(
        PROVIDERS_YAML,
        secret_lookup=lambda _key: None,
        transport_factory=lambda _spec: transport,
    )
    spec = registry.spec(name)
    adapter = await registry.get(name, allow_disabled=True)
    try:
        assert transport.requests == [], "constructor performed I/O"
        status = await adapter.health()
        assert isinstance(status, HealthStatus)
        assert status.ok is False
        if spec.secrets:
            assert transport.requests == [], (
                f"health() hit the network without required secrets: "
                f"{[str(r.url) for r in transport.requests]}"
            )
    finally:
        await registry.close()


def test_plugin_imports_pull_no_heavy_sdk() -> None:
    modules = sorted({_REGISTRY.spec(n).module_path for n in PLUGIN_NAMES})
    script = (
        "import importlib, sys\n"
        f"for m in {modules!r}:\n"
        "    importlib.import_module(m)\n"
        f"heavy = {HEAVY_SDKS!r}\n"
        "bad = sorted(m for m in sys.modules if any(m == h or m.startswith(h + '.') for h in heavy))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"heavy SDKs imported eagerly: {out.stdout.strip()}"


def test_unconfigured_health_pulls_no_heavy_sdk() -> None:
    """health() without secrets must fail with NotConfigured before importing any SDK."""
    script = (
        "import asyncio, sys, httpx\n"
        "from core.registry import PluginRegistry\n"
        "class T(httpx.AsyncBaseTransport):\n"
        "    async def handle_async_request(self, request):\n"
        "        raise httpx.ConnectError('network disabled', request=request)\n"
        "async def main():\n"
        f"    reg = PluginRegistry.from_file({str(PROVIDERS_YAML)!r}, transport_factory=lambda s: T())\n"
        "    for name in reg.specs:\n"
        "        if not reg.spec(name).secrets:\n"
        "            continue\n"
        "        adapter = await reg.get(name, allow_disabled=True)\n"
        "        await adapter.health()\n"
        "    await reg.close()\n"
        "asyncio.run(main())\n"
        f"heavy = {HEAVY_SDKS!r}\n"
        "bad = sorted({h for m in sys.modules for h in heavy if m == h or m.startswith(h + '.')})\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"SDKs imported by unconfigured health(): {out.stdout.strip()}"
