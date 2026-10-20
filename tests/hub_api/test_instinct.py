"""Instinct agent tests: action parsing, plan->act loop, LLM fallback, service API + hub callback."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from instinct.agent import InstinctAgent, parse_action
from instinct.config import InstinctSettings
from instinct.llm import build_agent_llm
from instinct.models import Observation, PageElement, TaskState
from instinct.service import create_app

JPEG = b"\xff\xd8\xff\xe0instinct"


class FakeSession:
    """Scripted in-memory browser (test double for PlaywrightSession)."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.typed: list[tuple[str, str, bool]] = []
        self.clicked: list[str] = []
        self.closed = False

    async def goto(self, url: str) -> str:
        self.url = url
        return f"navigated to {url}"

    async def click(self, target: str) -> str:
        if "missing" in target:
            raise RuntimeError("element not found")
        self.clicked.append(target)
        return f"clicked {target}"

    async def type(self, target: str, text: str, submit: bool) -> str:
        self.typed.append((target, text, submit))
        if submit:
            self.url = "https://search.example/results"
        return f"typed into {target}"

    async def extract(self, target: str | None, max_chars: int) -> str:
        return "Weather: 21C sunny"[:max_chars]

    async def screenshot(self) -> bytes:
        return JPEG

    async def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Example",
            elements=[PageElement(id=1, tag="input", type="text", text="Search"), PageElement(id=2, tag="button", text="Go")],
            text="Example page",
        )

    async def current_url(self) -> str:
        return self.url

    async def close(self) -> None:
        self.closed = True


