from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.interfaces import (
    Capability,
    EmbeddingRequest,
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
    QuotaExhausted,
    TranscriptionRequest,
)
from plugins.llm_pool.cerebras import CerebrasAdapter
from plugins.llm_pool.cloudflare_ai import CloudflareWorkersAIAdapter
from plugins.llm_pool.freellmapi import FreeLLMAPIAdapter
from plugins.llm_pool.gemini import GeminiAdapter
from plugins.llm_pool.groq import GroqAdapter
from plugins.llm_pool.mistral import MistralAdapter
from plugins.llm_pool.nvidia_nim import NvidiaNIMAdapter
from plugins.llm_pool.ollama import OllamaAdapter
from plugins.llm_pool.openrouter import OpenRouterAdapter
from plugins.llm_pool.sambanova import SambaNovaAdapter
from plugins.llm_pool.zai import ZAIAdapter
from tests.plugins.llm_pool.helpers import completion, make, req, sse

GROQ = {"base_url": "https://api.groq.com/openai/v1", "default_model": "llama-3.3-70b-versatile",
        "transcription_models": ["whisper-large-v3"]}


def groq() -> GroqAdapter:
    return make(GroqAdapter, "groq", dict(GROQ))


# --------------------------------------------------------------------------- Groq / generic behaviour


async def test_groq_chat_parses_body_and_rate_limit_headers(mock: respx.MockRouter) -> None:
    route = mock.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200,
        json=completion("hey", "llama-3.3-70b-versatile"),
        headers={
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "998",
            "x-ratelimit-reset-requests": "2m52.8s",
            "x-ratelimit-limit-tokens": "12000",
            "x-ratelimit-remaining-tokens": "11000",
            "x-ratelimit-reset-tokens": "5s",
        },
    )
    adapter = groq()
    resp = await adapter.chat(req(temperature=0.1))
    assert resp.content == "hey" and resp.routed_via == "groq" and resp.usage.total_tokens == 7
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "llama-3.3-70b-versatile" and sent["temperature"] == 0.1 and sent["stream"] is False
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"

    quota = await adapter.quota()
    assert quota.source == "provider" and quota.unit == "requests" and quota.window == "day"
    assert (quota.used, quota.limit) == (2, 1000)
    assert {b["metric"] for b in quota.extra["buckets"]} == {"requests", "tokens"}
    await adapter.close()


async def test_quota_without_headers_defers_to_router(mock: respx.MockRouter) -> None:
    quota = await groq().quota()
    assert quota.source == "router" and quota.unit == "unknown"


async def test_429_maps_to_quota_exhausted_with_retry_after(mock: respx.MockRouter) -> None:
    mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "17"}, json={"error": {"message": "slow down"}}),
            httpx.Response(429, json={"error": {"message": "Rate limit reached. Please try again in 1m30s."}}),
        ]
    )
    adapter = groq()
    with pytest.raises(QuotaExhausted) as first:
        await adapter.chat(req())
    assert first.value.retry_after == 17 and first.value.status_code == 429
    with pytest.raises(QuotaExhausted) as second:
        await adapter.chat(req())
    assert second.value.retry_after == pytest.approx(90, abs=2)


async def test_429_retry_after_falls_back_to_reset_header(mock: respx.MockRouter) -> None:
    mock.post("https://api.groq.com/openai/v1/chat/completions").respond(
        429,
        json={"error": {"message": "limit"}},
        headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "0",
                 "x-ratelimit-reset-requests": "45s"},
    )
    with pytest.raises(QuotaExhausted) as exc:
        await groq().chat(req())
    assert exc.value.retry_after == pytest.approx(45, abs=2)


async def test_preflight_refuses_while_bucket_is_empty(mock: respx.MockRouter) -> None:
    route = mock.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200,
        json=completion(),
        headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "0",
                 "x-ratelimit-reset-requests": "10m"},
    )
    adapter = groq()
    await adapter.chat(req())
    with pytest.raises(QuotaExhausted) as exc:
        await adapter.chat(req())
    assert exc.value.retry_after == pytest.approx(600, abs=5)
    assert route.call_count == 1  # second call never left the process

    lenient = make(GroqAdapter, "groq", {**GROQ, "respect_rate_headers": False})
    await lenient.chat(req())
    await lenient.chat(req())
    assert route.call_count == 3


