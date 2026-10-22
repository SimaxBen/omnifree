"""Tasks & calendar: recurring tasks, checklists, time blocking and voice-note appointments.

Google Calendar is the httpx.MockTransport fake of test_gcal (plus single-event GETs); the apps DB is SQLite.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
import pytest

from hub_api import gcal, recurrence, taskplus
from hub_api.appdata import local_tz
from hub_api.routers import notes as notes_mod
from hub_api.routers import tasks_plus as tasks_plus_mod
from tests.hub_api import fakes_apps as fa
from tests.hub_api.test_gcal import CAL, PRIMARY, SA_INFO, FakeGoogle
from tests.hub_api.test_mcp import MCP, mcp_plugins

TZ = local_tz()
DAY = date(2030, 3, 12)  # a Tuesday
NOW = datetime.combine(DAY, time(8, 0), TZ)


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), TZ)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    gcal.reset()
    monkeypatch.setattr(notes_mod, "local_today", lambda: DAY)
    monkeypatch.setattr(notes_mod, "local_now", lambda: NOW)
    monkeypatch.setattr(tasks_plus_mod, "local_now", lambda: NOW)


class Google(FakeGoogle):
    """FakeGoogle plus ``GET /calendars/{cal}/events/{id}`` (the undo checks the event's kind first)."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        import re

        path = request.url.path.removeprefix("/calendar/v3")
        m = re.fullmatch(r"/calendars/([^/]+)/events/([^/]+)", path)
        if request.method == "GET" and m:
            self.calls.append(("GET", path, {}))
            ev = self.events.get(m.group(1), {}).get(m.group(2))
            if ev is None or ev.get("status") == "cancelled":
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            return httpx.Response(200, json=ev)
        response = await super().handler(request)
        if request.method == "GET" and response.status_code == 200 and "calendarList" not in path:
            # like Google, list results carry the UTC offset of wall-clock times written with a timeZone
            page = json.loads(response.content)
            page["items"] = [_with_offset(item) for item in page.get("items") or []]
            return httpx.Response(200, json=page)
        return response


def _with_offset(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    for k in ("start", "end"):
        v = item.get(k) or {}
        if v.get("dateTime") and v.get("timeZone") and "+" not in v["dateTime"][10:]:
            out[k] = {**v, "dateTime": datetime.fromisoformat(v["dateTime"]).replace(tzinfo=TZ).isoformat()}
    return out


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    fake = Google()
    monkeypatch.setattr(gcal, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv(gcal.SA_KEY, json.dumps(SA_INFO))
    monkeypatch.setenv(gcal.ID_KEY, CAL)
    monkeypatch.setenv(gcal.READ_KEY, PRIMARY)  # the OmniFree calendar is not a read calendar: read separately
    return fake


@pytest.fixture
def hub(make_hub):
    return make_hub(fa.apps_plugins())


@pytest.fixture
def ghub(make_hub, google):
    return make_hub(fa.apps_plugins())


def _task(hub, **body: Any) -> dict[str, Any]:
    r = hub.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _all_tasks(hub) -> list[dict[str, Any]]:
    return hub.get("/tasks", params={"status": "all"}).json()["tasks"]


# ------------------------------------------------------------------ recurrence rules (pure)


@pytest.mark.parametrize(
    ("phrase", "rule", "rest"),
    [
        ("chaque lundi", {"freq": "weekly", "days": [0]}, ""),
        ("tous les jours à 8h", {"freq": "daily"}, "à 8h"),
        ("le 1er de chaque mois", {"freq": "monthly", "day": 1}, ""),
        ("every 3 days", {"freq": "interval", "interval": 3}, ""),
        ("every monday and thursday at 9", {"freq": "weekly", "days": [0, 3]}, "at 9"),
        ("tous les lundis et jeudis", {"freq": "weekly", "days": [0, 3]}, ""),
        ("on fridays", {"freq": "weekly", "days": [4]}, ""),
        ("every other day", {"freq": "interval", "interval": 2}, ""),
        ("un jour sur deux", {"freq": "interval", "interval": 2}, ""),
        ("toutes les 2 semaines", {"freq": "interval", "interval": 14}, ""),
        ("every weekday", {"freq": "weekly", "days": [0, 1, 2, 3, 4]}, ""),
        ("le week-end", {"freq": "weekly", "days": [5, 6]}, ""),
        ("every month on the 15th", {"freq": "monthly", "day": 15}, ""),
        ("monthly", {"freq": "monthly"}, ""),
        ("chaque année", {"freq": "yearly"}, ""),
        ("kol nhar m3a 7", {"freq": "daily"}, "m3a 7"),
        ("chaque lundi jusqu'au 2030-06-30", {"freq": "weekly", "days": [0], "until": "2030-06-30"}, ""),
    ],
)
def test_parse_recurrence(phrase: str, rule: dict[str, Any], rest: str) -> None:
    assert recurrence.parse_recurrence(phrase) == (rule, rest)


@pytest.mark.parametrize("phrase", ["call mom on monday", "rdv le 3 du mois prochain", "meeting friday 3pm",
                                    "dans 3 jours", "next week", ""])
def test_parse_recurrence_leaves_one_offs_alone(phrase: str) -> None:
    assert recurrence.parse_recurrence(phrase)[0] is None


def test_normalize_rule_defaults_and_errors() -> None:
    assert recurrence.normalize_rule("every week", DAY) == {"freq": "weekly", "days": [1]}  # Tuesday from the anchor
    assert recurrence.normalize_rule({"freq": "monthly"}, DAY) == {"freq": "monthly", "day": 12}
    assert recurrence.normalize_rule({"freq": "yearly"}, DAY) == {"freq": "yearly", "month": 3, "day": 12}
    assert recurrence.normalize_rule({"freq": "interval", "interval": 1}, DAY) == {"freq": "daily"}
    assert recurrence.normalize_rule(None, DAY) is None and recurrence.normalize_rule({}, DAY) is None
    for bad in ({"freq": "hourly"}, {"freq": "weekly", "days": [7]}, {"freq": "monthly", "day": 0},
                {"freq": "interval", "interval": 0}, {"freq": "daily", "until": "soon"}, "someday", 3):
        with pytest.raises(ValueError):
            recurrence.normalize_rule(bad, DAY)


def test_next_occurrences() -> None:
    weekly = {"freq": "weekly", "days": [0, 3]}
    assert recurrence.next_after(weekly, DAY) == date(2030, 3, 14)  # Tue -> Thu
    assert recurrence.next_after(weekly, date(2030, 3, 14)) == date(2030, 3, 18)  # Thu -> Mon
    m31 = {"freq": "monthly", "day": 31}
    assert recurrence.next_after(m31, date(2030, 1, 31)) == date(2030, 2, 28)  # short month: its last day
    assert recurrence.next_after(m31, date(2030, 2, 28)) == date(2030, 3, 31)
    leap = {"freq": "yearly", "month": 2, "day": 29}
    assert recurrence.next_after(leap, date(2028, 2, 29)) == date(2029, 2, 28)
    every3 = {"freq": "interval", "interval": 3}
    assert recurrence.next_due(every3, date(2030, 3, 1), DAY) == date(2030, 3, 13)  # cadence from Mar 1 kept
    daily = {"freq": "daily"}
    assert recurrence.next_due(daily, date(2030, 3, 9), DAY) == date(2030, 3, 13)  # late: no overdue copies
    assert recurrence.next_due(daily, date(2030, 3, 20), DAY) == date(2030, 3, 21)  # early: after its due date
    assert recurrence.next_due({**daily, "until": "2030-03-12"}, DAY, DAY) is None
    assert recurrence.first_on_or_after(weekly, DAY) == date(2030, 3, 14)
    assert recurrence.describe(weekly) == "Every Mon, Thu"
    assert recurrence.describe({"freq": "monthly", "day": 1}) == "Monthly on the 1st"
    assert recurrence.describe({"freq": "interval", "interval": 3, "until": "2030-05-01"}) == "Every 3 days until 2030-05-01"


# ------------------------------------------------------------------ recurring tasks (API)


def test_recurring_task_rolls_over_once(hub) -> None:
    t = _task(hub, title="Gym", due="2030-03-12", due_time="18:00", priority="high", details="legs",
              recurrence={"freq": "weekly", "days": [1, 4]}, checklist=[{"text": "shoes", "done": True}], estimate=60)
    assert t["recurrence"] == {"freq": "weekly", "days": [1, 4]} and t["recurrence_label"] == "Every Tue, Fri"
    r = hub.patch(f"/tasks/{t['id']}", json={"done": True})
    assert r.status_code == 200
    done = r.json()
    nxt = done["next"]
    assert done["done"] is True and done["next_id"] == nxt["id"]
    assert nxt["due"] == "2030-03-15" and nxt["due_time"] == "18:00" and nxt["done"] is False
    assert (nxt["title"], nxt["details"], nxt["priority"], nxt["estimate"]) == ("Gym", "legs", "high", 60)
    assert nxt["recurrence"] == t["recurrence"] and nxt["series_id"] == t["id"]
    assert [(i["text"], i["done"]) for i in nxt["checklist"]] == [("shoes", False)]  # a fresh checklist
    # undo + redo, or completing twice: still one next occurrence
    hub.patch(f"/tasks/{t['id']}", json={"done": False})
    again = hub.patch(f"/tasks/{t['id']}", json={"done": True}).json()
    assert "next" not in again and again["next_id"] == nxt["id"]
    assert len(_all_tasks(hub)) == 2
    # the series id stays the first task's
    third = hub.patch(f"/tasks/{nxt['id']}", json={"done": True}).json()["next"]
    assert third["series_id"] == t["id"] and third["due"] == "2030-03-19"


def test_recurrence_phrases_and_validation(hub) -> None:
    t = _task(hub, title="Water plants", recurrence="every 3 days")  # no due: starts today
    assert t["due"] == "2030-03-12" and t["recurrence"] == {"freq": "interval", "interval": 3}
    t = _task(hub, title="Bins", recurrence="chaque lundi")
    assert t["due"] == "2030-03-18"
    t = _task(hub, title="Pills", due_time=None, recurrence="daily")
    assert t["due"] == "2030-03-12"
    # a time already past today (it is 08:00): starts tomorrow
    r = hub.patch(f"/tasks/{_task(hub, title='Run')['id']}", json={"due": "2030-03-12", "due_time": "07:00"})
    assert r.status_code == 200
    late = _task(hub, title="Stretch", recurrence="daily", due="2030-03-12", due_time="07:00")
    assert late["due"] == "2030-03-12"  # an explicit due date is kept as given
    assert hub.post("/tasks", json={"title": "x", "recurrence": "sometimes"}).status_code == 422
    assert hub.post("/tasks", json={"title": "x", "recurrence": {"freq": "daily", "until": "2030-01-01"}}).status_code == 422
    # PATCH: set, then clearing the date is refused while it repeats, then clear the recurrence
    plain = _task(hub, title="Rent")
    r = hub.patch(f"/tasks/{plain['id']}", json={"recurrence": {"freq": "monthly", "day": 1}})
    assert r.json()["due"] == "2030-04-01" and r.json()["recurrence_label"] == "Monthly on the 1st"
    assert hub.patch(f"/tasks/{plain['id']}", json={"due": None}).status_code == 422
    r = hub.patch(f"/tasks/{plain['id']}", json={"recurrence": None, "due": None})
    assert r.status_code == 200 and r.json()["recurrence"] is None and r.json()["due"] is None
    # a time and a rule with no day: the rule gives the day (it is 08:00, so today's 07:30 has passed)
    r = hub.patch(f"/tasks/{plain['id']}", json={"due": None, "due_time": "07:30", "recurrence": "daily"})
    assert (r.json()["due"], r.json()["due_time"]) == ("2030-03-13", "07:30")
    assert hub.patch(f"/tasks/{plain['id']}", json={"due_time": "09:00", "recurrence": None, "due": None}).status_code == 422


def test_series_ends_at_until(hub) -> None:
    t = _task(hub, title="Course", due="2030-03-12", recurrence={"freq": "daily", "until": "2030-03-12"})
    out = hub.patch(f"/tasks/{t['id']}", json={"done": True}).json()
    assert "next" not in out and len(_all_tasks(hub)) == 1


def test_signed_done_link_and_mcp_roll_over(make_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub_api import mcp_server
    from hub_api.routers.notify import done_sig
    from tests.hub_api.conftest import TOKEN

    monkeypatch.setattr(mcp_server, "local_now", lambda: NOW)
    hub = make_hub(mcp_plugins())
    t = _task(hub, title="Standup", due="2030-03-12", due_time="09:00", recurrence="every weekday")
    for _ in range(2):  # the button tapped twice
        assert hub.client.post(f"/notify/done/{t['id']}?sig={done_sig(TOKEN, t['id'])}").status_code == 200
    tasks = _all_tasks(hub)
    assert len(tasks) == 2 and {x["due"] for x in tasks} == {"2030-03-12", "2030-03-13"}

    mcp = MCP(hub.client)
    added = mcp.ok("tasks_add", title="Call grandma", repeat="every sunday", due_time="11:00")
    assert added["due"] == "2030-03-17" and added["recurrence"] == {"freq": "weekly", "days": [6]}
    done = mcp.ok("tasks_complete", id=added["id"])
    assert done["next"]["due"] == "2030-03-24"
    bad = mcp.call("tasks_add", title="x", repeat="whenever")
    assert bad["isError"] is True and "recurrence" in bad["content"][0]["text"]


# ------------------------------------------------------------------ checklists


def test_checklist_patch_and_item_routes(hub) -> None:
    t = _task(hub, title="Trip", checklist=["passport", {"text": "  tickets  "}, {"text": " "}])
    items = t["checklist"]
    assert [i["text"] for i in items] == ["passport", "tickets"] and all(not i["done"] for i in items)
    a, b = items
    # the whole list: reorder + toggle + add (ids kept, a new one for the new item)
    r = hub.patch(f"/tasks/{t['id']}", json={"checklist": [{**b, "done": True}, a, {"text": "adapter"}]})
    out = r.json()["checklist"]
    assert [(i["text"], i["done"]) for i in out] == [("tickets", True), ("passport", False), ("adapter", False)]
    assert out[0]["id"] == b["id"] and out[1]["id"] == a["id"] and out[2]["id"] not in (a["id"], b["id"])
    # one item at a time
    out = hub.post(f"/tasks/{t['id']}/checklist", json={"text": "sunscreen"}).json()["checklist"]
    assert out[-1]["text"] == "sunscreen"
    out = hub.patch(f"/tasks/{t['id']}/checklist/{a['id']}", json={"done": True, "text": "passport (valid)"}).json()
    assert out["checklist"][1] == {"id": a["id"], "text": "passport (valid)", "done": True}
    assert hub.delete(f"/tasks/{t['id']}/checklist/{a['id']}").json()["checklist"][1]["text"] == "adapter"
    assert hub.delete(f"/tasks/{t['id']}/checklist/{a['id']}").status_code == 404
    assert hub.patch(f"/tasks/{t['id']}/checklist/nope", json={"done": True}).status_code == 404
    assert hub.post(f"/tasks/{t['id']}/checklist", json={"text": ""}).status_code == 422
    # emptied: stored as NULL, read back as []
    assert hub.patch(f"/tasks/{t['id']}", json={"checklist": []}).json()["checklist"] == []
    assert fa.sql("SELECT checklist FROM app_tasks WHERE id = ?", [t["id"]]) == [(None,)]


def test_checklist_normalization() -> None:
    items = taskplus.normalize_checklist([{"id": "x1", "text": "a"}, {"id": "x1", "text": "b"}, "c", 5, {"text": ""}])
    assert [i["text"] for i in items] == ["a", "b", "c"] and items[0]["id"] == "x1" and items[1]["id"] != "x1"
    assert len(taskplus.normalize_checklist(["x"] * 150)) == taskplus.CHECKLIST_MAX
    assert taskplus.checklist_of("not json") == [] and taskplus.checklist_of(None) == []
    with pytest.raises(ValueError):
        taskplus.normalize_checklist("nope")


def test_checklist_in_the_calendar_event(ghub, google) -> None:
    t = _task(ghub, title="Pack", due="2030-03-12", details="for Agadir", checklist=["socks"])
    desc = google.events[CAL][gcal.event_id(t["id"])]["description"]
    assert desc.startswith("for Agadir\n\n☐ socks\n\n")
    item = t["checklist"][0]["id"]
    ghub.patch(f"/tasks/{t['id']}/checklist/{item}", json={"done": True})
    assert "☑ socks" in google.events[CAL][gcal.event_id(t["id"])]["description"]
    # recurring: one event per occurrence, never an RRULE
    r = _task(ghub, title="Bins", due="2030-03-12", recurrence="every tuesday")
    nxt = ghub.patch(f"/tasks/{r['id']}", json={"done": True}).json()["next"]
    ev = google.events[CAL][gcal.event_id(nxt["id"])]
    assert ev["start"] == {"date": "2030-03-19"} and "recurrence" not in ev


# ------------------------------------------------------------------ free slots (pure)


def test_free_slots_around_busy_time() -> None:
    busy = [(_at(DAY, 9), _at(DAY, 10, 20)), (_at(DAY, 12), _at(DAY, 13)), (_at(DAY, 12, 30), _at(DAY, 14))]
    slots = taskplus.free_slots(busy, now=NOW, minutes=30, days=2)
    starts = [s.strftime("%m-%d %H:%M") for s, _ in slots]
    # 10:20 rounds up to 10:30; then hourly in the gap; at most 3 a day
    assert starts == ["03-12 10:30", "03-12 11:30", "03-12 14:00", "03-13 09:00", "03-13 10:00", "03-13 11:00"]
    assert all(e - s == timedelta(minutes=30) for s, e in slots)
    # a long block does not fit the 11:30-12:00 tail; "now" in the evening skips to tomorrow; 20:00 is the end
    long = taskplus.free_slots(busy, now=NOW, minutes=90, days=1)
    assert [s.strftime("%H:%M") for s, _ in long] == ["10:30", "14:00", "15:30"]
    late = taskplus.free_slots([], now=_at(DAY, 19, 40), minutes=30, days=2, limit=1)
    assert late[0][0] == _at(DAY + timedelta(days=1), 9)
    assert taskplus.free_slots([(_at(DAY, 9), _at(DAY, 20))], now=NOW, minutes=30, days=1) == []


# ------------------------------------------------------------------ time blocking


def test_slots_and_block_lifecycle(ghub, google) -> None:
    google.add_owner_event("Standup", _at(DAY, 9).isoformat(), _at(DAY, 10).isoformat())
    google.add_owner_event("Holiday", "2030-03-12", "2030-03-13")  # all day: does not block
    # an agent event in the OmniFree calendar (not a read calendar) still counts
    google.events[CAL]["a1"] = {"id": "a1", "summary": "Lunch", "status": "confirmed",
                                "start": {"dateTime": _at(DAY, 10).isoformat()},
                                "end": {"dateTime": _at(DAY, 11).isoformat()},
                                "extendedProperties": {"private": {"omnifreeKind": "agent"}}}
    _task(ghub, title="Call", due="2030-03-12", due_time="11:00")  # a timed task holds 30 minutes
    t = _task(ghub, title="Write report", estimate=60, checklist=["intro"])

    r = ghub.get(f"/tasks/{t['id']}/slots", params={"days": 2})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["writable"] is True and out["minutes"] == 60 and out["work_hours"] == ["09:00", "20:00"]
    assert [s["start"] for s in out["slots"]][:3] == ["2030-03-12T11:30", "2030-03-12T12:30", "2030-03-12T13:30"]

    # auto-pick: the first free slot
    r = ghub.post(f"/tasks/{t['id']}/block", json={})
    assert r.status_code == 200, r.text
    block = r.json()["task"]["block"]
    assert block["start"] == "2030-03-12T11:30" and block["end"] == "2030-03-12T12:30"
    ev = google.events[CAL][block["event_id"]]
    assert ev["summary"] == "Write report" and ev["transparency"] == "opaque"
    assert ev["start"] == {"dateTime": "2030-03-12T11:30:00", "timeZone": "Africa/Casablanca"}
    assert ev["extendedProperties"]["private"] == {"omnifreeKind": "block", "omnifreeBlockTaskId": t["id"]}
    assert "☐ intro" in ev["description"]
    # the next suggestions skip the block, except for the task itself (moving it)
    other = _task(ghub, title="Other")
    assert ghub.get(f"/tasks/{other['id']}/slots").json()["slots"][0]["start"] == "2030-03-12T12:30"
    assert ghub.get(f"/tasks/{t['id']}/slots").json()["slots"][0]["start"] == "2030-03-12T11:30"

    # moving: a chosen start and length; the old event goes
    r = ghub.post(f"/tasks/{t['id']}/block", json={"start": "2030-03-13T15:00", "minutes": 45})
    moved = r.json()["task"]["block"]
    assert moved["start"] == "2030-03-13T15:00" and moved["end"] == "2030-03-13T15:45"
    assert google.events[CAL][block["event_id"]]["status"] == "cancelled"
    assert r.json()["task"]["estimate"] == 60  # an existing estimate is kept
    assert ghub.post(f"/tasks/{t['id']}/block", json={"start": "tomorrow"}).status_code == 422

    # unblock deletes the event; deleting a blocked task deletes its block too
    out = ghub.delete(f"/tasks/{t['id']}/block").json()
    assert out["block"] is None and google.events[CAL][moved["event_id"]]["status"] == "cancelled"
    assert ghub.delete(f"/tasks/{t['id']}/block").status_code == 200  # nothing to do
    blocked = ghub.post(f"/tasks/{other['id']}/block", json={"minutes": 30}).json()["task"]
    assert blocked["estimate"] == 30
    assert ghub.delete(f"/tasks/{other['id']}").status_code == 204
    assert google.events[CAL][blocked["block"]["event_id"]]["status"] == "cancelled"
    assert ghub.get("/tasks/nope/slots").status_code == 404


def test_block_without_calendar_degrades(hub) -> None:
    t = _task(hub, title="Focus")
    r = hub.get(f"/tasks/{t['id']}/slots", params={"days": 1})
    assert r.status_code == 200
    out = r.json()
    assert out["writable"] is False and out["slots"][0]["start"] == "2030-03-12T09:00" and out["errors"] == []
    r = hub.post(f"/tasks/{t['id']}/block", json={})
    assert r.status_code == 409 and "not set up" in r.json()["detail"]


def test_block_when_the_day_is_full(ghub, google) -> None:
    for i in range(8):
        d = DAY + timedelta(days=i)
        google.add_owner_event(f"Busy {i}", _at(d, 8).isoformat(), _at(d, 21).isoformat())
    t = _task(ghub, title="Nowhere")
    r = ghub.post(f"/tasks/{t['id']}/block", json={})
    assert r.status_code == 409 and "no free slot" in r.json()["detail"]


def test_block_google_failure_is_reported(ghub, google) -> None:
    t = _task(ghub, title="Flaky")
    google.fail_status = 500
    r = ghub.post(f"/tasks/{t['id']}/block", json={"start": "2030-03-12T10:00"})
    assert r.status_code == 502
    assert _all_tasks(ghub)[0]["block"] is None


# ------------------------------------------------------------------ voice notes: appointments and repeats


def _voice(hub, llm: dict[str, Any], mode: str = "auto"):
    fa.TRANSCRIPTS["groq"] = "rdv dentiste jeudi 10h au centre, et chaque lundi sortir les poubelles"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"title": "Memo", "body": "memo", "tags": [], **llm}))
    return hub.post("/notes/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")}, data={"mode": mode})


