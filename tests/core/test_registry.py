from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from core.events import EventBus, EventType
from core.interfaces import ADAPTER_BASES, NotConfigured, PluginLoadError, ProviderKind, adapter_catalog
from core.registry import PluginRegistry
from tests.core import fakes
from tests.core.conftest import llm, storage

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _registry(path: Path, secrets: dict[str, str] | None = None) -> PluginRegistry:
    return PluginRegistry.from_file(path, secret_lookup=(secrets or {"FAKE_KEY": "k"}).get, bus=EventBus())


def test_chain_is_priority_ordered_and_skips_disabled(write_yaml) -> None:
    path = write_yaml({"b": llm(20), "a": llm(20), "c": llm(5), "d": llm(1, enabled=False), "s": storage(1, 10)})
    reg = _registry(path)
    assert reg.chain("llm") == ["c", "a", "b"]
    assert reg.chain(ProviderKind.STORAGE) == ["s"]
    assert [s.name for s in reg.list("llm")] == ["d", "c", "a", "b"]


async def test_get_instantiates_once_with_resolved_secret_aliases(write_yaml) -> None:
    path = write_yaml({"a": llm(10, optional_secrets={"extra": "MISSING_KEY"})})
    reg = _registry(path)
    inst = await reg.get("a")
    assert inst is await reg.get("a")
    assert isinstance(inst, fakes.FakeLLM)
    assert inst.secret("api_key") == "k"
    assert inst.secret("extra") is None
    assert inst.context.secret_names["extra"] == "MISSING_KEY"
    assert inst.context.timeout_s == 1.0
    with pytest.raises(NotConfigured):
        inst.require_secret("extra")
    assert reg.missing_secrets("a") == []
    await reg.close()


async def test_missing_secrets_reported(write_yaml) -> None:
    reg = _registry(write_yaml({"a": llm(10)}), secrets={"OTHER": "x"})
    assert reg.missing_secrets("a") == ["FAKE_KEY"]


async def test_disabled_and_broken_plugins(write_yaml) -> None:
    path = write_yaml(
        {
            "off": llm(10, enabled=False),
            "nomod": llm(10, module="tests.core.does_not_exist:X"),
            "nocls": llm(10, module="tests.core.fakes:Missing"),
            "notadapter": llm(10, module="tests.core.fakes:NOT_AN_ADAPTER"),
            "wrongkind": llm(10, module="tests.core.fakes:FakeStorage"),
        }
    )
    reg = _registry(path)
    events = reg.bus.subscribe([EventType.PLUGIN_LOAD_FAILED])
    with pytest.raises(NotConfigured):
        await reg.get("off")
    assert await reg.get("off", allow_disabled=True)
    for name in ("nomod", "nocls", "notadapter", "wrongkind"):
        with pytest.raises(PluginLoadError):
            await reg.get(name)
        assert reg.load_error(name)
    assert events.queue.qsize() == 4
    with pytest.raises(NotConfigured):
        reg.spec("nope")


def test_invalid_entries_rejected(write_yaml) -> None:
    with pytest.raises(ValueError):
        _registry(write_yaml({"a": llm(10, module="not a module path")}))
    with pytest.raises(ValueError):
        _registry(write_yaml({"a": llm(10, kind="teleport")}))
    with pytest.raises(ValueError):
        _registry(write_yaml({"Bad-Name": llm(10)}))


async def test_enable_disable_persists_atomically(write_yaml) -> None:
    path = write_yaml({"a": llm(10), "b": llm(20)})
    reg = _registry(path)
    await reg.get("a")
    sub = reg.bus.subscribe(["registry.*"])
    closed_before = fakes.FakeLLM.closed
    await reg.set_enabled("a", False)
    assert fakes.FakeLLM.closed == closed_before + 1  # instance dropped and closed
    assert reg.chain("llm") == ["b"]
    assert (await sub.get(1)).type == EventType.PLUGIN_DISABLED
    reloaded = _registry(path)
    assert reloaded.spec("a").enabled is False
    assert reloaded.spec("a").secrets == {"api_key": "FAKE_KEY"}
    assert not list(path.parent.glob(".providers.*.tmp"))
    await reg.set_enabled("a", True)
    assert _registry(path).chain("llm") == ["a", "b"]


