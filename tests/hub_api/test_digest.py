"""/digest: stats from D1 + Drive listing + router counters, LLM write-up with a template fallback, D1 cache."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest

from core.interfaces import StoredObject
from hub_api.routers import digest as digest_mod
from hub_api.routers import expenses as exp_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.digest import fallback_text, period_range, top_merchants
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)

WRITE_UP = {
    "markdown": "## Your week\n\nYou spent **120 MAD**.",
    "highlights": ["Groceries led spending", "2 tasks done"],
    "suggestions": ["Clear the overdue task"],
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    for mod in (exp_mod, notes_mod, digest_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)


@pytest.fixture
def app_hub(make_hub):
    hub = make_hub(fa.apps_plugins())
    now = datetime.now(timezone.utc)

    async def listing(prefix: str = "", *, providers: Any = None, limit: int = 1000) -> dict[str, Any]:
        return {
            "r2": [StoredObject(key="a.jpg", size=1000, last_modified=now),
                   StoredObject(key="old.bin", size=5, last_modified=datetime(2020, 1, 1, tzinfo=timezone.utc)),
                   StoredObject(key="nodate.bin", size=7)],
            "plain": "backend down",
        }

    hub.state.router.storage_list = listing
    return hub


def _seed(hub) -> None:
    for body in (
        {"date": "2026-10-19", "merchant": "Marjane", "total": 100, "category": "groceries"},
        {"date": "2026-10-15", "merchant": "marjane", "total": 20, "category": "groceries"},
        {"date": "2026-10-16", "merchant": "Tram", "total": 6, "category": "transport"},
        {"date": "2026-10-14", "merchant": "Hotel", "total": 80, "currency": "EUR", "category": "travel"},
        {"date": "2026-10-02", "merchant": "Early", "total": 999, "category": "other"},  # outside the week
    ):
        assert hub.post("/expenses", json=body).status_code == 201
    t1 = hub.post("/tasks", json={"title": "done one"}).json()
    hub.patch(f"/tasks/{t1['id']}", json={"done": True})
    hub.post("/tasks", json={"title": "late", "due": "2026-10-10"})
    hub.post("/tasks", json={"title": "later", "due": "2026-11-10"})
    hub.post("/notes", json={"body": "a note"})


def test_pure_helpers() -> None:
    assert period_range("week", TODAY) == (date(2026, 10, 14), TODAY)
    assert period_range("month", TODAY) == (date(2026, 10, 1), TODAY)
    tops = top_merchants([
        {"merchant": "Marjane", "currency": "MAD", "total": 10},
        {"merchant": "marjane ", "currency": "MAD", "total": 5},
        {"merchant": "Cafe", "currency": "MAD", "total": 12},
    ])
    assert tops == [{"merchant": "Marjane", "currency": "MAD", "total": 15.0, "count": 2},
                    {"merchant": "Cafe", "currency": "MAD", "total": 12.0, "count": 1}]
    empty = {"expenses": {"total_by_currency": {}, "by_category": {}, "count": 0, "top_merchants": []},
             "tasks": {"done": 0, "open": 0, "overdue": 2}, "notes": {"created": 0, "voice": 0},
             "drive": {"uploaded_files": None, "uploaded_bytes": None}, "llm": {"requests_by_provider": {}}}
    text = fallback_text("week", date(2026, 10, 14), TODAY, empty)
    assert text["markdown"].startswith("## This week") and "**nothing**" in text["markdown"]
    assert text["highlights"] == ["A quiet period: nothing recorded yet."]
    assert "Clear or reschedule the 2 overdue tasks." in text["suggestions"]


def test_digest_stats_llm_and_cache(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(app_hub)
    fa.CHAT_SCRIPT["groq"].append(json.dumps(WRITE_UP))
    r = app_hub.get("/digest")
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["period"], d["from"], d["to"], d["source"], d["cached"]) == ("week", "2026-10-14", "2026-10-20", "llm", False)
    assert d["markdown"] == WRITE_UP["markdown"] and d["highlights"] == WRITE_UP["highlights"]
    assert d["suggestions"] == WRITE_UP["suggestions"]
    exp = d["stats"]["expenses"]
    assert exp["count"] == 4 and exp["total_by_currency"] == {"MAD": 126.0, "EUR": 80.0}
    assert exp["by_category"]["groceries"] == {"MAD": 120.0}
    assert exp["top_merchants"][0] == {"merchant": "Marjane", "currency": "MAD", "total": 120.0, "count": 2}
    assert d["stats"]["tasks"] == {"done": 1, "open": 2, "overdue": 1}
    assert d["stats"]["notes"] == {"created": 1, "voice": 0}
    assert d["stats"]["drive"] == {"uploaded_files": 1, "uploaded_bytes": 1000}
    assert d["stats"]["llm"] == {"requests_by_provider": {}}

    # The LLM got the stats as JSON.
    _name, req = fa.CHAT_LOG[-1]
    sent = json.loads(req.messages[-1].content)
    assert sent["stats"]["expenses"]["count"] == 4 and "weekly" in req.messages[0].content

    calls = len(fa.CHAT_LOG)
    again = app_hub.get("/digest", params={"period": "week"}).json()
    assert again["cached"] is True and again["generated_at"] == d["generated_at"] and len(fa.CHAT_LOG) == calls

    # The router counts requests at the real UTC time: read the counters for a week ending today.
    monkeypatch.setattr(digest_mod, "local_today", lambda: datetime.now(timezone.utc).date())
    fa.CHAT_SCRIPT["groq"].append(json.dumps(WRITE_UP))
    fresh = app_hub.get("/digest", params={"refresh": 1}).json()
    assert fresh["cached"] is False and len(fa.CHAT_LOG) == calls + 1
    assert fresh["stats"]["llm"]["requests_by_provider"].get("groq", 0) >= 1  # the first digest's call


def test_digest_month_and_template_fallback(app_hub) -> None:
    _seed(app_hub)
    fa.CHAT_SCRIPT["groq"].append("Sorry, I cannot help with that.")
    fa.CHAT_SCRIPT["vision_b"].append("still not json")
    fa.CHAT_SCRIPT["gemini"].append("nope")
    d = app_hub.get("/digest", params={"period": "month"}).json()
    assert (d["from"], d["to"], d["source"]) == ("2026-10-01", "2026-10-20", "template")
    assert d["stats"]["expenses"]["count"] == 5
    assert d["markdown"].startswith("## This month")
    assert any("groceries" in h or "other" in h for h in d["highlights"])
    assert app_hub.get("/digest", params={"period": "year"}).status_code == 422


def test_digest_llm_down_uses_template(make_hub) -> None:
    plugins = fa.apps_plugins()
    for name in ("groq", "gemini", "vision_b"):
        plugins.pop(name)
    hub = make_hub(plugins)
    d = hub.get("/digest").json()
    assert d["source"] == "template" and d["model"] is None
    assert d["stats"]["expenses"]["count"] == 0 and d["highlights"]