VOICE_LLM = {
    "tasks": [
        {"title": "Sortir les poubelles", "when": "chaque lundi", "due": None, "repeat": "chaque lundi"},
        {"title": "Arroser", "when": "tous les jours à 8h", "due": "2030-03-12", "due_time": "08:00"},
        {"title": "Acheter du pain", "when": "demain", "due": "2030-03-13"},
    ],
    "events": [
        {"title": "Dentiste", "when": "jeudi 10h", "date": "2030-03-14", "time": "10:00", "location": "Centre Anfa"},
        {"title": "Réunion avec Karim", "when": "demain à 15h", "duration_minutes": 30},
        {"title": "Dîner chez Sara", "when": "samedi", "date": "2030-03-16"},  # no time: a task
    ],
}


def test_voice_appointments_become_events(ghub, google) -> None:
    r = _voice(ghub, VOICE_LLM)
    assert r.status_code == 201, r.text
    out = r.json()
    events = {e["title"]: e for e in out["events"]}
    assert set(events) == {"Dentiste", "Réunion avec Karim"}
    dentist = google.events[CAL][events["Dentiste"]["id"]]
    assert dentist["start"] == {"dateTime": "2030-03-14T10:00:00", "timeZone": "Africa/Casablanca"}
    assert dentist["end"] == {"dateTime": "2030-03-14T11:00:00", "timeZone": "Africa/Casablanca"}  # 60 min default
    assert dentist["location"] == "Centre Anfa"
    assert dentist["extendedProperties"]["private"] == {"omnifreeKind": "voice"}
    karim = google.events[CAL][events["Réunion avec Karim"]["id"]]
    assert karim["start"]["dateTime"] == "2030-03-13T15:00:00" and karim["end"]["dateTime"] == "2030-03-13T15:30:00"
    assert events["Dentiste"]["location"] == "Centre Anfa" and events["Dentiste"]["link"]

    tasks = {t["title"]: t for t in out["tasks"]}
    assert set(tasks) == {"Sortir les poubelles", "Arroser", "Acheter du pain", "Dîner chez Sara"}
    bins = tasks["Sortir les poubelles"]
    assert bins["recurrence"] == {"freq": "weekly", "days": [0]} and bins["due"] == "2030-03-18"
    water = tasks["Arroser"]
    # it is 08:00 already: tomorrow at 8
    assert water["recurrence"] == {"freq": "daily"} and (water["due"], water["due_time"]) == ("2030-03-13", "08:00")
    assert tasks["Acheter du pain"]["recurrence"] is None
    assert tasks["Dîner chez Sara"]["due"] == "2030-03-16" and tasks["Dîner chez Sara"]["due_time"] is None

    # the prompt explains the rule
    system = fa.CHAT_LOG[-1][1].messages[0].content
    assert '"events"' in system and "NOT in tasks" in system and '"repeat"' in system

    # undo from the voice sheet: DELETE /gcal/events/{id}
    eid = events["Dentiste"]["id"]
    assert ghub.delete(f"/gcal/events/{eid}").status_code == 204
    assert google.events[CAL][eid]["status"] == "cancelled"
    assert ghub.delete(f"/gcal/events/{eid}").status_code == 404
    # never an event OmniFree did not make
    google.events[CAL]["mine"] = {"id": "mine", "summary": "Owner's", "status": "confirmed",
                                  "start": {"date": "2030-03-20"}, "end": {"date": "2030-03-21"}}
    assert ghub.delete("/gcal/events/mine").status_code == 403
    assert google.events[CAL]["mine"]["status"] == "confirmed"


def test_voice_appointments_fall_back_to_timed_tasks(hub) -> None:
    out = _voice(hub, {"tasks": [], "events": VOICE_LLM["events"][:1]}).json()
    assert out["events"] == []
    (task,) = out["tasks"]
    assert (task["title"], task["due"], task["due_time"]) == ("Dentiste", "2030-03-14", "10:00")
    assert task["details"] == "At Centre Anfa"
    assert hub.delete("/gcal/events/x").status_code == 409  # not set up


def test_voice_calendar_failure_falls_back(ghub, google) -> None:
    google.fail_status = 500
    out = _voice(ghub, {"tasks": [], "events": VOICE_LLM["events"][:1]}).json()
    assert out["events"] == [] and [t["title"] for t in out["tasks"]] == ["Dentiste"]


def test_voice_note_mode_skips_events(ghub, google) -> None:
    out = _voice(ghub, VOICE_LLM, mode="note").json()
    assert out["events"] == [] and out["tasks"] == [] and out["note"] is not None
    # tasks mode with only an appointment: no fallback note
    out = _voice(ghub, {"tasks": [], "events": VOICE_LLM["events"][:1]}, mode="tasks").json()
    assert out["note"] is None and len(out["events"]) == 1