@pytest.mark.parametrize(
    ("status", "body", "exc_type"),
    [
        (400, {"error": {"message": "You exceeded your current quota"}}, QuotaExhausted),
        (400, {"error": {"message": "invalid temperature"}}, ProviderError),
        (401, {"error": "bad key"}, ProviderAuthError),
        (403, {"error": "daily limit exceeded for this key"}, QuotaExhausted),
        (402, {"error": "insufficient credits"}, QuotaExhausted),
        (503, {"error": "overloaded"}, ProviderUnavailable),
    ],
)
async def test_error_mapping(mock: respx.MockRouter, status: int, body: dict, exc_type: type) -> None:
    mock.post("https://api.groq.com/openai/v1/chat/completions").respond(status, json=body)
    with pytest.raises(ProviderError) as exc:
        await groq().chat(req())
    assert type(exc.value) is exc_type


async def test_in_body_error_with_200_is_mapped(mock: respx.MockRouter) -> None:
    mock.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}
    )
    with pytest.raises(QuotaExhausted):
        await groq().chat(req())


async def test_missing_secret_is_not_configured(mock: respx.MockRouter) -> None:
    adapter = make(GroqAdapter, "groq", dict(GROQ), secrets={})
    with pytest.raises(NotConfigured):
        await adapter.chat(req())
    health = await adapter.health()
    assert not health.ok and "NotConfigured" in health.detail


async def test_stream_yields_chunks_and_captures_headers(mock: respx.MockRouter) -> None:
    body = sse(
        {"model": "m", "choices": [{"delta": {"content": "Hel"}}]},
        {"model": "m", "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    )
    mock.post("https://api.cerebras.ai/v1/chat/completions").respond(
        200, content=body, headers={"content-type": "text/event-stream",
                                    "x-ratelimit-limit-requests-day": "14400",
                                    "x-ratelimit-remaining-requests-day": "14000"}
    )
    adapter = make(CerebrasAdapter, "cerebras", {"base_url": "https://api.cerebras.ai/v1", "default_model": "m"})
    chunks = [c async for c in adapter.stream(req())]
    assert "".join(c.delta for c in chunks) == "Hello"
    assert chunks[-1].usage.total_tokens == 3 and chunks[-1].finish_reason == "stop"
    quota = await adapter.quota()
    assert (quota.used, quota.limit, quota.window) == (400, 14400, "day")


async def test_stream_429_and_in_stream_quota_error(mock: respx.MockRouter) -> None:
    route = mock.post("https://api.cerebras.ai/v1/chat/completions")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "3"}, text="too many"),
        httpx.Response(200, content=sse({"error": {"message": "token quota exceeded"}}, done=False),
                       headers={"content-type": "text/event-stream"}),
    ]
    adapter = make(CerebrasAdapter, "cerebras", {"base_url": "https://api.cerebras.ai/v1", "default_model": "m"})
    with pytest.raises(QuotaExhausted) as exc:
        [c async for c in adapter.stream(req())]
    assert exc.value.retry_after == 3
    with pytest.raises(QuotaExhausted):
        [c async for c in adapter.stream(req())]


async def test_groq_transcription_multipart(mock: respx.MockRouter) -> None:
    route = mock.post("https://api.groq.com/openai/v1/audio/transcriptions").respond(200, json={"text": "bonjour"})
    adapter = groq()
    assert adapter.supports(Capability.TRANSCRIBE)
    out = await adapter.transcribe(TranscriptionRequest(audio=b"RIFF....", filename="a.wav", language="fr"))
    assert out.text == "bonjour" and out.model == "whisper-large-v3" and out.routed_via == "groq"
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("multipart/form-data")
    raw = request.content
    assert b'name="model"' in raw and b"whisper-large-v3" in raw and b'filename="a.wav"' in raw and b"fr" in raw


@pytest.mark.parametrize(
    ("cls", "base"),
    [
        (SambaNovaAdapter, "https://api.sambanova.ai/v1"),
        (NvidiaNIMAdapter, "https://integrate.api.nvidia.com/v1"),
        (ZAIAdapter, "https://api.z.ai/api/paas/v4"),
    ],
)
async def test_plain_openai_compatible_adapters(mock: respx.MockRouter, cls: type, base: str) -> None:
    mock.post(f"{base}/chat/completions").respond(200, json=completion("ok"))
    mock.get(f"{base}/models").respond(200, json={"data": [{"id": "m1", "owned_by": "x"}]})
    adapter = make(cls, "p", {"base_url": base, "default_model": "m1"})
    assert (await adapter.chat(req())).content == "ok"
    assert [m.id for m in await adapter.list_models()] == ["m1"]
    assert (await adapter.health()).ok


