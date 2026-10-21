"""/notify: ntfy pushes (JSON publish API, mocked), per-kind composition, D1 dedupe, prefs, budget changes."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from hub_api import notifier
from hub_api.routers import expenses as exp_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers.notify import budget_changes, fmt_money
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 21)  # a Wednesday
TOPIC = "omnifree-test-topic-x9k2"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    fa.reset()
    for mod in (exp_mod, notes_mod, notify_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        log.append({"url": str(request.url), "auth": request.headers.get("authorization"), **body})
        return httpx.Response(200, json={"id": f"msg{len(log)}", "event": "message"})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


@pytest.fixture
def app_hub(make_hub, sent):
    return make_hub(fa.apps_plugins())


def _seed(hub) -> None:
    for body in (
        {"title": "Payer la facture Inwi", "due": "2026-10-21", "priority": "high"},
        {"title": "Appeler Karim", "due": "2026-10-21"},
        {"title": "Vieux truc", "due": "2026-10-10"},
        {"title": "Plus tard", "due": "2026-11-02"},
        {"title": "Sans date"},
    ):
        assert hub.post("/tasks", json=body).status_code == 201
    for body in (
        {"date": "2026-10-20", "merchant": "Marjane", "total": 120, "category": "groceries"},
        {"date": "2026-10-20", "merchant": "Tram", "total": 6.5, "category": "transport"},
        {"date": "2026-10-15", "merchant": "Hotel", "total": 1500, "category": "travel"},
        {"date": "2026-09-12", "merchant": "Old", "total": 300, "category": "other"},
    ):
        assert hub.post("/expenses", json=body).status_code == 201


def test_pure_helpers() -> None:
    assert fmt_money(1234.5, "MAD") == "1 234,50 MAD"
    assert fmt_money(120, "EUR") == "120 EUR"
    payload = notifier.build_payload("t", "msg", title="Été ✓", priority=9, tags=["a"], click="https://x/#/notes/tasks",
                                     actions=[{"action": "view"}] * 5)
    assert payload["priority"] == 5 and payload["title"] == "Été ✓" and len(payload["actions"]) == 3
    items = [
        {"provider": "groq", "metric": "requests", "window": "day", "status": "over", "fraction": 1.02},
        {"provider": "r2", "metric": "bytes", "window": None, "status": "warn", "fraction": 0.85},
        {"provider": "neon", "metric": "bytes", "window": "month", "status": "ok", "fraction": 0.1},
        {"provider": "b2", "metric": "bytes", "window": None, "status": "warn", "fraction": 0.9},
    ]
    current, fresh = budget_changes(items, {"r2/bytes/total": "warn", "groq/requests/day": "warn", "b2/bytes/total": "over"})
    assert current == {"groq/requests/day": "over", "r2/bytes/total": "warn", "b2/bytes/total": "warn"}
    assert [i["provider"] for i in fresh] == ["groq"]  # warn -> over is new; same status or over -> warn is not


def test_status_prefs_and_test_push(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    st = app_hub.get("/notify/status").json()
    assert st["configured"] is True and st["topic"] == TOPIC and st["server"] == "https://ntfy.sh"
    assert st["subscribe_url"] == f"https://ntfy.sh/{TOPIC}" and st["token_set"] is False
    assert st["prefs"] == {k: True for k in ("morning", "evening", "weekly", "budget", "month", "inbox")}

    r = app_hub.post("/notify/test")
    assert r.status_code == 200 and r.json()["sent"] is True
    msg = sent[-1]
    assert msg["url"] == "https://ntfy.sh" and msg["topic"] == TOPIC and msg["title"] == "OmniFree : test réussi ✓"
    assert msg["auth"] is None and msg["click"] == "https://omnifree.ilyasouhnine.tech/#/more/notifications"
    assert app_hub.post("/notify/test").json()["sent"] is True  # the test push is never deduplicated
    assert [x["kind"] for x in app_hub.get("/notify/status").json()["recent"]] == ["test", "test"]

    r = app_hub.put("/notify/prefs", json={"prefs": {"evening": False}})
    assert r.json()["prefs"]["evening"] is False and r.json()["prefs"]["morning"] is True
    assert app_hub.put("/notify/prefs", json={"prefs": {"nope": True}}).status_code == 422
    out = app_hub.post("/notify/run?kind=evening").json()
    assert out["sent"] is False and out["skipped"] == "turned off in the app"

    monkeypatch.setenv("NTFY_SERVER", "https://push.example.org/")
    monkeypatch.setenv("NTFY_TOKEN", "tk_secret")
    app_hub.post("/notify/test")
    assert sent[-1]["url"] == "https://push.example.org" and sent[-1]["auth"] == "Bearer tk_secret"


def test_not_configured_and_ntfy_errors(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTFY_TOPIC")
    assert app_hub.get("/notify/status").json()["configured"] is False
    r = app_hub.post("/notify/test")
    assert r.status_code == 409 and "NTFY_TOPIC" in r.json()["detail"]
    out = app_hub.post("/notify/run?kind=morning").json()
    assert out["sent"] is False and "not configured" in out["skipped"]
    assert app_hub.post("/notify/run?kind=nope").status_code == 422

    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda req: httpx.Response(429, text="limit reached")))
    r = app_hub.post("/notify/run?kind=morning")
    assert r.status_code == 502 and "429" in r.json()["detail"]
    assert app_hub.get("/notify/status").json()["recent"][0]["status"] == "failed"
    # A failed push does not block the retry (QStash retries on 5xx).
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "ok"})))
    assert app_hub.post("/notify/run?kind=morning").json()["sent"] is True


def test_morning_dedupe_and_content(app_hub, sent) -> None:
    _seed(app_hub)
    fa.sql("CREATE TABLE IF NOT EXISTS app_inbox_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    from hub_api.appdata import now_iso

    fa.sql("INSERT INTO app_inbox_state VALUES ('receipts_sync', ?, ?)",
           [json.dumps({"last_sync_at": now_iso(), "last_result": {"created": [{"id": "a"}, {"id": "b"}]}}), now_iso()])
    preview = app_hub.post("/notify/run?kind=morning&preview=1").json()
    assert preview["skipped"] == "preview" and not sent

    out = app_hub.post("/notify/run?kind=morning")
    assert out.status_code == 200 and out.json()["sent"] is True
    msg = sent[-1]
    assert msg["title"] == "Bonjour : 2 tâches aujourd'hui"
    lines = msg["message"].split("\n")
    assert lines[:2] == ["• ❗ Payer la facture Inwi", "• Appeler Karim"]
    assert "En retard : 1 tâche" in lines and "Hier : 126,50 MAD (2 dépenses)" in lines
    assert "Import e-mail : 2 nouvelles dépenses" in lines
    assert msg["click"] == "https://omnifree.ilyasouhnine.tech/#/notes/tasks"
    assert [a["label"] for a in msg["actions"]] == ["Tâches", "Dépenses"] and msg["tags"] == ["sunrise"]

    again = app_hub.post("/notify/run?kind=morning").json()
    assert again["sent"] is False and again["skipped"] == "already sent" and len(sent) == 1
    assert app_hub.post("/notify/run?kind=morning&force=1").json()["sent"] is True and len(sent) == 2


def test_evening_only_when_something_to_say(app_hub, sent) -> None:
    out = app_hub.post("/notify/run?kind=evening").json()  # no task due, no expense today -> the nudge only
    assert out["sent"] is True and sent[-1]["title"] == "Ce soir : les dépenses du jour ?"
    assert sent[-1]["click"].endswith("#/more/expenses")

    fa.sql("DELETE FROM app_notify_log")
    app_hub.post("/expenses", json={"date": "2026-10-21", "merchant": "Cafe", "total": 12})
    out = app_hub.post("/notify/run?kind=evening").json()
    assert out["sent"] is False and out["skipped"] == "nothing to report"

    app_hub.post("/tasks", json={"title": "Rendre le livre", "due": "2026-10-21"})
    out = app_hub.post("/notify/run?kind=evening").json()
    assert out["sent"] is True and sent[-1]["title"] == "Ce soir : 1 tâche encore ouverte"
    assert "dépenses" not in sent[-1]["message"]


def test_weekly_and_month(app_hub, sent) -> None:
    _seed(app_hub)
    out = app_hub.post("/notify/run?kind=weekly").json()
    assert out["sent"] is True
    msg = sent[-1]
    assert msg["title"] == "Ta semaine : 1 626,50 MAD dépensés"
    assert msg["message"].startswith("Du 14 octobre au 20 octobre : 3 dépenses")
    assert "Surtout : voyages (1 500 MAD)." in msg["message"]
    assert app_hub.post("/notify/run?kind=weekly").json()["skipped"] == "already sent"

    out = app_hub.post("/notify/run?kind=month").json()
    assert out["sent"] is True
    msg = sent[-1]
    assert msg["title"] == "Bilan de octobre : 1 626,50 MAD dépensés"
    assert "Octobre au 21 : 3 dépenses, 1 626,50 MAD (mois dernier : 300 MAD)." in msg["message"]
    assert "Récurrent" not in msg["message"]  # no money tables: expense totals only


def test_month_uses_recurring_table_when_present(app_hub, sent) -> None:
    _seed(app_hub)
    app_hub.get("/notify/status")  # creates the schemas
    fa.sql("CREATE TABLE app_money_recurring (id TEXT PRIMARY KEY, name TEXT, amount REAL, currency TEXT,"
           " kind TEXT, frequency TEXT, day_of_month INTEGER)")
    fa.sql("INSERT INTO app_money_recurring VALUES ('r1', 'Salaire', 8000, 'MAD', 'income', 'monthly', 25),"
           " ('r2', 'Loyer', 3000, 'MAD', 'expense', 'monthly', 1), ('r3', 'Domaine', 120, 'MAD', 'expense', 'yearly', 3)")
    out = app_hub.post("/notify/run?kind=month&preview=1").json()
    assert "Récurrent / mois : +8 000 MAD revenus, −3 010 MAD charges." in out["message"]
    assert "Reste estimé (MAD) : +3 363,50 MAD." in out["message"]


def test_budget_pushes_only_new_alerts(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    report: dict[str, Any] = {"items": [
        {"provider": "groq", "metric": "requests", "window": "day", "status": "over", "fraction": 1.0},
        {"provider": "r2", "metric": "bytes", "window": None, "status": "warn", "fraction": 0.84},
        {"provider": "neon", "metric": "bytes", "window": None, "status": "ok", "fraction": 0.2},
    ]}

    async def fake_budget(router: Any) -> dict[str, Any]:
        return report

    monkeypatch.setattr(notify_mod, "build_budget", fake_budget)
    out = app_hub.post("/notify/run?kind=budget").json()
    assert out["sent"] is True
    msg = sent[-1]
    assert msg["title"] == "Quotas : 1 limite dépassée" and msg["priority"] == 4
    assert msg["message"].split("\n") == ["⛔ groq requests/day : 100 %", "⚠ r2 bytes : 84 %"]

    assert app_hub.post("/notify/run?kind=budget").json()["skipped"] == "nothing to report" and len(sent) == 1
    report["items"][2]["status"] = "warn"  # neon gets close: only it is pushed
    report["items"][2]["fraction"] = 0.81
    app_hub.post("/notify/run?kind=budget")
    assert len(sent) == 2 and sent[-1]["message"] == "⚠ neon bytes : 81 %" and sent[-1]["priority"] == 3
    report["items"][1]["status"] = "ok"  # r2 recovers, then gets close again: pushed again
    app_hub.post("/notify/run?kind=budget")
    report["items"][1]["status"] = "warn"
    app_hub.post("/notify/run?kind=budget")
    assert len(sent) == 3 and sent[-1]["message"] == "⚠ r2 bytes : 84 %"
