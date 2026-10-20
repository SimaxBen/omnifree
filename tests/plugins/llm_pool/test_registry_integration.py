"""The real providers.yaml loads every llm_pool adapter, and the router fails over across them."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest
import respx

from core.events import EventBus
from core.interfaces import ChatMessage, ChatRequest, LLMAdapter, NotConfigured
from core.quota import QuotaStore
from core.registry import PluginRegistry
from core.router import SmartRouter
from tests.plugins.llm_pool.helpers import completion

ROOT = Path(__file__).resolve().parents[3]
SECRETS = {
    "FREELLMAPI_API_KEY": "freellmapi-k",
    "GROQ_API_KEY": "g",
    "CEREBRAS_API_KEY": "c",
    "CLOUDFLARE_ACCOUNT_ID": "acc",
    "CLOUDFLARE_AI_API_TOKEN": "t",
}


@pytest.fixture
def providers(tmp_path: Path) -> Path:
    path = tmp_path / "providers.yaml"
    shutil.copy(ROOT / "config" / "providers.yaml", path)
    return path


async def test_every_llm_pool_entry_loads(providers: Path) -> None:
    registry = PluginRegistry.from_file(providers, secret_lookup=SECRETS.get)
    names = [s.name for s in registry.list() if s.group == "llm_pool"]
    assert len(names) == 12
    for name in names:
        adapter = await registry.get(name, allow_disabled=True)
        assert isinstance(adapter, LLMAdapter), name
        assert registry.load_error(name) is None
        assert registry.class_meta(name)["available"] is True
    await registry.close()


def test_trial_and_tiny_cap_providers_sit_at_the_end_of_the_cloud_chain(providers: Path) -> None:
    registry = PluginRegistry.from_file(providers, secret_lookup=SECRETS.get)
    chain = [n for n in registry.chain("llm") if n != "ollama"]
    assert chain[-2:] == ["openrouter", "cerebras"]  # 50 RPD, card-only trial (sambanova is disabled: no free tier)
    assert "sambanova" not in chain and "mistral" not in chain
    assert registry.spec("cerebras").settings["trial"] is True


async def test_router_fails_over_from_freellmapi_and_groq_to_cloudflare(
    providers: Path, tmp_path: Path, mock: respx.MockRouter
) -> None:
    mock.post("http://oracle-arm:3001/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    mock.post("https://api.groq.com/openai/v1/chat/completions").respond(
        429, headers={"retry-after": "120"}, json={"error": {"message": "rate limit"}}
    )
    mock.post("https://api.cloudflare.com/client/v4/accounts/acc/ai/v1/chat/completions").respond(
        200, json=completion("from cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    )
    registry = PluginRegistry.from_file(providers, secret_lookup=SECRETS.get, bus=EventBus())
    async with SmartRouter(registry, QuotaStore(tmp_path / "quota.db")) as router:
        resp = await router.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
        assert resp.content == "from cloudflare" and resp.routed_via == "cloudflare_ai"
        assert any(a.startswith("freellmapi") for a in resp.attempts)
        assert any(a.startswith("groq") for a in resp.attempts)
        statuses = {s.name: s for s in await router.status("llm")}
        assert statuses["groq"].exhausted_until is not None


async def test_router_reports_freellmapi_upstream(providers: Path, tmp_path: Path, mock: respx.MockRouter) -> None:
    mock.post("http://oracle-arm:3001/v1/chat/completions").respond(
        200, json=completion("pooled"), headers={"X-Routed-Via": "cerebras/gpt-oss-120b"}
    )
    registry = PluginRegistry.from_file(providers, secret_lookup=SECRETS.get, bus=EventBus())
    async with SmartRouter(registry, QuotaStore(tmp_path / "quota.db")) as router:
        resp = await router.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert resp.routed_via == "freellmapi:cerebras"


async def test_missing_secret_skips_provider(providers: Path) -> None:
    registry = PluginRegistry.from_file(providers, secret_lookup={}.get)
    adapter = await registry.get("groq")
    with pytest.raises(NotConfigured):
        await adapter.chat(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    await registry.close()
