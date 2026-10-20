from __future__ import annotations

import base64
import json

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.hub_api.conftest import INSTINCT_TOKEN, TOKEN

# ------------------------------------------------------------------ plugins / switchboard


def test_list_plugins_with_health_and_latency(hub):
    data = hub.get("/plugins", params={"health": "true"}).json()
    by_name = {p["name"]: p for p in data["plugins"]}
    assert by_name["store_big"]["healthy"] is True
    assert by_name["store_big"]["latency_ms"] == 2.5
    assert by_name["llm_locked"]["enabled"] is False
    assert by_name["llm_locked"]["missing_secrets"] == ["OMNIFREE_TEST_MISSING_KEY"]
    assert data["chains"]["llm"] == ["llm_a", "llm_b"]
    assert data["chains"]["storage"] == ["store_small", "store_big"]


def test_list_plugins_filtered_by_kind(hub):
    data = hub.get("/plugins", params={"kind": "dns"}).json()
    assert [p["name"] for p in data["plugins"]] == ["pihole"]
    assert list(data["chains"]) == ["dns"]


def test_plugin_detail_and_health(hub):
    detail = hub.get("/plugins/llm_b").json()
    assert detail["settings"]["upstream"] == "groq"
    assert detail["class_meta"]["display_name"] == "Fake LLM"
    assert hub.get("/plugins/nope").status_code == 404
    health = hub.post("/plugins/llm_a/health").json()
    assert health["ok"] is True and health["provider"] == "llm_a"


def test_disable_enable_persists_to_yaml(hub):
    r = hub.post("/plugins/llm_a/disable")
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert hub.yaml()["plugins"]["llm_a"]["enabled"] is False
    # Disabled provider is skipped by the router.
    assert hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "x"}]}).headers["X-Routed-Via"] == "llm_b:groq"
    r = hub.put("/plugins/llm_a/enabled", json={"enabled": True})
    assert r.json()["enabled"] is True
    assert hub.yaml()["plugins"]["llm_a"]["enabled"] is True


def test_reorder_persists_and_changes_routing(hub):
    r = hub.post("/plugins/reorder", json={"kind": "llm", "names": ["llm_b"]})
    assert r.status_code == 200
    assert r.json()["chain"] == ["llm_b", "llm_a"]
    plugins = hub.yaml()["plugins"]
    assert plugins["llm_b"]["priority"] == 10 and plugins["llm_a"]["priority"] == 20
    assert hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "x"}]}).headers["X-Routed-Via"] == "llm_b:groq"
    bad = hub.post("/plugins/reorder", json={"kind": "llm", "names": ["store_big"]})
    assert bad.status_code == 400


def test_priority_settings_and_reset(make_hub, plugins):
    plugins["llm_a"]["settings"]["behaviour"] = "quota"
    h = make_hub(plugins)
    h.post("/llm/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert h.get("/plugins/llm_a").json()["exhausted_until"] is not None
    r = h.patch("/plugins/llm_a/settings", json={"settings": {"behaviour": None}})
    assert r.status_code == 200 and "behaviour" not in r.json()["settings"]
    assert "behaviour" not in h.yaml()["plugins"]["llm_a"]["settings"]
    # Endpoint/TLS keys cannot be redirected over the API (would leak provider keys).
    for key in ("base_url", "verify_tls", "webdav_url"):
        denied = h.patch("/plugins/llm_a/settings", json={"settings": {key: "https://attacker.invalid"}})
        assert denied.status_code == 403 and key in denied.json()["detail"]
    assert "base_url" not in h.yaml()["plugins"]["llm_a"]["settings"]
    reset = h.post("/plugins/llm_a/reset").json()
    assert reset["exhausted_until"] is None and reset["breaker"] == "closed"
    assert h.post("/llm/chat", json={"messages": [{"role": "user", "content": "x"}]}).headers["X-Routed-Via"] == "llm_a"
    r = h.put("/plugins/llm_b/priority", json={"priority": 1})
    assert r.json()["chain"] == ["llm_b", "llm_a"]


# ------------------------------------------------------------------ websocket


def test_ws_rejects_bad_token(hub):
    with pytest.raises(WebSocketDisconnect):
        with hub.client.websocket_connect("/ws?access_token=wrong") as ws:
            ws.receive_json()


def test_ws_streams_router_events(hub):
    with hub.client.websocket_connect(f"/ws?access_token={TOKEN}&types=router.*") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["types"] == ["router.*"]
        hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        event = ws.receive_json()
        assert event["type"] == "router.success"
        assert event["provider"] == "llm_a"
        assert event["data"]["op"] == "chat"
        ws.send_json({"op": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_ws_subprotocol_auth_resubscribe_and_replay(hub):
    hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    with hub.client.websocket_connect("/ws?types=router.success&replay=5", subprotocols=["bearer", TOKEN]) as ws:
        assert ws.receive_json()["type"] == "hello"
        replayed = ws.receive_json()
        assert replayed["type"] == "router.success"
        ws.send_json({"op": "subscribe", "types": ["dns.*"]})
        assert ws.receive_json() == {"type": "subscribed", "types": ["dns.*"]}
        hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "filtered out"}]})
        hub.post("/dns/blocking", json={"enabled": True})
        assert ws.receive_json()["type"] == "dns.blocking_changed"