async def test_models_404_falls_back_to_config(mock: respx.MockRouter) -> None:
    mock.get("https://api.z.ai/api/paas/v4/models").respond(404, text="not found")
    adapter = make(ZAIAdapter, "zai", {"base_url": "https://api.z.ai/api/paas/v4", "models": ["glm-4.5-flash"]})
    assert [m.id for m in await adapter.list_models()] == ["glm-4.5-flash"]


# --------------------------------------------------------------------------- Gemini


GEMINI = {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "default_model": "gemini-2.5-flash",
          "default_embedding_model": "gemini-embedding-001"}


async def test_gemini_models_embed_and_list_wrapped_quota(mock: respx.MockRouter) -> None:
    base = GEMINI["base_url"]
    mock.get(f"{base}/models").respond(200, json={"data": [{"id": "models/gemini-2.5-flash"}, {"id": "models/gemma-3-27b-it"}]})
    mock.post(f"{base}/embeddings").respond(
        200, json={"data": [{"index": 1, "embedding": [0.2]}, {"index": 0, "embedding": [0.1]}], "model": "gemini-embedding-001"}
    )
    mock.post(f"{base}/chat/completions").respond(
        429, json=[{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                              "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"}]}}]
    )
    adapter = make(GeminiAdapter, "gemini", dict(GEMINI))
    assert [m.id for m in await adapter.list_models()] == ["gemini-2.5-flash", "gemma-3-27b-it"]
    emb = await adapter.embed(EmbeddingRequest(input=["a", "b"]))
    assert emb.vectors == [[0.1], [0.2]] and emb.routed_via == "gemini"
    with pytest.raises(QuotaExhausted) as exc:
        await adapter.chat(req("models/gemini-2.5-flash"))
    assert exc.value.retry_after == pytest.approx(31, abs=2)


async def test_gemini_strips_model_prefix_in_request(mock: respx.MockRouter) -> None:
    route = mock.post(f"{GEMINI['base_url']}/chat/completions").respond(200, json=completion())
    await make(GeminiAdapter, "gemini", dict(GEMINI)).chat(req("models/gemini-2.5-pro"))
    assert json.loads(route.calls.last.request.content)["model"] == "gemini-2.5-pro"


# --------------------------------------------------------------------------- Mistral


MISTRAL = {"base_url": "https://api.mistral.ai/v1", "codestral_base_url": "https://codestral.mistral.ai/v1",
           "default_model": "mistral-small-latest"}


async def test_mistral_codestral_routing(mock: respx.MockRouter) -> None:
    main = mock.post("https://api.mistral.ai/v1/chat/completions").respond(200, json=completion("main"))
    cod = mock.post("https://codestral.mistral.ai/v1/chat/completions").respond(200, json=completion("cod"))
    cod_stream = sse({"model": "codestral-latest", "choices": [{"delta": {"content": "x"}}]})

    with_key = make(MistralAdapter, "mistral", dict(MISTRAL), secrets={"api_key": "main-k", "codestral_api_key": "cod-k"})
    assert (await with_key.chat(req("codestral-latest"))).content == "cod"
    assert cod.calls.last.request.headers["authorization"] == "Bearer cod-k"
    assert (await with_key.chat(req())).content == "main"
    assert main.calls.last.request.headers["authorization"] == "Bearer main-k"

    cod.side_effect = [httpx.Response(200, content=cod_stream, headers={"content-type": "text/event-stream"})]
    chunks = [c async for c in with_key.stream(req("codestral-latest"))]
    assert chunks[0].delta == "x" and cod.call_count == 2

    without_key = make(MistralAdapter, "mistral", dict(MISTRAL), secrets={"api_key": "main-k"})
    await without_key.chat(req("codestral-latest"))
    assert main.call_count == 2 and main.calls.last.request.headers["authorization"] == "Bearer main-k"


# --------------------------------------------------------------------------- OpenRouter