class FakeBrowsers:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.closed = False

    async def new_session(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        self.closed = True


def llm_transport(replies: list[str], *, seen: list[httpx.Request] | None = None, header: str | None = None,
                  primary_status: int = 200) -> httpx.MockTransport:
    script = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == "llm.test" and primary_status != 200:
            return httpx.Response(primary_status, json={"error": "rate limited"}, headers={"retry-after": "30"})
        content = script.pop(0) if len(script) > 1 else script[0]
        headers = {"x-upstream": "groq"} if header else {}
        return httpx.Response(
            200,
            json={
                "id": "c1", "model": "auto",
                "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            headers=headers,
        )

    return httpx.MockTransport(handler)


def settings(**kw: Any) -> InstinctSettings:
    base: dict[str, Any] = {
        "llm_base_url": "http://llm.test/v1",
        "llm_api_key_name": "",
        "hub_url": "",
        "max_steps": 6,
    }
    base.update(kw)
    return InstinctSettings(**base)


# ------------------------------------------------------------------ parsing


def test_parse_action_variants():
    assert parse_action('{"action": "goto", "url": "https://a.b"}').url == "https://a.b"
    fenced = 'Plan: search first.\n```json\n{"thought": "t", "action": "type", "element": "#1", "text": "hi", "submit": true}\n```'
    action = parse_action(fenced)
    assert action.action == "type" and action.element == 1 and action.submit is True
    nested = parse_action('{"tool": "CLICK", "args": {"selector": "text=Go"}}')
    assert nested.action == "click" and nested.selector == "text=Go"
    assert parse_action('{"action": "done", "text": "42"}').result == "42"
    with pytest.raises(ValueError):
        parse_action("I will click the button")
    with pytest.raises(ValueError):
        parse_action('{"action": "click"}')
    with pytest.raises(ValueError):
        parse_action('{"action": "fly"}')


# ------------------------------------------------------------------ agent loop


async def _run_agent(replies: list[str], **kw: Any) -> tuple[TaskState, list[tuple[str, dict]], FakeSession, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    cfg = settings(**{k: v for k, v in kw.items() if k != "header"})
    llm = build_agent_llm(cfg, transport=llm_transport(replies, seen=seen, header=kw.get("header")))
    events: list[tuple[str, dict]] = []

    async def emit(type_: str, **data: Any) -> None:
        events.append((str(type_), data))

    shots: dict[str, bytes] = {}
    agent = InstinctAgent(llm, cfg, emit, lambda tid, _s, img: shots.__setitem__(tid, img))
    session = FakeSession()
    task = TaskState(goal="find the weather", start_url="https://search.example", max_steps=cfg.max_steps)
    await agent.run(task, session)
    await llm.close()
    assert not shots or shots[task.id] == JPEG
    return task, events, session, seen


async def test_agent_plan_act_loop_completes():
    task, events, session, seen = await _run_agent(
        [
            '{"thought": "search", "action": "type", "element": 1, "text": "weather paris", "submit": true}',
            '{"action": "extract"}',
            '{"action": "click", "selector": "#missing"}',
            '{"action": "done", "result": "21C and sunny"}',
        ],
        llm_routed_via_header="x-upstream",
        header=True,
    )
    assert task.status == "completed" and task.result == "21C and sunny"
    assert session.typed == [('[data-omni-id="1"]', "weather paris", True)]
    steps = [s for s in task.steps if s.step > 0]
    assert steps[1].result == "Weather: 21C sunny"
    assert steps[2].error and "element not found" in steps[2].error  # browser errors are fed back
    assert steps[0].routed_via == "freellmapi:groq"
    types = [t for t, _ in events]
    assert types[-1] == "instinct.task.completed"
    shots = [d for t, d in events if t == "instinct.task.screenshot"]
    assert shots and base64.b64decode(shots[0]["image_b64"]) == JPEG
    assert shots[1]["url"] == "https://search.example/results"
    # The prompt carries the observation and the history of previous steps.
    last_prompt = json.loads(seen[-1].content)["messages"][1]["content"]
    assert "[1] <input type=text> 'Search'" in last_prompt
    assert "ERROR RuntimeError: element not found" in last_prompt
    assert json.loads(seen[0].content)["model"] == "auto"


async def test_agent_step_limit_and_invalid_replies():
    task, events, _session, _seen = await _run_agent(["not json at all"], max_steps=3)
    assert task.status == "failed" and "step limit" in (task.error or "")
    assert all(s.error for s in task.steps if s.step > 0)
    assert events[-1][0] == "instinct.task.failed"


async def test_agent_llm_fallback_on_quota():
    seen: list[httpx.Request] = []
    cfg = settings(fallback_base_url="http://ollama.test/v1", fallback_model="qwen2.5:7b")
    llm = build_agent_llm(cfg, transport=llm_transport(['{"action": "done", "result": "ok"}'], seen=seen, primary_status=429))
    reply = await llm.complete([{"role": "user", "content": "hi"}])
    await llm.close()
    assert reply.routed_via == "ollama"
    assert [r.url.host for r in seen] == ["llm.test", "ollama.test"]
    assert json.loads(seen[1].content)["model"] == "qwen2.5:7b"


async def test_agent_fails_cleanly_when_llm_down():
    cfg = settings()
    llm = build_agent_llm(cfg, transport=llm_transport(["{}"], primary_status=503))
    events: list[str] = []

    async def emit(type_: str, **_data: Any) -> None:
        events.append(str(type_))

    task = TaskState(goal="x", max_steps=3)
    await InstinctAgent(llm, cfg, emit).run(task, FakeSession())
    await llm.close()
    assert task.status == "failed" and "LLM unavailable" in (task.error or "")
    assert events[-1] == "instinct.task.failed"


# ------------------------------------------------------------------ service


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_service_runs_task_and_pushes_events_to_hub():
    hub_calls: list[httpx.Request] = []

    def hub_handler(request: httpx.Request) -> httpx.Response:
        hub_calls.append(request)
        return httpx.Response(202, json={"accepted": 1})

    browsers = FakeBrowsers()
    cfg = settings(hub_url="http://hub.test")
    app = create_app(
        cfg,
        browser_factory=browsers,
        api_token="svc-token",
        hub_token="hub-token",
        llm_transport=llm_transport(['{"action": "goto", "url": "https://example.org"}', '{"action": "done", "result": "found it"}']),
        hub_transport=httpx.MockTransport(hub_handler),
    )
    auth = {"Authorization": "Bearer svc-token"}
    with TestClient(app) as client:
        assert client.get("/healthz").json()["hub_forwarding"] is True
        assert client.post("/tasks", json={"goal": "x"}).status_code == 401
        r = client.post("/tasks", json={"goal": "open example", "max_steps": 4}, headers=auth)
        assert r.status_code == 202
        task_id = r.json()["id"]
        _wait_for(lambda: client.get(f"/tasks/{task_id}", headers=auth).json()["status"] == "completed")
        detail = client.get(f"/tasks/{task_id}", headers=auth).json()
        assert detail["result"] == "found it" and detail["max_steps"] == 4
        assert client.get("/tasks", headers=auth).json()["items"][0]["id"] == task_id
        shot = client.get(f"/tasks/{task_id}/screenshot", headers=auth)
        assert shot.status_code == 200 and shot.content == JPEG
        assert client.get("/tasks/nope", headers=auth).status_code == 404
        _wait_for(lambda: any(json.loads(c.content)["type"] == "instinct.task.completed" for c in hub_calls))
    assert browsers.sessions[0].closed and browsers.closed
    sent = [json.loads(c.content) for c in hub_calls]
    assert all(c.headers["authorization"] == "Bearer hub-token" for c in hub_calls)
    assert all(c.url.path == "/agent/events" for c in hub_calls)
    types = [e["type"] for e in sent]
    assert types[0] == "instinct.task.created"
    assert "instinct.task.screenshot" in types
    assert all(e["data"]["task_id"] == task_id for e in sent)


def test_service_cancel_and_fail_closed():
    class SlowSession(FakeSession):
        async def observe(self) -> Observation:
            await asyncio.sleep(10)
            return await super().observe()

    class SlowBrowsers(FakeBrowsers):
        async def new_session(self) -> FakeSession:
            session = SlowSession()
            self.sessions.append(session)
            return session

    app = create_app(settings(), browser_factory=SlowBrowsers(), api_token="svc-token", hub_token="",
                     llm_transport=llm_transport(['{"action": "done"}']))
    auth = {"Authorization": "Bearer svc-token"}
    with TestClient(app) as client:
        task_id = client.post("/tasks", json={"goal": "slow"}, headers=auth).json()["id"]
        _wait_for(lambda: client.get(f"/tasks/{task_id}", headers=auth).json()["status"] == "running")
        r = client.post(f"/tasks/{task_id}/cancel", headers=auth)
        assert r.json()["status"] == "cancelled"
        with client.websocket_connect("/ws?access_token=svc-token") as ws:
            client.post("/tasks", json={"goal": "second"}, headers=auth)
            assert ws.receive_json()["type"] == "instinct.task.created"

    locked = create_app(settings(), browser_factory=FakeBrowsers(), api_token="", hub_token="",
                        llm_transport=llm_transport(["{}"]))
    with TestClient(locked) as client:
        assert client.post("/tasks", json={"goal": "x"}, headers={"Authorization": "Bearer x"}).status_code == 503
