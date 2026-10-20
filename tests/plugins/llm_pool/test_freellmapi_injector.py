from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from core.interfaces import NotConfigured, ProviderAuthError
from core.registry import PluginRegistry
from plugins.llm_pool.freellmapi_injector import (
    FreeLLMAPIAdminClient,
    build_plan,
    push_keys,
    render_env,
    write_private_file,
)

ROOT = Path(__file__).resolve().parents[3]

VAULT = {
    "GROQ_API_KEY": "gsk_1",
    "GEMINI_API_KEY": "AIza_2",
    "MISTRAL_API_KEY": "mis_3",
    "OPENROUTER_API_KEY": "sk-or-4",
    "CEREBRAS_API_KEY": "csk_5",
    "NVIDIA_API_KEY": "nvapi_6",
    "COHERE_API_KEY": "co_7",
    "CLOUDFLARE_ACCOUNT_ID": "acc",
    "CLOUDFLARE_AI_API_TOKEN": "cftok",
    "SAMBANOVA_API_KEY": "sn_8",
    "ZAI_API_KEY": "zai_9",
    "FREELLMAPI_ENCRYPTION_KEY": "ab" * 32,
    "FREELLMAPI_ADMIN_EMAIL": "me@example.com",
    "FREELLMAPI_ADMIN_PASSWORD": "long-enough-pw",
}


@pytest.fixture
def registry(tmp_path: Path) -> PluginRegistry:
    path = tmp_path / "providers.yaml"
    shutil.copy(ROOT / "config" / "providers.yaml", path)
    return PluginRegistry.from_file(path, secret_lookup=VAULT.get)


def test_plan_from_real_providers_yaml(registry: PluginRegistry) -> None:
    plan = build_plan(registry, VAULT.get)
    keys = {k["platform"]: k for k in plan.keys}
    assert set(keys) == {"groq", "google", "mistral", "openrouter", "cerebras", "nvidia", "cohere", "cloudflare"}
    assert keys["cloudflare"]["key"] == "acc:cftok"
    assert keys["google"]["key"] == "AIza_2"
    assert all(k["label"] == "omnifree" and k["enabled"] is True for k in plan.keys)
    customs = {c["label"]: c for c in plan.custom_providers}
    assert customs["SambaNova"]["baseUrl"] == "https://api.sambanova.ai/v1" and customs["SambaNova"]["apiKey"] == "sn_8"
    assert customs["Z.AI"]["baseUrl"] == "https://api.z.ai/api/paas/v4"
    assert customs["Ollama (local)"]["baseUrl"] == "http://ollama:11434/v1"
    assert customs["Ollama (local)"]["models"] == ["qwen2.5:7b", "llama3:8b"]
    config = plan.to_config()
    assert set(config) == {"admin", "keys", "customProviders"}
    assert config["admin"] == {"email": "me@example.com", "password": "long-enough-pw"}
    summary = json.dumps(plan.summary())
    assert "gsk_1" not in summary and "long-enough-pw" not in summary


def test_plan_skips_missing_and_disabled(registry: PluginRegistry) -> None:
    partial = {"GROQ_API_KEY": "g", "CLOUDFLARE_AI_API_TOKEN": "t"}
    registry.specs["groq"].enabled = False
    plan = build_plan(registry, partial.get, only_enabled=True, overrides={"include_ollama": False})
    assert plan.keys == [] and plan.custom_providers == [] and plan.admin is None
    joined = " | ".join(plan.skipped)
    assert "groq: disabled" in joined and "cloudflare_ai: missing secret(s) account_id" in joined
    assert "zai: missing secret api_key" in joined


def test_plan_overrides(registry: PluginRegistry) -> None:
    plan = build_plan(
        registry, VAULT.get,
        overrides={"zai_platform": "zhipu", "label": "prod", "routing_strategy": "balanced", "ollama_url": "http://10.0.0.5:11434/v1/"},
    )
    assert {"platform": "zhipu", "key": "zai_9", "label": "prod", "enabled": True} in plan.keys
    assert all(c["label"] != "Z.AI" for c in plan.custom_providers)
    assert plan.routing == {"strategy": "balanced"}
    assert any(c["baseUrl"] == "http://10.0.0.5:11434/v1" for c in plan.custom_providers)


def test_plan_reads_injector_settings_from_yaml(tmp_path: Path) -> None:
    doc = yaml.safe_load((ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))
    doc["plugins"]["freellmapi"]["settings"]["injector"] = {"include_ollama": False, "label": "yaml"}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    plan = build_plan(PluginRegistry.from_file(path, secret_lookup=VAULT.get), VAULT.get)
    assert all(c["label"] != "Ollama (local)" for c in plan.custom_providers)
    assert plan.keys[0]["label"] == "yaml"