OPENROUTER = {"base_url": "https://openrouter.ai/api/v1", "default_model": "meta-llama/llama-3.3-70b-instruct:free",
              "free_only": True}


async def test_openrouter_refuses_paid_models_without_calling(mock: respx.MockRouter) -> None:
    route = mock.post("https://openrouter.ai/api/v1/chat/completions").respond(200, json=completion())
    adapter = make(OpenRouterAdapter, "openrouter", dict(OPENROUTER))
    with pytest.raises(ProviderError) as exc:
        await adapter.chat(req("openai/gpt-4o"))
    assert exc.value.status_code == 400 and type(exc.value) is ProviderError
    with pytest.raises(ProviderError):
        [c async for c in adapter.stream(req("anthropic/claude-x"))]
    assert route.call_count == 0
    await adapter.chat(req())
    assert route.call_count == 1

    paid_ok = make(OpenRouterAdapter, "openrouter", {**OPENROUTER, "free_only": False})
    await paid_ok.chat(req("openai/gpt-4o"))
    assert route.call_count == 2


async def test_openrouter_models_filter_and_quota(mock: respx.MockRouter) -> None:
    mock.get("https://openrouter.ai/api/v1/models").respond(
        200, json={"data": [{"id": "a:free", "context_length": 8192}, {"id": "b"}]}
    )
    mock.get("https://openrouter.ai/api/v1/key").respond(
        200, json={"data": {"usage": 0, "limit": None, "is_free_tier": True, "rate_limit": {"requests": 20, "interval": "10s"}}}
    )
    adapter = make(OpenRouterAdapter, "openrouter", dict(OPENROUTER))
    models = await adapter.list_models()
    assert [m.id for m in models] == ["a:free"] and models[0].context_window == 8192
    quota = await adapter.quota()
    assert quota.source == "provider" and quota.extra["is_free_tier"] is True and quota.limit is None


# --------------------------------------------------------------------------- Cloudflare Workers AI


