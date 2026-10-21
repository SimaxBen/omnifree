"""MCP server on /mcp: handshake, tool schemas, tool calls on the hub fakes, auth, resources, serverless (no network)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.config import Settings, get_settings
from core.interfaces import CronAdapter, CronJob, HealthStatus
from hub_api.app import create_app
from hub_api.settings import HubSettings
from tests.hub_api import fakes, fakes_apps, fakes_rag
from tests.hub_api.conftest import TOKEN

PROTOCOL = "2025-11-25"
BASE_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
R = "tests.hub_api.fakes_rag"
F = "tests.hub_api.fakes"

EXPECTED_TOOLS = {
    "llm_chat", "llm_models", "drive_list", "drive_search", "drive_read_text", "drive_upload_text",
    "drive_share_link", "ask_drive", "rag_index", "notes_search", "notes_create", "notes_get", "tasks_list",
    "tasks_add", "tasks_complete", "expenses_month", "expense_add", "links_create", "drops_create",
    "schedules_list", "schedule_create", "budget_status", "chats_list",
}


CRON_JOBS: list[CronJob] = []


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fakes_apps.reset()
    fakes_rag.reset()
    CRON_JOBS.clear()


class FakeCron(CronAdapter):
    async def create(self, job: CronJob) -> CronJob:
        created = job.model_copy(update={"id": f"job{len(CRON_JOBS) + 1}"})
        CRON_JOBS.append(created)
        return created

    async def list(self) -> list[CronJob]:
        return list(CRON_JOBS)

    async def delete(self, job_id: str) -> None:  # pragma: no cover - not exposed over MCP
        raise AssertionError("delete must not be reachable")

    async def trigger(self, job_id: str) -> None:  # pragma: no cover
        pass

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


class MCP:
    """Minimal JSON-RPC client for the stateless Streamable HTTP endpoint."""

    def __init__(self, client: TestClient, token: str | None = TOKEN) -> None:
        self.client = client
        self.token = token
        self._id = 0

    def headers(self, **extra: str) -> dict[str, str]:
        h = {**BASE_HEADERS, "Mcp-Protocol-Version": PROTOCOL, **extra}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def post(self, payload: dict[str, Any], **headers: str) -> Any:
        return self.client.post("/mcp", content=json.dumps(payload), headers=self.headers(**headers))

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        r = self.post(body)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        data = r.json()
        assert data["id"] == self._id
        return data

    def result(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.rpc(method, params)
        assert "error" not in data, data
        return data["result"]

    def call(self, _tool: str, /, **arguments: Any) -> dict[str, Any]:
        return self.result("tools/call", {"name": _tool, "arguments": arguments})

    def ok(self, _tool: str, /, **arguments: Any) -> Any:
        res = self.call(_tool, **arguments)
        assert res["isError"] is False, res
        return res["structuredContent"]


def mcp_plugins() -> dict[str, Any]:
    plugins = fakes_apps.apps_plugins()
    plugins["turso"] = {"module": f"{R}:FakeTurso", "kind": "db", "group": "databases", "priority": 20}
    return plugins


@pytest.fixture
def mcp_hub(make_hub):
    return make_hub(mcp_plugins())


@pytest.fixture
def mcp(mcp_hub) -> MCP:
    return MCP(mcp_hub.client)


# --------------------------------------------------------------------------- protocol


def test_initialize_handshake(mcp: MCP) -> None:
    res = mcp.result(
        "initialize",
        {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
    )
    assert res["protocolVersion"] == PROTOCOL
    assert res["serverInfo"]["name"] == "omnifree"
    assert {"tools", "resources", "prompts"} <= set(res["capabilities"])
    assert "OmniFree" in res["instructions"]
    # stateless: no session id is issued
    r = mcp.post({"jsonrpc": "2.0", "id": 99, "method": "ping"})
    assert "mcp-session-id" not in {k.lower() for k in r.headers}
    # the initialized notification is accepted without a body
    r = mcp.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert mcp.result("ping") == {}


def test_tools_list_has_every_tool_with_schemas(mcp: MCP) -> None:
    tools = {t["name"]: t for t in mcp.result("tools/list")["tools"]}
    assert set(tools) == EXPECTED_TOOLS
    for t in tools.values():
        assert t["description"], t["name"]
        assert t["inputSchema"]["type"] == "object"
    assert not any("delete" in name for name in tools)
    chat = tools["llm_chat"]["inputSchema"]["properties"]
    assert {"prompt", "messages", "model", "provider"} <= set(chat)
    read = tools["drive_read_text"]["inputSchema"]
    assert set(read["required"]) == {"provider", "key"}
    assert read["properties"]["max_bytes"]["maximum"] == 1_000_000
    assert tools["tasks_add"]["inputSchema"]["properties"]["priority"]["enum"] == ["low", "normal", "high"]
    assert "food" in tools["expense_add"]["inputSchema"]["properties"]["category"]["enum"]
    assert tools["notes_get"]["annotations"]["readOnlyHint"] is True
    assert tools["notes_create"]["annotations"]["destructiveHint"] is False


def test_prompts(mcp: MCP) -> None:
    names = {p["name"] for p in mcp.result("prompts/list")["prompts"]}
    assert names == {"weekly_review", "inbox_triage"}
    got = mcp.result("prompts/get", {"name": "inbox_triage", "arguments": {"notebook": "Work"}})
    assert "'Work'" in got["messages"][0]["content"]["text"]


def test_get_is_not_allowed_in_stateless_mode(mcp_hub) -> None:
    r = mcp_hub.client.get("/mcp", headers={**BASE_HEADERS, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 405 and r.headers["allow"] == "POST"


# --------------------------------------------------------------------------- auth


def test_auth_required(mcp_hub) -> None:
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for token in (None, "wrong"):
        r = MCP(mcp_hub.client, token=token).post(ping)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json()["error"]["code"] == -32001
    # the hub's ?access_token= convenience is not accepted for MCP
    r = mcp_hub.client.post(f"/mcp?access_token={TOKEN}", content=json.dumps(ping), headers=BASE_HEADERS)
    assert r.status_code == 401
    assert MCP(mcp_hub.client).post(ping).status_code == 200


def test_separate_mcp_token(make_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_API_TOKEN", "agent-only-token")
    hub = make_hub(mcp_plugins())
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert MCP(hub.client, token="agent-only-token").post(ping).status_code == 200
    assert MCP(hub.client, token=TOKEN).post(ping).status_code == 200  # hub token still works
    assert MCP(hub.client, token="other").post(ping).status_code == 401
    # the MCP token does not open the rest of the hub
    assert hub.client.get("/budget", headers={"Authorization": "Bearer agent-only-token"}).status_code == 401


def test_no_token_configured_fails_closed(make_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    hub = make_hub(mcp_plugins(), token=None)
    r = MCP(hub.client, token="anything").post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 503


def test_foreign_origin_rejected(mcp: MCP) -> None:
    r = mcp.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, Origin="https://evil.example")
    assert r.status_code == 403
    r = mcp.post({"jsonrpc": "2.0", "id": 2, "method": "ping"}, Origin="http://localhost:5173")
    assert r.status_code == 200


# --------------------------------------------------------------------------- tools


def test_llm_chat_and_models(mcp: MCP) -> None:
    fakes_apps.CHAT_SCRIPT["groq"].append("Bonjour!")
    out = mcp.ok("llm_chat", prompt="Say hi", system="Be brief")
    assert out["content"] == "Bonjour!" and out["routed_via"] == "groq"
    _name, request = fakes_apps.CHAT_LOG[-1]
    assert [m.role for m in request.messages] == ["system", "user"]
    out = mcp.ok("llm_chat", messages=[{"role": "user", "content": "x"}], provider="vision_b")
    assert out["routed_via"] == "vision_b"
    res = mcp.call("llm_chat")
    assert res["isError"] is True and "prompt" in res["content"][0]["text"]
    res = mcp.call("llm_chat", prompt="x", provider="r2")
    assert res["isError"] is True and "not llm" in res["content"][0]["text"]
    models = mcp.ok("llm_models")
    assert models["chain"][0] == "groq" and "g" in [m["id"] for m in models["providers"]["groq"]]


def test_drive_tools(mcp: MCP) -> None:
    out = mcp.ok("drive_upload_text", key="agent/notes.md", content="# Hello\nworld ✓", provider="r2")
    assert out["backend"] == "r2" and out["content_type"].startswith("text/markdown")
    fakes.STORES["r2"]["bin/photo.png"] = (b"\x89PNG\x00\x00", "image/png")

    listing = mcp.ok("drive_list", prefix="agent/")
    assert [i["key"] for i in listing["items"]] == ["agent/notes.md"]
    found = mcp.ok("drive_search", query="NOTES")
    assert found["count"] == 1

    text = mcp.ok("drive_read_text", provider="r2", key="agent/notes.md")
    assert text["content"] == "# Hello\nworld ✓" and text["truncated"] is False
    cut = mcp.ok("drive_read_text", provider="r2", key="agent/notes.md", max_bytes=9)
    assert cut["content"] == "# Hello\nw" and cut["truncated"] is True

    res = mcp.call("drive_read_text", provider="r2", key="bin/photo.png")
    assert res["isError"] is True and "not a text file" in res["content"][0]["text"]
    res = mcp.call("drive_read_text", provider="r2", key="missing.txt")
    assert res["isError"] is True and "not found" in res["content"][0]["text"]
    res = mcp.call("drive_read_text", provider="r2", key="../etc/passwd")
    assert res["isError"] is True and "invalid object key" in res["content"][0]["text"]

    link = mcp.ok("drive_share_link", provider="r2", key="agent/notes.md", expires_s=600)
    assert link["url"] == "https://signed.example/r2/agent/notes.md?exp=600"
    res = mcp.call("drive_share_link", provider="r2", key="agent/notes.md", expires_s=10)
    assert res["isError"] is True  # schema: expires_s >= 60


def test_notes_and_tasks(mcp: MCP) -> None:
    note = mcp.ok("notes_create", body="Buy milk\n\nand **bread**", notebook="Home", tags=["#Shopping"])
    assert note["title"] == "Buy milk" and note["tags"] == ["shopping"]
    mcp.ok("notes_create", body="x" * 1000, title="Long")
    found = mcp.ok("notes_search", q="bread")
    assert [n["id"] for n in found["notes"]] == [note["id"]]
    assert "preview" in found["notes"][0] and "body" not in found["notes"][0]
    assert {"name": "Home", "count": 1} in found["notebooks"]
    long = mcp.ok("notes_search", q="Long")["notes"][0]
    assert long["body_chars"] == 1000 and len(long["preview"]) <= 400
    full = mcp.ok("notes_get", id=note["id"])
    assert full["body"] == "Buy milk\n\nand **bread**"
    res = mcp.call("notes_get", id="nope")
    assert res["isError"] is True and "note not found (HTTP 404)" in res["content"][0]["text"]

    task = mcp.ok("tasks_add", title="Call the bank", due="2030-01-02", priority="high")
    assert task["due"] == "2030-01-02" and task["done"] is False
    rel = mcp.ok("tasks_add", title="Relative", due="tomorrow")
    assert rel["due"] and len(rel["due"]) == 10
    res = mcp.call("tasks_add", title="Bad", due="someday maybe")
    assert res["isError"] is True and "due date" in res["content"][0]["text"]
    open_ids = [t["id"] for t in mcp.ok("tasks_list")["tasks"]]
    assert set(open_ids) == {task["id"], rel["id"]}
    done = mcp.ok("tasks_complete", id=task["id"])
    assert done["done"] is True and done["done_at"]
    assert [t["id"] for t in mcp.ok("tasks_list", status="done")["tasks"]] == [task["id"]]


def test_expenses(mcp: MCP) -> None:
    out = mcp.ok("expense_add", total=42.5, merchant="Marjane", category="groceries", date="2026-03-04",
                 currency="dh", items=[{"name": "milk", "qty": 2, "price": 7}])
    assert out["currency"] == "MAD" and out["date"] == "2026-03-04"
    month = mcp.ok("expenses_month", month="2026-03")
    assert [e["id"] for e in month["expenses"]] == [out["id"]]
    assert month["summary"]
    res = mcp.call("expenses_month", month="March")
    assert res["isError"] is True and "YYYY-MM" in res["content"][0]["text"]
    res = mcp.call("expense_add", total=1, currency="not-a-currency")
    assert res["isError"] is True and "currency" in res["content"][0]["text"]


def test_links_and_drops(mcp: MCP) -> None:
    link = mcp.ok("links_create", url="https://example.com/a", code="docs")
    assert link["code"] == "docs" and link["short_url"].endswith("/s/docs")
    res = mcp.call("links_create", url="https://example.com/b", code="docs")
    assert res["isError"] is True and "already taken" in res["content"][0]["text"]
    res = mcp.call("links_create", url="ftp://x")
    assert res["isError"] is True and "http" in res["content"][0]["text"]

    fakes.STORES["r2"]["share/report.pdf"] = (b"%PDF-1.4", "application/pdf")
    drop = mcp.ok("drops_create", provider="r2", key="share/report.pdf", expires_in_hours=2, password="s3cret")
    assert drop["filename"] == "report.pdf" and drop["has_password"] is True and "/d/" in drop["url"]
    res = mcp.call("drops_create", provider="plain", key="share/report.pdf")
    assert res["isError"] is True and "cannot presign" in res["content"][0]["text"]


def test_schedules_without_cron_provider(mcp: MCP) -> None:
    assert mcp.ok("schedules_list") == {"jobs": [], "errors": {}}
    res = mcp.call("schedule_create", name="n", schedule="0 7 * * 1", target="https://example.com")
    assert res["isError"] is True and "no enabled cron provider" in res["content"][0]["text"]


def test_schedules(make_hub) -> None:
    plugins = mcp_plugins()
    plugins["qstash"] = {"module": "tests.hub_api.test_mcp:FakeCron", "kind": "cron", "group": "cron", "priority": 10}
    mcp = MCP(make_hub(plugins).client)
    job = mcp.ok("schedule_create", name="digest", schedule="0  7 * * 1", target="https://example.com/hook",
                 headers={"Authorization": "Bearer x", "X-Plain": "1"})
    assert job["provider"] == "qstash" and job["schedule"] == "0 7 * * 1" and job["method"] == "POST"
    assert job["headers"] == {"Authorization": "***", "X-Plain": "1"}  # secrets never echoed
    listed = mcp.ok("schedules_list")
    assert [j["id"] for j in listed["jobs"]] == ["job1"]
    res = mcp.call("schedule_create", name="bad", schedule="every monday", target="https://example.com")
    assert res["isError"] is True and "5 fields" in res["content"][0]["text"]


def test_budget_tool_and_resources(mcp: MCP) -> None:
    budget = mcp.ok("budget_status")
    assert {"items", "summary"} <= set(budget)  # same payload as GET /budget
    assert any(i["provider"] == "r2" for i in budget["items"])
    resources = {r["uri"] for r in mcp.result("resources/list")["resources"]}
    assert {"omnifree://budget", "omnifree://tasks/open"} <= resources
    templates = {t["uriTemplate"] for t in mcp.result("resources/templates/list")["resourceTemplates"]}
    assert "omnifree://notes/{id}" in templates

    content = mcp.result("resources/read", {"uri": "omnifree://budget"})["contents"][0]
    assert content["mimeType"] == "application/json"
    assert json.loads(content["text"]).keys() == budget.keys()

    task = mcp.ok("tasks_add", title="Resource task")
    tasks = json.loads(mcp.result("resources/read", {"uri": "omnifree://tasks/open"})["contents"][0]["text"])
    assert [t["id"] for t in tasks["tasks"]] == [task["id"]]

    note = mcp.ok("notes_create", body="Body text", title="Res note")
    text = mcp.result("resources/read", {"uri": f"omnifree://notes/{note['id']}"})["contents"][0]["text"]
    assert text == "# Res note\n\nBody text"
    missing = mcp.rpc("resources/read", {"uri": "omnifree://notes/nope"})
    assert "error" in missing


def test_rag_index_and_ask_drive(make_hub) -> None:
    hub = make_hub({
        "ragllm": {"module": f"{R}:FakeRagLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
                   "settings": {"default_model": "fake-chat", "embedding_models": ["fake-embed"]}},
        "drive": {"module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 10,
                  "settings": {"capacity_bytes": 10_000_000}},
        "turso": {"module": f"{R}:FakeTurso", "kind": "db", "group": "databases", "priority": 10},
    })
    mcp = MCP(hub.client)
    fakes.STORES["drive"]["docs/fruit.txt"] = (b"Apples are red. Bananas are yellow.", "text/plain")
    doc = mcp.ok("rag_index", provider="drive", key="docs/fruit.txt")["doc"]
    assert doc["chunks"] >= 1 and doc["error"] is None
    out = mcp.ok("ask_drive", question="What colour are apples?", top_k=3)
    assert out["answer"] == "Apples are red [1]."
    assert out["sources"][0]["key"] == "docs/fruit.txt"


# --------------------------------------------------------------------------- serverless (no lifespan)


def test_works_without_lifespan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Like a Vercel function: the ASGI lifespan never runs and each request may get a fresh event loop."""
    import yaml

    for var in ("OMNIFREE_HUB_API_TOKEN", "HUB_API_TOKEN", "OMNIFREE_HUB_TOKEN", "MCP_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "plugins": mcp_plugins()}), encoding="utf-8")
    settings = Settings(
        root_dir=tmp_path, providers_file=path, data_dir=tmp_path / "data", quota_db=tmp_path / "data" / "q.db",
        vault_path=tmp_path / "no.vault", hub_api_token=TOKEN, health_interval_s=0,
    )
    app = create_app(settings, HubSettings(background_health=False))
    client = TestClient(app)  # no `with`: lifespan not run; TestClient uses a new event loop per request
    mcp = MCP(client)
    assert mcp.result("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}})["serverInfo"]["name"] == "omnifree"
    fakes_apps.CHAT_SCRIPT["groq"].append("pong")
    assert mcp.ok("llm_chat", prompt="ping")["content"] == "pong"  # router (quota DB) opened lazily
    assert mcp.ok("tasks_add", title="t1")["title"] == "t1"
    assert len(mcp.ok("tasks_list")["tasks"]) == 1
    assert json.loads(mcp.result("resources/read", {"uri": "omnifree://budget"})["contents"][0]["text"])
    assert (tmp_path / "data" / "q.db").is_file()


def test_serverless_entrypoint_serves_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIFREE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIFREE_QUOTA_DB", str(tmp_path / "data" / "quota.db"))
    monkeypatch.setenv("OMNIFREE_HUB_API_TOKEN", "t0ken")
    monkeypatch.setenv("MCP_API_TOKEN", "agent")
    monkeypatch.setenv("OMNIFREE_VAULT_PATH", str(tmp_path / "missing.vault"))
    for var in ("OMNIFREE_STATE_BACKEND", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("hub_api.serverless", None)
    get_settings.cache_clear()
    try:
        serverless = importlib.import_module("hub_api.serverless")
        client = TestClient(serverless.app)
        mcp = MCP(client, token="agent")
        assert mcp.result("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}})["protocolVersion"] == PROTOCOL
        assert len(mcp.result("tools/list")["tools"]) == len(EXPECTED_TOOLS)
        assert MCP(client, token=None).post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    finally:
        sys.modules.pop("hub_api.serverless", None)
        get_settings.cache_clear()