# ------------------------------------------------------------------ agent proxy + event callback


def _instinct(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST" and request.url.path == "/tasks":
            body = json.loads(request.content)
            return httpx.Response(202, json={"id": "t1", "goal": body["goal"], "status": "queued"})
        if request.url.path == "/tasks":
            return httpx.Response(200, json={"items": [{"id": "t1", "status": "running"}]})
        if request.url.path == "/tasks/t1":
            return httpx.Response(200, json={"id": "t1", "status": "running", "steps": []})
        if request.url.path == "/tasks/t1/cancel":
            return httpx.Response(200, json={"id": "t1", "status": "cancelled"})
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "unknown task"})

    return handler


def test_agent_proxy_submit_status_cancel(make_hub):
    calls: list[httpx.Request] = []
    h = make_hub(instinct_handler=_instinct(calls))
    r = h.post("/agent/tasks", json={"goal": "find the weather", "start_url": "https://example.com", "max_steps": 5})
    assert r.status_code == 202, r.text
    assert r.json()["id"] == "t1"
    sent = calls[0]
    assert sent.headers["authorization"] == f"Bearer {INSTINCT_TOKEN}"
    assert json.loads(sent.content) == {"goal": "find the weather", "start_url": "https://example.com", "max_steps": 5}
    assert h.get("/agent/tasks").json()["items"][0]["id"] == "t1"
    assert h.get("/agent/tasks/t1").json()["status"] == "running"
    assert h.post("/agent/tasks/t1/cancel").json()["status"] == "cancelled"
    assert h.get("/agent/tasks/zzz").status_code == 404
    assert h.get("/agent/tasks/..%2Fhealthz").status_code in (400, 404)
    assert h.post("/agent/tasks/a.b/cancel").status_code == 400
    assert h.get("/agent/health").json() == {"ok": True}
    assert h.post("/agent/tasks", json={"goal": ""}).status_code == 422


def test_agent_unreachable_falls_back_to_cached_state(make_hub):
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    h = make_hub(instinct_handler=down)
    assert h.post("/agent/tasks", json={"goal": "x"}).status_code == 502
    h.post("/agent/events", json={"type": "instinct.task.step", "data": {"task_id": "t9", "step": 2, "status": "running"}})
    cached = h.get("/agent/tasks/t9").json()
    assert cached["stale"] is True and cached["status"] == "running"
    assert h.get("/agent/tasks/unknown").status_code == 502


def test_agent_events_reach_ws_and_screenshot_endpoint(hub):
    jpeg = b"\xff\xd8\xff\xe0fakejpeg"
    with hub.client.websocket_connect(f"/ws?access_token={TOKEN}&types=instinct.*") as ws:
        ws.receive_json()
        r = hub.post(
            "/agent/events",
            json=[
                {"type": "instinct.task.step", "data": {"task_id": "t1", "step": 1, "action": {"action": "goto"}}},
                {"type": "instinct.task.screenshot", "data": {"task_id": "t1", "step": 1, "image_b64": base64.b64encode(jpeg).decode()}},
            ],
        )
        assert r.json() == {"accepted": 2}
        step = ws.receive_json()
        shot = ws.receive_json()
    assert step["type"] == "instinct.task.step" and step["data"]["step"] == 1
    assert shot["type"] == "instinct.task.screenshot"
    assert base64.b64decode(shot["data"]["image_b64"]) == jpeg
    r = hub.get("/agent/tasks/t1/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == jpeg


def test_agent_events_reject_foreign_types_and_require_auth(hub):
    assert hub.post("/agent/events", json={"type": "router.success", "data": {}}).status_code == 400
    assert hub.client.post("/agent/events", json={"type": "instinct.task.step", "data": {}}).status_code == 401
