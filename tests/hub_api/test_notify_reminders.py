"""Timed tasks in the pushes: the 15-minute reminders (window, dedupe, prefs), the signed "Fait ✓" link,
and times in the morning/evening pushes."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest

from hub_api import notifier
from hub_api.appdata import local_tz
from hub_api.routers import expenses as exp_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers.notify import done_sig, reminder_window
from tests.hub_api import fakes_apps as fa
from tests.hub_api.conftest import TOKEN

TZ = local_tz()
TODAY = date(2026, 10, 21)
TOPIC = "omnifree-test-topic-x9k2"


class Clock:
    now = datetime(2026, 10, 21, 14, 50, tzinfo=TZ)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    fa.reset()
    Clock.now = datetime(2026, 10, 21, 14, 50, tzinfo=TZ)
    for mod in (exp_mod, notes_mod, notify_mod):
        monkeypatch.setattr(mod, "local_today", lambda: Clock.now.date())
    monkeypatch.setattr(notify_mod, "local_now", lambda: Clock.now)
    monkeypatch.setattr(notes_mod, "local_now", lambda: Clock.now)
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"msg{len(log)}"})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


@pytest.fixture
def app_hub(make_hub, sent):
    return make_hub(fa.apps_plugins())


def _task(hub, title: str, due: str | None = None, at: str | None = None, **extra: Any) -> dict[str, Any]:
    body = {"title": title, **({"due": due} if due else {}), **({"due_time": at} if at else {}), **extra}
    r = hub.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ window


def test_window_bounds() -> None:
    now = datetime(2026, 10, 21, 14, 50, tzinfo=TZ)
    assert reminder_window(now) == (now - timedelta(minutes=5), now + timedelta(minutes=15))


def test_reminders_window_and_content(app_hub, sent) -> None:
    inside = _task(app_hub, "Appeler Karim", "2026-10-21", "15:00", details="pour le devis")
    edge_late = _task(app_hub, "Juste passé", "2026-10-21", "14:45")  # now - 5 min: still in
    _task(app_hub, "Trop tôt", "2026-10-21", "15:06")  # now + 16 min: next run
    _task(app_hub, "Trop tard", "2026-10-21", "14:44")  # now - 6 min: missed window
    _task(app_hub, "Sans heure", "2026-10-21")
    _task(app_hub, "Autre jour", "2026-10-22", "15:00")
    high = _task(app_hub, "Urgent", "2026-10-21", "15:05", priority="high")  # exactly now + 15 min
    done = _task(app_hub, "Déjà fait", "2026-10-21", "14:55")
    app_hub.patch(f"/tasks/{done['id']}", json={"done": True})

    preview = app_hub.post("/notify/run?kind=reminders&preview=1").json()
    assert preview["skipped"] == "preview" and preview["title"] == "3 rappels" and not sent

    out = app_hub.post("/notify/run?kind=reminders")
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["sent"] is True and body["count"] == 3
    titles = [m["title"] for m in sent]
    assert titles == ["⏰ Juste passé", "⏰ Appeler Karim", "❗ ⏰ Urgent"]  # soonest first

    msg = sent[1]
    assert msg["message"] == "15:00 (dans 10 min)\npour le devis"
    assert sent[0]["message"] == "14:45 (il y a 5 min)" and sent[2]["message"].startswith("15:05 (dans 15 min)")
    assert msg["priority"] == 4 and msg["tags"] == ["alarm_clock"]
    assert msg["click"] == "https://omnifree.ilyasouhnine.tech/#/notes/tasks"
    done_action, view_action = msg["actions"]
    assert done_action == {
        "action": "http", "label": "Fait ✓", "method": "POST", "clear": True,
        "url": f"https://api.ilyasouhnine.tech/notify/done/{inside['id']}?sig={done_sig(TOKEN, inside['id'])}",
    }
    assert view_action["action"] == "view" and view_action["label"] == "Tâches"
    assert {edge_late["id"], high["id"]} <= {m["actions"][0]["url"].split("/")[-1].split("?")[0] for m in sent}


def test_reminders_dedupe_across_retries_and_overlapping_windows(app_hub, sent) -> None:
    t = _task(app_hub, "RDV", "2026-10-21", "15:00")
    assert app_hub.post("/notify/run?kind=reminders").json()["count"] == 1
    again = app_hub.post("/notify/run?kind=reminders").json()  # a QStash retry
    assert again["sent"] is False and again["skipped"] == "already sent" and len(sent) == 1
    Clock.now = Clock.now + timedelta(minutes=15)  # the next run: 15:00 is still inside [14:55, 15:20]
    assert app_hub.post("/notify/run?kind=reminders").json()["skipped"] == "already sent" and len(sent) == 1
    # Moving the task to another time is a new reminder.
    app_hub.patch(f"/tasks/{t['id']}", json={"due_time": "15:10"})
    assert app_hub.post("/notify/run?kind=reminders").json()["count"] == 1 and len(sent) == 2
    log = [r["kind"] for r in app_hub.get("/notify/status").json()["recent"]]
    assert log == ["reminders", "reminders"]
    keys = [r[0] for r in fa.sql("SELECT key FROM app_notify_log ORDER BY key")]
    assert keys == [f"reminder:{t['id']}:2026-10-21T15:00", f"reminder:{t['id']}:2026-10-21T15:10"]


def test_reminders_across_midnight(app_hub, sent) -> None:
    Clock.now = datetime(2026, 10, 21, 23, 50, tzinfo=TZ)
    _task(app_hub, "Minuit cinq", "2026-10-22", "00:05")
    _task(app_hub, "Hier soir", "2026-10-21", "23:46")
    out = app_hub.post("/notify/run?kind=reminders").json()
    assert out["count"] == 2 and [m["title"] for m in sent] == ["⏰ Hier soir", "⏰ Minuit cinq"]


def test_reminders_prefs_nothing_and_failures(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    assert app_hub.post("/notify/run?kind=reminders").json()["skipped"] == "nothing to report"
    _task(app_hub, "RDV", "2026-10-21", "15:00")
    assert app_hub.get("/notify/status").json()["prefs"]["reminders"] is True  # on by default
    app_hub.put("/notify/prefs", json={"prefs": {"reminders": False}})
    out = app_hub.post("/notify/run?kind=reminders").json()
    assert out["skipped"] == "turned off in the app" and not sent
    app_hub.put("/notify/prefs", json={"prefs": {"reminders": True}})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda req: httpx.Response(500, text="down")))
    r = app_hub.post("/notify/run?kind=reminders")
    assert r.status_code == 502  # QStash retries...
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "x"})))
    assert app_hub.post("/notify/run?kind=reminders").json()["count"] == 1  # ...and the retry goes through


def test_reminder_without_hub_token_has_no_done_button(make_hub, sent) -> None:
    hub = make_hub(fa.apps_plugins(), token=None)
    from hub_api.routers.notify import compose_reminder

    push = compose_reminder(hub.state, {"id": "t1", "title": "x", "due": "2026-10-21", "due_time": "15:00",
                                        "at": Clock.now + timedelta(minutes=10)}, Clock.now)
    assert push.http_actions == [] and push.actions == [("Tâches", "#/notes/tasks")]


# ------------------------------------------------------------------ signed done link


def test_done_link_marks_only_that_task(app_hub, sent) -> None:
    a = _task(app_hub, "A", "2026-10-21", "15:00")
    b = _task(app_hub, "B", "2026-10-21", "15:00")
    url = f"/notify/done/{a['id']}"
    # No bearer token: the ntfy app calls it directly.
    r = app_hub.client.post(f"{url}?sig={done_sig(TOKEN, a['id'])}")
    assert r.status_code == 200 and r.json() == {"ok": True, "id": a["id"], "title": "A", "done": True}
    tasks = {t["id"]: t for t in app_hub.get("/tasks?status=all").json()["tasks"]}
    assert tasks[a["id"]]["done"] is True and tasks[a["id"]]["done_at"]
    assert tasks[b["id"]]["done"] is False
    # Idempotent: a second tap keeps the first done_at.
    first = tasks[a["id"]]["done_at"]
    assert app_hub.client.post(f"{url}?sig={done_sig(TOKEN, a['id'])}").status_code == 200
    assert app_hub.get("/tasks?status=done").json()["tasks"][0]["done_at"] == first


def test_done_link_rejects_bad_signatures(app_hub, sent) -> None:
    a = _task(app_hub, "A", "2026-10-21", "15:00")
    b = _task(app_hub, "B", "2026-10-21", "15:00")
    good_b = done_sig(TOKEN, b["id"])
    for path in (
        f"/notify/done/{a['id']}",  # no signature
        f"/notify/done/{a['id']}?sig=",
        f"/notify/done/{a['id']}?sig=deadbeef",
        f"/notify/done/{a['id']}?sig={good_b}",  # another task's signature
        f"/notify/done/{a['id']}?sig={done_sig('other-token', a['id'])}",  # signed with another key
        f"/notify/done/{a['id']}?sig={done_sig(TOKEN, a['id']).upper()}",
        f"/notify/done/nope?sig={done_sig(TOKEN, 'nope')}",  # valid signature, unknown task
    ):
        assert app_hub.client.post(path).status_code == 404, path
    assert app_hub.client.get(f"/notify/done/{a['id']}?sig={done_sig(TOKEN, a['id'])}").status_code == 405  # POST only
    assert all(not t["done"] for t in app_hub.get("/tasks").json()["tasks"])
    # The key is derived: neither the hub token nor the topic appears in the signature.
    sig = done_sig(TOKEN, a["id"])
    assert len(sig) == 64 and TOKEN not in sig and sig != done_sig(TOPIC, a["id"])


def test_done_link_404_without_hub_token(make_hub, sent) -> None:
    hub = make_hub(fa.apps_plugins(), token=None)
    assert hub.client.post(f"/notify/done/x?sig={done_sig('', 'x')}").status_code == 404


# ------------------------------------------------------------------ morning / evening with times


def test_morning_lists_times_in_order(app_hub, sent) -> None:
    Clock.now = datetime(2026, 10, 21, 8, 0, tzinfo=TZ)
    _task(app_hub, "Sans heure", "2026-10-21", priority="high")
    _task(app_hub, "Déjeuner", "2026-10-21", "12:30")
    _task(app_hub, "Café", "2026-10-21", "09:15")
    _task(app_hub, "Normal", "2026-10-21")
    app_hub.post("/notify/run?kind=morning")
    lines = sent[-1]["message"].split("\n")
    assert lines[:4] == ["• 09:15 Café", "• 12:30 Déjeuner", "• ❗ Sans heure", "• Normal"]
    assert sent[-1]["title"] == "Bonjour : 4 tâches aujourd'hui"


def test_evening_separates_upcoming_timed_tasks(app_hub, sent) -> None:
    Clock.now = datetime(2026, 10, 21, 20, 0, tzinfo=TZ)
    app_hub.post("/expenses", json={"date": "2026-10-21", "merchant": "Cafe", "total": 12})
    _task(app_hub, "Plus tard ce soir", "2026-10-21", "21:30")
    out = app_hub.post("/notify/run?kind=evening&preview=1").json()
    assert out["title"] == "Ce soir : 1 tâche à venir" and out["message"] == "À venir : 21:30 Plus tard ce soir"

    _task(app_hub, "Raté", "2026-10-21", "18:00")
    _task(app_hub, "Sans heure", "2026-10-21")
    out = app_hub.post("/notify/run?kind=evening&preview=1").json()
    assert out["title"] == "Ce soir : 2 tâches encore ouvertes, 1 à venir"
    assert out["message"].split("\n") == ["• 18:00 Raté", "• Sans heure", "À venir : 21:30 Plus tard ce soir"]


def test_reminders_every_three_hours_cover_the_next_three_hours(app_hub, sent) -> None:
    # The owner runs reminders every 3 h: `ahead=180` must cover every task until the next run.
    _task(app_hub, "Dans 2 h 50", "2026-10-21", "17:40")
    _task(app_hub, "Après le prochain passage", "2026-10-21", "17:51")
    out = app_hub.post("/notify/run?kind=reminders&ahead=180").json()
    assert out["count"] == 1 and [m["title"] for m in sent] == ["⏰ Dans 2 h 50"]
    assert app_hub.post("/notify/run?kind=reminders&ahead=2000").status_code == 422