CF = {"base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
      "default_model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "default_embedding_model": "@cf/baai/bge-m3"}
CF_SECRETS = {"account_id": "acc123", "api_key": "cf-token"}


def cf(**ctx) -> CloudflareWorkersAIAdapter:
    from core.interfaces import QuotaLimit

    return make(CloudflareWorkersAIAdapter, "cloudflare_ai", dict(CF), secrets=dict(CF_SECRETS),
                limits=(QuotaLimit(metric="neurons", window="day", limit=10000),), **ctx)


async def test_cloudflare_chat_embed_and_models(mock: respx.MockRouter) -> None:
    base = "https://api.cloudflare.com/client/v4/accounts/acc123/ai"
    chat = mock.post(f"{base}/v1/chat/completions").respond(200, json=completion("cf"))
    mock.post(f"{base}/v1/embeddings").respond(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
    search = mock.get(f"{base}/models/search")
    search.side_effect = [
        httpx.Response(200, json={"success": True, "result": [{"name": f"@cf/m{i}"} for i in range(100)],
                                  "result_info": {"total_count": 101}}),
        httpx.Response(200, json={"success": True, "result": [
            {"name": "@cf/last", "properties": [{"property_id": "context_window", "value": "32000"}]}],
            "result_info": {"total_count": 101}}),
    ]
    adapter = cf()
    assert (await adapter.chat(req())).content == "cf"
    assert chat.calls.last.request.headers["authorization"] == "Bearer cf-token"
    assert (await adapter.embed(EmbeddingRequest(input=["x"]))).vectors == [[1.0, 2.0]]
    models = await adapter.list_models()
    assert len(models) == 101 and models[-1].context_window == 32000
    assert search.calls[1].request.url.params["page"] == "2"


async def test_cloudflare_neuron_quota_and_fallback(mock: respx.MockRouter) -> None:
    gql = mock.post("https://api.cloudflare.com/client/v4/graphql")
    gql.side_effect = [
        httpx.Response(200, json={"data": {"viewer": {"accounts": [
            {"aiInferenceAdaptiveGroups": [{"sum": {"totalNeurons": 1234.5}}, {"sum": {"totalNeurons": 100}}]}]}}}),
        httpx.Response(200, json={"data": None, "errors": [{"message": "not authorized for that account"}]}),
    ]
    adapter = cf()
    quota = await adapter.quota()
    assert (quota.used, quota.limit, quota.unit, quota.source) == (1334.5, 10000, "neurons", "provider")
    sent = json.loads(gql.calls[0].request.content)
    assert sent["variables"]["account"] == "acc123" and "totalNeurons" in sent["query"]
    fallback = await adapter.quota()
    assert fallback.source == "router"


async def test_cloudflare_requires_account_id(mock: respx.MockRouter) -> None:
    adapter = make(CloudflareWorkersAIAdapter, "cloudflare_ai", dict(CF), secrets={"api_key": "t"})
    with pytest.raises(NotConfigured):
        await adapter.chat(req())


# --------------------------------------------------------------------------- Ollama


async def test_ollama_uses_v1_chat_and_native_tags_without_auth(mock: respx.MockRouter) -> None:
    chat = mock.post("http://oracle-arm:11434/v1/chat/completions").respond(200, json=completion("local"))
    mock.get("http://oracle-arm:11434/api/tags").respond(
        200, json={"models": [{"name": "qwen2.5:7b", "details": {"family": "qwen2"}}, {"name": "llama3:8b"}]}
    )
    adapter = make(OllamaAdapter, "ollama", {"base_url": "http://oracle-arm:11434", "auth": "none",
                                             "default_model": "qwen2.5:7b"}, secrets={})
    assert (await adapter.chat(req())).content == "local"
    assert "authorization" not in chat.calls.last.request.headers
    assert [m.id for m in await adapter.list_models()] == ["qwen2.5:7b", "llama3:8b"]
    assert (await adapter.health()).ok

    v1 = make(OllamaAdapter, "ollama", {"base_url": "http://oracle-arm:11434/v1", "auth": "none",
                                        "default_model": "qwen2.5:7b"}, secrets={})
    await v1.chat(req())
    assert chat.call_count == 2


# --------------------------------------------------------------------------- FreeLLMAPI


FREE = {"base_url": "http://oracle-arm:3001/v1", "default_model": "auto", "routed_via_header": None}


async def test_freellmapi_routed_via_and_auto_model(mock: respx.MockRouter) -> None:
    route = mock.post("http://oracle-arm:3001/v1/chat/completions").respond(
        200, json=completion("pooled"), headers={"X-Routed-Via": "groq/llama-3.3-70b-versatile"}
    )
    adapter = make(FreeLLMAPIAdapter, "freellmapi", dict(FREE))
    resp = await adapter.chat(req())
    assert resp.routed_via == "freellmapi:groq"
    assert json.loads(route.calls.last.request.content)["model"] == "auto"

    full = make(FreeLLMAPIAdapter, "freellmapi", {**FREE, "routed_via_format": "full"})
    assert (await full.chat(req())).routed_via == "freellmapi:groq/llama-3.3-70b-versatile"

    route.side_effect = [httpx.Response(200, json=completion())]
    assert (await adapter.chat(req())).routed_via == "freellmapi"


async def test_freellmapi_stream_routed_via(mock: respx.MockRouter) -> None:
    mock.post("http://oracle-arm:3001/v1/chat/completions").respond(
        200, content=sse({"model": "m", "choices": [{"delta": {"content": "a"}}]}),
        headers={"content-type": "text/event-stream", "x-routed-via": "google/gemini-2.5-flash"},
    )
    chunks = [c async for c in make(FreeLLMAPIAdapter, "freellmapi", dict(FREE)).stream(req())]
    assert chunks[0].routed_via == "freellmapi:google"


async def test_freellmapi_health_ping_then_models(mock: respx.MockRouter) -> None:
    ping = mock.get("http://oracle-arm:3001/api/ping").respond(200, json={"ok": True})
    models = mock.get("http://oracle-arm:3001/v1/models").respond(401, json={"error": "bad key"})
    adapter = make(FreeLLMAPIAdapter, "freellmapi", dict(FREE))
    status = await adapter.health()
    assert not status.ok and "ProviderAuthError" in status.detail and ping.called
    models.side_effect = [httpx.Response(200, json={"data": [{"id": "auto"}]})]
    assert (await adapter.health()).ok
    ping.side_effect = [httpx.ConnectError("refused")]
    down = await adapter.health()
    assert not down.ok and "ProviderUnavailable" in down.detail