async def test_reorder_rewrites_priorities_and_persists(write_yaml) -> None:
    path = write_yaml({"a": llm(10), "b": llm(20), "c": llm(30), "s": storage(5, 10)})
    reg = _registry(path)
    sub = reg.bus.subscribe([EventType.CHAIN_REORDERED])
    chain = await reg.reorder("llm", ["c", "a"])
    assert chain == ["c", "a", "b"]
    assert [reg.spec(n).priority for n in chain] == [10, 20, 30]
    assert reg.spec("s").priority == 5
    event = await sub.get(1)
    assert event.data["chain"] == ["c", "a", "b"]
    assert _registry(path).chain("llm") == ["c", "a", "b"]
    with pytest.raises(NotConfigured):
        await reg.reorder("llm", ["s"])
    await reg.set_priority("b", 1)
    assert _registry(path).chain("llm")[0] == "b"


async def test_update_settings_and_reload(write_yaml) -> None:
    path = write_yaml({"a": llm(10)})
    reg = _registry(path)
    first = await reg.get("a")
    await reg.update_settings("a", {"default_model": "m2", "new": 1})
    second = await reg.get("a")
    assert first is not second
    assert second.setting("default_model") == "m2"
    await reg.update_settings("a", {"new": None})
    assert "new" not in _registry(path).spec("a").settings
    await reg.reload()
    assert reg.spec("a").settings["default_model"] == "m2"


async def test_class_meta_and_catalog(write_yaml) -> None:
    reg = _registry(write_yaml({"a": llm(10), "x": llm(10, module="tests.core.nope:X")}))
    meta = reg.class_meta("a")
    assert meta["available"] and meta["display_name"] == "Fake LLM"
    assert "embed" in meta["capabilities"]
    assert reg.class_meta("x")["available"] is False
    assert "tests.core.fakes:FakeLLM" in adapter_catalog()
    assert reg.describe("a")["missing_secrets"] == []


def test_project_providers_yaml_is_valid_and_follows_convention() -> None:
    reg = PluginRegistry.from_file(PROJECT_ROOT / "config" / "providers.yaml")
    names = set(reg.specs)
    expected = {
        "freellmapi", "groq", "gemini", "mistral", "openrouter", "cloudflare_ai", "nvidia_nim", "cohere",
        "sambanova", "zai", "cerebras", "ollama", "oracle_compute", "oracle_adb_1", "oracle_adb_2",
        "oracle_block_storage", "gcp_cloud_run", "gcp_e2_micro", "gcp_firestore", "gcp_gcs",
        "cloudflare_workers", "cloudflare_r2", "cloudflare_d1", "aws_dynamodb", "supabase", "neon", "turso",
        "mongodb_atlas", "upstash_redis", "ibm_cloudant", "backblaze_b2", "koyeb", "vercel",
        "netlify", "github_pages", "github_actions", "upstash_qstash", "pihole", "tailscale",
    }
    assert expected <= names
    groups = {"llm_pool", "oracle", "gcp", "cloudflare", "aws", "databases", "storage", "compute_edge", "network"}
    pattern = re.compile(r"^plugins\.(?P<group>\w+)\.(?P<file>[a-z0-9_]+):(?P<cls>[A-Z]\w+)$")
    for spec in reg.specs.values():
        m = pattern.match(spec.module)
        assert m, spec.module
        assert m["group"] == spec.group and spec.group in groups, spec.name
        assert spec.kind in ADAPTER_BASES
        assert (PROJECT_ROOT / "plugins" / spec.group / "__init__.py").exists()
        for key in [*spec.secrets.values(), *spec.optional_secrets.values()]:
            assert re.match(r"^[A-Z][A-Z0-9_]+$", key), (spec.name, key)
    raw = yaml.safe_load((PROJECT_ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))
    assert raw["version"] == 1


def test_project_contract_documents_every_plugin() -> None:
    contract = (PROJECT_ROOT / "CONTRACT.md").read_text(encoding="utf-8")
    reg = PluginRegistry.from_file(PROJECT_ROOT / "config" / "providers.yaml")
    for spec in reg.specs.values():
        assert f"`{spec.module}`" in contract, spec.module
        for key in spec.secrets.values():
            assert key in contract, (spec.name, key)
