from __future__ import annotations

import json

from tests.hub_api.conftest import AUTH, TOKEN


def _sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_healthz_is_public(hub):
    r = hub.client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_protected_routes_require_bearer_token(hub):
    assert hub.client.get("/plugins").status_code == 401
    assert hub.client.get("/plugins", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert hub.client.get("/plugins", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert hub.client.get("/plugins", headers=AUTH).status_code == 200
    assert hub.client.get(f"/plugins?access_token={TOKEN}").status_code == 200


def test_hub_fails_closed_without_configured_token(make_hub):
    h = make_hub(token=None)
    r = h.client.get("/plugins", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


def test_fallback_token_name_is_honoured(make_hub, monkeypatch):
    monkeypatch.setenv("OMNIFREE_HUB_TOKEN", "alt-token")
    h = make_hub(token=None)
    assert h.client.get("/plugins", headers={"Authorization": "Bearer alt-token"}).status_code == 200


def test_cors_preflight_and_exposed_headers(hub):
    r = hub.client.options(
        "/llm/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers={"Origin": "http://localhost:5173"})
    assert "x-routed-via" in r.headers["access-control-expose-headers"].lower()


def test_chat_sets_routed_via_header(hub):
    r = hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["X-Routed-Via"] == "llm_a"
    body = r.json()
    assert body["routed_via"] == "llm_a"
    assert body["content"] == "llm_a says: hi"


def test_chat_fails_over_on_quota_and_reports_upstream(make_hub, plugins):
    plugins["llm_a"]["settings"]["behaviour"] = "quota"
    h = make_hub(plugins)
    r = h.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["X-Routed-Via"] == "llm_b:groq"
    assert any(a.startswith("llm_a:quota") for a in r.json()["attempts"])
    # The quota cooldown is visible on the gauges endpoint.
    q = h.get("/llm/quotas").json()["providers"]
    assert q["llm_a"]["exhausted_until"] is not None
    assert q["llm_b"]["breaker"] == "closed"


def test_chat_with_pinned_provider_and_no_fallback(make_hub, plugins):
    plugins["llm_b"]["settings"]["behaviour"] = "down"
    h = make_hub(plugins)
    r = h.post(
        "/llm/chat",
        json={"messages": [{"role": "user", "content": "x"}], "provider": "llm_b", "allow_fallback": False},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "no_provider_available"
    assert body["attempts"] and body["attempts"][0].startswith("llm_b:unavailable")


def test_chat_unknown_pinned_provider_is_404(hub):
    r = hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "x"}], "provider": "nope"})
    assert r.status_code == 404


def test_stream_sse_with_routed_via_header(make_hub, plugins):
    plugins["llm_a"]["settings"]["behaviour"] = "down"
    h = make_hub(plugins)
    r = h.post("/llm/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["X-Routed-Via"] == "llm_b:groq"
    events = _sse_events(r.text)
    assert [e for e, _ in events][-1] == "done"
    text = "".join(d["delta"] for e, d in events if e == "chunk")
    assert text == "Hello from llm_b"
    assert events[-1][1]["chunks"] == 4


def test_chat_with_stream_flag_uses_sse(hub):
    r = hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["X-Routed-Via"] == "llm_a"


def test_stream_all_down_returns_json_error(make_hub, plugins):
    plugins["llm_a"]["settings"]["behaviour"] = "down"
    plugins["llm_b"]["settings"]["behaviour"] = "quota"
    h = make_hub(plugins)
    r = h.post("/llm/stream", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert r.json()["kind"] == "llm"


def test_models_and_quotas(hub):
    hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    models = hub.get("/llm/models").json()
    assert models["chain"] == ["llm_a", "llm_b"]
    assert [m["id"] for m in models["providers"]["llm_a"]] == ["a-model"]
    quotas = hub.get("/llm/quotas").json()["providers"]
    day = next(q for q in quotas["llm_a"]["quotas"] if q["unit"] == "requests" and q["window"] == "day")
    assert day["used"] == 1 and day["limit"] == 1000 and day["remaining"] == 999
    assert "llm_locked" not in quotas  # disabled providers are not gauged


def test_model_routing_prefers_host(hub):
    r = hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}], "model": "b-model"})
    assert r.headers["X-Routed-Via"] == "llm_b:groq"
    assert r.json()["model"] == "b-model"
