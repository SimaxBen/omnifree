"""kind=summary: the persistent daily summary push (stable web push tag, renotify false, ntfy low priority)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx
import pytest
import pywebpush

from hub_api import daily_summary, notifier, webpush
from hub_api.appdata import local_tz
from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from tests.hub_api import fakes_apps as fa
from tests.hub_api.test_push import PRIVATE, PUBLIC, _sub

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 13, 0, tzinfo=local_tz())
TOPIC = "omnifree-test-topic-x9k2"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    fa.reset()
    for mod in (exp_mod, notes_mod, notify_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(notify_mod, "local_now", lambda: NOW)
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", PRIVATE)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(503)))
    log: dict[str, list[dict[str, Any]]] = {"ntfy": [], "webpush": []}

    def ntfy(request: httpx.Request) -> httpx.Response:
        log["ntfy"].append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"msg{len(log['ntfy'])}"})

    class Ok:
        status_code = 201
        reason = "Created"
        text = ""

    def fake_webpush(**kwargs: Any) -> Ok:
        log["webpush"].append(json.loads(kwargs["data"]))
        return Ok()

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(ntfy))
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    return log


@pytest.fixture
def hub(make_hub, sent):
    h = make_hub(fa.apps_plugins())
    assert h.post("/push/subscribe", json=_sub(1)).status_code == 200
    return h


def _seed(hub) -> None:
    for body in (
        {"title": "Dentiste", "due": "2026-10-21", "due_time": "16:30", "priority": "high"},
        {"title": "Courses", "due": "2026-10-21"},
        {"title": "Rendre le livre", "due": "2026-10-23"},
        {"title": "Très loin", "due": "2026-12-01"},
        {"title": "Oublié", "due": "2026-10-01"},
    ):
        assert hub.post("/tasks", json=body).status_code == 201
    assert hub.post("/money/incomes", json={"date": "2026-10-01", "amount": 10000, "source": "Salaire"}).status_code == 201
    assert hub.post("/expenses", json={"date": "2026-10-05", "merchant": "Marjane", "total": 2500,
                                       "category": "groceries"}).status_code == 201
    assert hub.put("/money/budgets/groceries", json={"amount": 3000}).status_code == 200


def test_money_line_and_lines() -> None:
    assert daily_summary.money_line(None) is None
    assert daily_summary.money_line({"currency": "MAD", "income": {"total": 0}, "spent": {"total": 0}}) is None
    assert daily_summary.money_line({"currency": "MAD", "income": {"total": 0}, "spent": {"total": 120}}) == \
        "Dépensé ce mois : 120 MAD"
    line = daily_summary.money_line({"currency": "MAD", "income": {"total": 1000}, "spent": {"total": 1500}, "net": -500,
                                     "categories": [{"budget": 400, "spent": 100}, {"budget": None, "spent": 9},
                                                    {"budget": 50, "spent": 80}]})
    assert line == "Reste ce mois : −500 MAD (revenus 1 000 MAD)\nBudgets : 300 MAD disponibles"
    assert daily_summary.summary_lines([], [], 0, None, TODAY, NOW) == ["Rien de prévu aujourd'hui."]


def test_summary_push_replaces_silently(hub, sent) -> None:
    _seed(hub)
    preview = hub.post("/notify/run?kind=summary&preview=1").json()
    assert preview["skipped"] == "preview" and not sent["ntfy"]
    assert preview["title"] == "Aujourd'hui, 21 octobre : 2 tâches"
    lines = preview["message"].splitlines()
    assert lines[:4] == ["Tâches :", "• 16:30 ❗ Dentiste", "• Courses", "• 23 octobre Rendre le livre"]
    assert "Très loin" not in preview["message"]  # more than a week ahead
    assert "En retard : 1 tâche" in lines
    assert "Reste ce mois : 7 500 MAD (revenus 10 000 MAD)" in lines and "Budgets : 500 MAD disponibles" in lines

    out = hub.post("/notify/run?kind=summary").json()
    assert out["sent"] is True
    wp = sent["webpush"][-1]
    assert wp["tag"] == "omnifree-summary" and wp["renotify"] is False
    assert wp["url"].endswith("/#/home")
    assert sent["ntfy"][-1]["priority"] == 2 and "summary" in sent["ntfy"][-1]["tags"]
    # Same hour slot: a QStash retry is deduplicated; a forced run sends again with the same tag.
    assert hub.post("/notify/run?kind=summary").json()["skipped"] == "already sent"
    assert hub.post("/notify/run?kind=summary&force=1").json()["sent"] is True
    assert [p["tag"] for p in sent["webpush"]] == ["omnifree-summary", "omnifree-summary"]


def test_summary_is_a_kind_with_a_pref(hub, sent) -> None:
    st = hub.get("/notify/status").json()
    assert "summary" in st["kinds"] and st["prefs"]["summary"] is True
    hub.put("/notify/prefs", json={"prefs": {"summary": False}})
    assert hub.post("/notify/run?kind=summary").json()["skipped"] == "turned off in the app"
    # The other kinds keep renotifying (no "renotify" key in their payload).
    assert "renotify" not in json.loads(webpush.build_payload("t", "b", tag="x"))
    assert json.loads(webpush.build_payload("t", "b", tag="x", renotify=False))["renotify"] is False


def test_empty_day(hub, sent) -> None:
    out = hub.post("/notify/run?kind=summary&preview=1").json()
    assert out["title"] == "Aujourd'hui, 21 octobre" and out["message"] == "Rien de prévu aujourd'hui."
