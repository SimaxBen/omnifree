"""/plan/today: the AI day plan (pure planner helpers, LLM validation + greedy fallback, Google Calendar booking and
"Clear plan", the morning push line). Google is the FakeGoogle of test_gcal; the LLM is scripted."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx
import pytest

from hub_api import gcal, notifier
from hub_api.appdata import local_tz
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers import plan as plan_mod
from hub_api.routers.plan import estimate_minutes, free_gaps, greedy_plan, to_minutes, validate_llm_plan
from tests.hub_api import fakes_apps as fa
from tests.hub_api.test_gcal import CAL, SA_INFO, FakeGoogle

TZ = local_tz()
DAY = date(2030, 3, 12)


def _at(h: int, m: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(plan_mod, "local_now", lambda: _at(7, 10))
    monkeypatch.setattr(notes_mod, "local_today", lambda: DAY)
    monkeypatch.setattr(notify_mod, "local_today", lambda: DAY)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(gcal, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv(gcal.SA_KEY, json.dumps(SA_INFO))
    monkeypatch.setenv(gcal.ID_KEY, CAL)
    monkeypatch.setenv(gcal.READ_KEY, f"owner@gmail.com,{CAL}")
    return fake


def _task(hub, title: str, due: str | None = None, at: str | None = None, **extra: Any) -> dict[str, Any]:
    body = {"title": title, **({"due": due} if due else {}), **({"due_time": at} if at else {}), **extra}
    r = hub.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _tasks_of(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in plan["blocks"] if b["kind"] == "task"]


def _no_overlap(blocks: list[dict[str, Any]]) -> None:
    spans = sorted((to_minutes(b["start"]), to_minutes(b["end"])) for b in blocks if b["kind"] != "event")
    for (_, e1), (s2, _) in zip(spans, spans[1:], strict=False):
        assert e1 <= s2, spans


# ------------------------------------------------------------------ pure helpers


def test_estimate_minutes_reads_written_estimates() -> None:
    assert estimate_minutes({"title": "Write report (45m)"}) == 45
    assert estimate_minutes({"title": "Deep work ~1h30"}) == 90
    assert estimate_minutes({"title": "Taxes", "details": "est: 2h"}) == 120
    assert estimate_minutes({"title": "Call [20 min]"}) == 20
    assert estimate_minutes({"title": "Read", "details": "durée 1.5h"}) == 90
    assert estimate_minutes({"title": "Huge (9h)"}) == 240  # clamped
    assert estimate_minutes({"title": "Buy 2 kg of rice"}) == 30  # numbers without an estimate marker
    assert estimate_minutes({"title": "Room 101 at 3 min walk"}) == 30
    assert estimate_minutes({"title": "Plain"}) == 30


def test_free_gaps_and_time_parsing() -> None:
    assert free_gaps(480, 1260, [(540, 600), (590, 660), (1200, 1300)]) == [(480, 540), (660, 1200)]
    assert free_gaps(480, 600, []) == [(480, 600)]
    assert free_gaps(480, 600, [(400, 700)]) == []
    assert to_minutes("09:30") == 570 and to_minutes("9h05") == 545 and to_minutes("25:00") is None
    assert to_minutes(None) is None and to_minutes("soon") is None


def test_greedy_plan_orders_breaks_and_lunch() -> None:
    tasks = [{"id": f"t{i}", "title": f"T{i}", "minutes": 60, "priority": "normal", "due": None} for i in range(4)]
    tasks.append({"id": "big", "title": "Too big", "minutes": 240, "priority": "normal", "due": None})
    blocks, unscheduled = greedy_plan((480, 900), [(600, 660)], tasks, DAY)
    kinds = [(b["kind"], b["start"], b["end"], b["ref_id"]) for b in blocks]
    assert ("break", "13:00", "13:45", None) in kinds  # lunch
    t = [k for k in kinds if k[0] == "task"]
    assert t[0] == ("task", "08:00", "09:00", "t0") and t[1] == ("task", "09:00", "10:00", "t1")
    assert ("task", "11:00", "12:00", "t2") in t and ("task", "12:00", "13:00", "t3") in t
    assert any(k[0] == "break" and k[1] == "13:45" for k in kinds) is False  # no work before it: no extra break
    assert [u["id"] for u in unscheduled] == ["big"]
    long_day = [{"id": f"x{i}", "title": "X", "minutes": 45, "priority": "normal", "due": None} for i in range(3)]
    blocks, _ = greedy_plan((480, 720), [], long_day, DAY, lunch=False)
    assert [(b["kind"], b["start"]) for b in blocks] == [("task", "08:00"), ("task", "08:45"), ("break", "09:30"),
                                                         ("task", "09:45")]


def test_validate_llm_plan_drops_bad_blocks() -> None:
    tasks = [{"id": "a", "title": "A", "minutes": 30}, {"id": "b", "title": "B", "minutes": 30}]
    busy = [(600, 660)]
    good = validate_llm_plan({"blocks": [
        {"kind": "task", "task_id": "a", "start": "08:00", "end": "08:30", "reason": "fresh"},
        {"kind": "task", "task_id": "b", "start": "10:15", "end": "10:45"},  # overlaps the meeting
        {"kind": "task", "task_id": "a", "start": "11:00", "end": "11:30"},  # twice
        {"kind": "break", "start": "08:30", "end": "08:40"},
        {"kind": "event", "start": "12:00", "end": "13:00"},  # ignored
    ]}, (480, 1260), busy, tasks, DAY)
    assert good is not None
    assert [(b["kind"], b["ref_id"], b["start"]) for b in good] == [("task", "a", "08:00"), ("break", None, "08:30")]
    assert good[0]["reason"] == "fresh" and good[0]["title"] == "A"
    assert validate_llm_plan(None, (480, 1260), busy, tasks, DAY) is None
    assert validate_llm_plan({"blocks": "x"}, (480, 1260), busy, tasks, DAY) is None
    bad = {"blocks": [{"task_id": "zz", "start": "08:00", "end": "08:30"},
                      {"task_id": "a", "start": "07:00", "end": "07:30"},
                      {"task_id": "b", "start": "08:00", "end": "09:00"}]}
    assert validate_llm_plan(bad, (480, 1260), busy, tasks, DAY) is None  # under half usable
    assert validate_llm_plan({"blocks": []}, (480, 1260), busy, tasks, DAY) is None  # room left, nothing placed


# ------------------------------------------------------------------ routes


def test_plan_greedy_fallback_cached_and_morning_line(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setenv("NTFY_TOPIC", "plan-topic")
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(
        lambda r: sent.append(json.loads(r.content)) or httpx.Response(200, json={"id": "m1"})))
    assert app_hub.get("/plan/today").json()["status"] == "none"
    _task(app_hub, "Old thing (1h)", "2030-03-01")
    _task(app_hub, "Pay rent", DAY.isoformat(), priority="high")
    _task(app_hub, "Dentist", DAY.isoformat(), "15:00")
    _task(app_hub, "Urgent idea", None, priority="high")
    _task(app_hub, "Someday", "2030-06-01")
    fa.CHAT_SCRIPT["groq"].append("not json at all")

    r = app_hub.post("/plan/today")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["source"] == "greedy" and plan["status"] == "proposed" and plan["day"] == DAY.isoformat()
    assert plan["window"] == {"start": "08:00", "end": "21:00"}
    titles = [b["title"] for b in _tasks_of(plan)]
    assert titles[:3] == ["Old thing (1h)", "Pay rent", "Urgent idea"]  # overdue, due today (high), high priority
    assert "Someday" not in titles
    dentist = next(b for b in plan["blocks"] if b["title"] == "Dentist")
    assert dentist["start"] == "15:00" and dentist["fixed"] is True and dentist["reason"] == "due at 15:00"
    assert _tasks_of(plan)[0]["end"] == "09:00" and "overdue" in _tasks_of(plan)[0]["reason"]
    assert any(b["kind"] == "break" and b["title"] == "Lunch" for b in plan["blocks"])
    _no_overlap(plan["blocks"])
    assert plan["calendar"] == {"configured": False, "writable": False}
    assert app_hub.get("/plan/today").json()["blocks"] == plan["blocks"]
    assert len(fa.CHAT_LOG) == 1  # one LLM call per plan

    out = app_hub.post("/notify/run?kind=morning&preview=1").json()
    assert "Plan : 08:00 Old thing (1h) · 09:00 Pay rent · 09:45 Urgent idea (+1)"  # break after 90 min in out["message"].splitlines()

    assert app_hub.delete("/plan/today").json()["cleared"] is True
    assert app_hub.get("/plan/today").json()["status"] == "none"
    out = app_hub.post("/notify/run?kind=morning&preview=1").json()
    assert "Plan :" not in out["message"]


def test_plan_uses_a_valid_llm_answer(app_hub) -> None:
    a = _task(app_hub, "Write report", DAY.isoformat())
    b = _task(app_hub, "Call bank", DAY.isoformat())
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"blocks": [
        {"kind": "task", "task_id": b["id"], "start": "08:30", "end": "09:00", "reason": "bank opens at 8:30"},
        {"kind": "task", "task_id": a["id"], "start": "09:00", "end": "10:30", "reason": "focus time"},
        {"kind": "break", "start": "10:30", "end": "10:45"},
    ]}))
    plan = app_hub.post("/plan/today").json()
    assert plan["source"] == "llm"
    assert [(x["kind"], x["ref_id"], x["start"], x["end"]) for x in plan["blocks"]] == [
        ("task", b["id"], "08:30", "09:00"), ("task", a["id"], "09:00", "10:30"), ("break", None, "10:30", "10:45")]
    assert plan["blocks"][0]["reason"] == "bank opens at 8:30" and plan["unscheduled"] == []
    prompt = fa.CHAT_LOG[0][1].messages[0].content
    assert "08:00-21:00" in prompt and a["id"] in prompt
    # no LLM on request
    plan = app_hub.post("/plan/today", json={"use_llm": False}).json()
    assert plan["source"] == "greedy" and len(fa.CHAT_LOG) == 1
    plan = app_hub.post("/plan/today", headers={"X-OmniFree-Llm": "0"}).json()
    assert plan["source"] == "greedy" and len(fa.CHAT_LOG) == 1


def test_plan_late_in_the_day_and_from_now(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    _task(app_hub, "Thing", DAY.isoformat())
    monkeypatch.setattr(plan_mod, "local_now", lambda: _at(14, 7))
    plan = app_hub.post("/plan/today", json={"use_llm": False}).json()
    assert plan["window"]["start"] == "14:15" and _tasks_of(plan)[0]["start"] == "14:15"
    assert not any(b["title"] == "Lunch" for b in plan["blocks"])
    monkeypatch.setattr(plan_mod, "local_now", lambda: _at(22, 0))
    plan = app_hub.post("/plan/today").json()
    assert _tasks_of(plan) == [] and plan["unscheduled"][0]["reason"] == "the day is over"
    assert fa.CHAT_LOG == []


def test_accept_needs_a_plan_and_a_calendar(app_hub) -> None:
    assert app_hub.post("/plan/today/accept", json={"book": True}).status_code == 404
    _task(app_hub, "Thing", DAY.isoformat())
    app_hub.post("/plan/today", json={"use_llm": False})
    assert app_hub.post("/plan/today/accept", json={"book": True}).status_code == 409
    out = app_hub.post("/plan/today/accept").json()
    assert out["status"] == "accepted" and out["booked"] == 0 and out["errors"] == []


def test_plan_with_google_agenda_booking_and_clear(make_hub, google: FakeGoogle) -> None:
    hub = make_hub(fa.apps_plugins())
    google.add_owner_event("Standup", "2030-03-12T09:00:00+01:00", "2030-03-12T10:00:00+01:00")
    google.add_owner_event("Holiday", "2030-03-12", "2030-03-13")  # all day: not a block
    t1 = _task(hub, "Report (1h)", DAY.isoformat())
    _task(hub, "Timed", DAY.isoformat(), "17:00")
    plan = hub.post("/plan/today", json={"use_llm": False}).json()
    standup = next(b for b in plan["blocks"] if b["kind"] == "event")
    local = datetime.fromisoformat("2030-03-12T09:00:00+01:00").astimezone(TZ).strftime("%H:%M")
    assert standup["title"] == "Standup" and standup["start"] == local and standup["fixed"] is True
    assert not any(b["title"] == "Holiday" for b in plan["blocks"])
    _no_overlap(plan["blocks"])
    assert plan["calendar"] == {"configured": True, "writable": True}

    out = hub.post("/plan/today/accept", json={"book": True}).json()
    assert out["status"] == "accepted" and out["booked"] == 1 and out["errors"] == []
    booked = [e for e in google.events[CAL].values() if
              ((e.get("extendedProperties") or {}).get("private") or {}).get("omnifreeKind") == "plan"]
    assert len(booked) == 1
    props = booked[0]["extendedProperties"]["private"]
    assert props["omnifreePlanDay"] == DAY.isoformat() and props["omnifreePlanTask"] == t1["id"]
    assert booked[0]["summary"] == "▸ Report (1h)" and booked[0]["start"]["timeZone"] == "Africa/Casablanca"

    # re-planning removes the booked blocks and ignores them as busy time
    again = hub.post("/plan/today", json={"use_llm": False}).json()
    assert again["cleared_previous"] is True and again["booked"] == 0
    assert booked[0]["status"] == "cancelled"
    assert not any(b["title"].startswith("▸") for b in again["blocks"])

    hub.post("/plan/today/accept", json={"book": True})
    live = [e for e in google.events[CAL].values() if e.get("status") != "cancelled"
            and (e.get("extendedProperties") or {}).get("private", {}).get("omnifreeKind") == "plan"]
    assert len(live) == 1
    cleared = hub.delete("/plan/today").json()
    assert cleared["deleted_events"] == 1 and cleared["error"] is None
    assert live[0]["status"] == "cancelled"