def test_short_admin_password_rejected(registry: PluginRegistry) -> None:
    bad = {**VAULT, "FREELLMAPI_ADMIN_PASSWORD": "short"}
    with pytest.raises(NotConfigured):
        build_plan(registry, bad.get)


def test_render_env_and_private_write(tmp_path: Path) -> None:
    env = render_env(VAULT.get, host_bind="100.64.0.7")
    assert f"ENCRYPTION_KEY={'ab' * 32}" in env and "HOST_BIND=100.64.0.7" in env
    assert "FREEAPI_CONFIG_PATH=/run/omnifree/freellmapi.config.json" in env and "PORT=3001" in env
    with pytest.raises(NotConfigured):
        render_env({"FREELLMAPI_ENCRYPTION_KEY": "nothex"}.get, host_bind="1.2.3.4")
    with pytest.raises(NotConfigured):
        render_env(VAULT.get, host_bind="")
    target = write_private_file(tmp_path / "nested" / "freellmapi.config.json", "{}\n")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert [p.name for p in target.parent.iterdir()] == ["freellmapi.config.json"]
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- admin API push


BASE = "http://oracle-arm:3001"


def _admin(mock: respx.MockRouter, existing: list[dict]) -> dict[str, respx.Route]:
    routes = {
        "providers": mock.get(f"{BASE}/api/keys/providers").respond(
            200, json={"providers": [{"platform": p} for p in
                                     ("groq", "google", "mistral", "openrouter", "cerebras", "nvidia", "cohere")]}
        ),
        "list": mock.get(f"{BASE}/api/keys").respond(200, json=existing),
        "add": mock.post(f"{BASE}/api/keys"),
        "delete": mock.delete(url__regex=rf"{BASE}/api/keys/\d+").respond(204),
        "check": mock.post(url__regex=rf"{BASE}/api/health/check/\d+"),
    }
    counter = iter(range(100, 200))
    routes["add"].side_effect = lambda request: httpx.Response(201, json={"id": next(counter)})
    routes["check"].side_effect = lambda request: httpx.Response(
        200, json={"keyId": int(request.url.path.rsplit("/", 1)[1]), "status": "healthy"}
    )
    return routes


async def test_push_adds_missing_keeps_existing(mock: respx.MockRouter, registry: PluginRegistry) -> None:
    routes = _admin(mock, [{"id": 7, "platform": "groq", "enabled": True, "status": "healthy"}])
    plan = build_plan(registry, VAULT.get)
    async with FreeLLMAPIAdminClient(BASE + "/v1", "dash-token") as client:
        results = await push_keys(client, plan)
    by = {r.platform: r for r in results if r.platform != "custom"}
    assert by["groq"].action == "exists" and by["groq"].key_id == 7
    assert by["cloudflare"].action == "unsupported"
    assert by["google"].action == "added" and by["google"].status == "healthy"
    assert routes["delete"].call_count == 0
    bodies = [json.loads(c.request.content) for c in routes["add"].calls]
    assert {"platform": "google", "key": "AIza_2"} in bodies and all(b["platform"] != "groq" for b in bodies)
    assert all(c.request.headers["authorization"] == "Bearer dash-token" for c in routes["add"].calls)
    assert sum(1 for r in results if r.platform == "custom" and r.action == "skipped") == 3


async def test_push_replace_deletes_then_adds(mock: respx.MockRouter, registry: PluginRegistry) -> None:
    routes = _admin(mock, [{"id": 7, "platform": "groq"}, {"id": 8, "platform": "groq"}])
    plan = build_plan(registry, VAULT.get)
    async with FreeLLMAPIAdminClient(BASE, "t") as client:
        results = await push_keys(client, plan, replace=True, check=False)
    groq = next(r for r in results if r.platform == "groq")
    assert groq.action == "replaced" and groq.status is None
    assert sorted(c.request.url.path for c in routes["delete"].calls) == ["/api/keys/7", "/api/keys/8"]
    assert routes["check"].call_count == 0


async def test_push_auth_failure_does_not_echo_body(mock: respx.MockRouter, registry: PluginRegistry) -> None:
    mock.get(f"{BASE}/api/keys/providers").respond(401, text="token gsk_1 rejected")
    async with FreeLLMAPIAdminClient(BASE, "t") as client:
        with pytest.raises(ProviderAuthError) as exc:
            await push_keys(client, build_plan(registry, VAULT.get))
    assert "gsk_1" not in str(exc.value)


def test_admin_client_requires_token() -> None:
    with pytest.raises(NotConfigured):
        FreeLLMAPIAdminClient(BASE, "")
