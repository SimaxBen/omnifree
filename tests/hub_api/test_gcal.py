"""Google Calendar through a service account: token exchange (JWT), task -> event pushes, the reconcile, the agenda
feed, the morning/reminder pushes, the digest count and the MCP tools. Google is an httpx.MockTransport fake."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hub_api import gcal, notifier
from hub_api.appdata import local_tz
from hub_api.routers import gcal as gcal_router
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from tests.hub_api import fakes_apps as fa
from tests.hub_api.test_mcp import MCP, mcp_plugins

TZ = local_tz()
CAL = "omnifree123@group.calendar.google.com"
PRIMARY = "owner@gmail.com"
SA_EMAIL = "omnifree-calendar@proj.iam.gserviceaccount.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DAY = date(2030, 3, 12)  # a Tuesday, far enough ahead to stay "open" for the reconcile window

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
SA_INFO = {"type": "service_account", "client_email": SA_EMAIL, "private_key": _PEM, "private_key_id": "kid1",
           "token_uri": TOKEN_URI, "project_id": "proj"}


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _start_of(item: dict[str, Any]) -> datetime:
    s = item["start"]
    if "dateTime" in s:
        t = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=TZ)
    return datetime.combine(date.fromisoformat(s["date"]), datetime.min.time(), TZ)


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (method, path, json or params)
        self.events: dict[str, dict[str, dict[str, Any]]] = {CAL: {}, PRIMARY: {}}
        self.names = {CAL: "OmniFree", PRIMARY: "Owner"}
        self.listed: dict[str, str] = {CAL: "owner"}  # the service account's calendarList
        self.shared: dict[str, str] = {PRIMARY: "reader"}  # shared with it, not yet in its list
        self.token_forms: list[dict[str, list[str]]] = []
        self.delay = 0.0
        self.fail_status: int | None = None

    def api_calls(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if "calendarList" not in p]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.delay:
            await asyncio.sleep(self.delay)
        if str(request.url) == TOKEN_URI:
            self.token_forms.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3599, "token_type": "Bearer"})
        assert request.headers["authorization"] == "Bearer tok-1"
        assert request.url.host == "www.googleapis.com"
        path = request.url.path.removeprefix("/calendar/v3")
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        self.calls.append((request.method, path, body or params))
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "backend error"}})
        m = re.fullmatch(r"/users/me/calendarList(?:/(.+))?", path)
        if m:
            cal = m.group(1) or body.get("id")
            if request.method == "GET":
                role = self.listed.get(cal)
            else:
                role = self.shared.get(cal) or self.listed.get(cal)
                if role:
                    self.listed[cal] = role
            if not role:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            return httpx.Response(200, json={"id": cal, "accessRole": role, "summary": self.names.get(cal)})
        m = re.fullmatch(r"/calendars/([^/]+)/events(?:/([^/]+))?", path)
        assert m, path
        cal, eid = m.group(1), m.group(2)
        store = self.events.setdefault(cal, {})
        if request.method == "POST":
            if body.get("id") and body["id"] in store and store[body["id"]].get("status") != "cancelled":
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            ev = {**body, "id": body.get("id") or f"g{len(store) + 1}", "htmlLink": "https://calendar.google.com/e"}
            store[ev["id"]] = ev
            return httpx.Response(200, json=ev)
        if request.method == "PUT":
            if eid not in store:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            store[eid] = {**body, "id": eid}
            return httpx.Response(200, json=store[eid])
        if request.method == "DELETE":
            if eid not in store or store[eid].get("status") == "cancelled":
                return httpx.Response(410, json={"error": {"message": "Gone"}})
            store[eid]["status"] = "cancelled"
            return httpx.Response(204)
        # GET: events.list
        items = [e for e in store.values() if e.get("status") != "cancelled"]
        prop = params.get("privateExtendedProperty")
        if prop:
            k, v = prop.split("=", 1)
            items = [e for e in items if ((e.get("extendedProperties") or {}).get("private") or {}).get(k) == v]
        else:
            lo = datetime.fromisoformat(params["timeMin"].replace("Z", "+00:00"))
            hi = datetime.fromisoformat(params["timeMax"].replace("Z", "+00:00"))
            items = sorted((e for e in items if lo <= _start_of(e) < hi), key=_start_of)
        return httpx.Response(200, json={"summary": self.names.get(cal, cal), "items": items})

    def add_owner_event(self, title: str, start: str, end: str | None = None, **extra: Any) -> dict[str, Any]:
        n = len(self.events[PRIMARY]) + 1
        if "T" in start:
            ev = {"start": {"dateTime": start}, "end": {"dateTime": end or start}}
        else:
            ev = {"start": {"date": start}, "end": {"date": end or start}}
        ev.update({"id": f"own{n}", "summary": title, "status": "confirmed",
                   "htmlLink": f"https://calendar.google.com/event?eid=own{n}", **extra})
        self.events[PRIMARY][ev["id"]] = ev
        return ev


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fa.reset()
    fake = FakeGoogle()
    monkeypatch.setattr(gcal, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv(gcal.SA_KEY, json.dumps(SA_INFO))
    monkeypatch.setenv(gcal.ID_KEY, CAL)
    monkeypatch.setenv(gcal.READ_KEY, f"{PRIMARY},{CAL}")
    return fake


@pytest.fixture
def app_hub(make_hub, google):
    return make_hub(fa.apps_plugins())


def _task(hub, title: str, due: str | None = None, at: str | None = None, **extra: Any) -> dict[str, Any]:
    body = {"title": title, **({"due": due} if due else {}), **({"due_time": at} if at else {}), **extra}
    r = hub.post("/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _sync_rows() -> dict[str, tuple[str, str | None]]:
    return {r[0]: (r[1], r[2]) for r in fa.sql("SELECT task_id, status, error FROM app_gcal_sync")}


# ------------------------------------------------------------------ ids, token


def test_event_id_is_deterministic_and_valid() -> None:
    ids = ["a", "Ab3xYz09QwEr", "x" * 64, "é-ü_9", "AAAAAAAAAAAA", "aaaaaaaaaaaa"]
    seen = set()
    for task_id in ids:
        eid = gcal.event_id(task_id)
        assert eid == gcal.event_id(task_id)
        assert re.fullmatch(r"[a-v0-9]{5,1024}", eid), eid
        assert base64.b32hexdecode(eid[8:].upper() + "=" * (-len(eid[8:]) % 8)).decode() == task_id
        seen.add(eid)
    assert len(seen) == len(ids)  # case-sensitive ids stay distinct


def test_token_exchange_jwt_and_cache(google) -> None:
    cfg = gcal.load_config({gcal.SA_KEY: base64.b64encode(json.dumps(SA_INFO).encode()).decode(),
                            gcal.ID_KEY: CAL}.get)  # base64 JSON accepted too
    assert cfg.writable and cfg.sa_email == SA_EMAIL and cfg.calendars == (CAL,)
    before = int(time.time())
    assert asyncio.run(gcal.access_token(cfg)) == "tok-1"
    assert asyncio.run(gcal.access_token(cfg)) == "tok-1"
    assert len(google.token_forms) == 1  # cached until expiry
    form = google.token_forms[0]
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header_b64, claims_b64, sig_b64 = form["assertion"][0].split(".")
    header = json.loads(_b64d(header_b64))
    claims = json.loads(_b64d(claims_b64))
    assert header["alg"] == "RS256" and header["typ"] == "JWT" and header["kid"] == "kid1"
    assert claims["iss"] == SA_EMAIL and claims["aud"] == TOKEN_URI
    assert claims["scope"] == "https://www.googleapis.com/auth/calendar"
    assert before <= claims["iat"] <= before + 5 and claims["exp"] == claims["iat"] + 3600
    _KEY.public_key().verify(_b64d(sig_b64), f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(),
                             hashes.SHA256())


def test_not_configured_degrades_silently(make_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    monkeypatch.setattr(gcal, "transport", httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    hub = make_hub(fa.apps_plugins())
    t = _task(hub, "Plain", DAY.isoformat(), "10:00")
    assert hub.patch(f"/tasks/{t['id']}", json={"done": True}).status_code == 200
    assert hub.delete(f"/tasks/{t['id']}").status_code == 204
    st = hub.get("/gcal/status").json()
    assert st["configured"] is False and st["sa_email"] is None and "GOOGLE_CALENDAR_SA_JSON" in st["error"]
    assert hub.get("/gcal/events").json() == {"configured": False, "events": [], "errors": [],
                                              "from": hub.get("/gcal/events").json()["from"],
                                              "to": hub.get("/gcal/events").json()["to"]}
    assert hub.post("/gcal/sync").json()["configured"] is False
    assert hub.post("/gcal/events", json={"title": "x", "start": "2030-01-01"}).status_code == 409
    assert calls == []


# ------------------------------------------------------------------ task lifecycle


def test_task_lifecycle_pushes_events(app_hub, google) -> None:
    t = _task(app_hub, "Dentist", DAY.isoformat(), "09:30", details="bring the card", priority="high")
    eid = gcal.event_id(t["id"])
    ev = google.events[CAL][eid]
    assert ("POST", f"/calendars/{CAL}/events") in google.api_calls()
    assert ev["summary"] == "Dentist"
    assert ev["start"] == {"dateTime": "2030-03-12T09:30:00", "timeZone": "Africa/Casablanca"}
    assert ev["end"] == {"dateTime": "2030-03-12T10:00:00", "timeZone": "Africa/Casablanca"}
    assert ev["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]}
    assert ev["description"] == "bring the card\n\nhttps://omnifree.ilyasouhnine.tech/#/notes/tasks"
    assert ev["extendedProperties"]["private"] == {"omnifreeTaskId": t["id"], "omnifreeKind": "task"}
    assert ev["colorId"] == "11"
    assert _sync_rows()[t["id"]] == ("synced", None)

    # date only -> all-day (end = next day), priority normal clears the color
    app_hub.patch(f"/tasks/{t['id']}", json={"due_time": None, "priority": "normal", "title": "Dentiste"})
    ev = google.events[CAL][eid]
    assert google.api_calls()[-1] == ("PUT", f"/calendars/{CAL}/events/{eid}")
    assert ev["start"] == {"date": "2030-03-12"} and ev["end"] == {"date": "2030-03-13"}
    assert ev["summary"] == "Dentiste" and "colorId" not in ev and ev["reminders"] == {"useDefault": False}

    # done -> "✓ " and no reminders; undone -> back
    app_hub.patch(f"/tasks/{t['id']}", json={"due_time": "18:00"})
    app_hub.patch(f"/tasks/{t['id']}", json={"done": True})
    ev = google.events[CAL][eid]
    assert ev["summary"] == "✓ Dentiste" and ev["reminders"] == {"useDefault": False}
    app_hub.patch(f"/tasks/{t['id']}", json={"done": False})
    assert google.events[CAL][eid]["summary"] == "Dentiste"
    assert google.events[CAL][eid]["reminders"]["overrides"] == [{"method": "popup", "minutes": 10}]

    # changing only the note link pushes nothing
    n = len(google.api_calls())
    app_hub.patch(f"/tasks/{t['id']}", json={"note_id": None})
    assert len(google.api_calls()) == n

    # no date any more -> the event is deleted; deleting the task then costs no call
    app_hub.patch(f"/tasks/{t['id']}", json={"due": None})
    assert google.api_calls()[-1] == ("DELETE", f"/calendars/{CAL}/events/{eid}")
    assert google.events[CAL][eid]["status"] == "cancelled" and _sync_rows()[t["id"]][0] == "deleted"
    n = len(google.api_calls())
    app_hub.delete(f"/tasks/{t['id']}")
    assert len(google.api_calls()) == n

    # a deleted event comes back through PUT when the task gets a date again
    t2 = _task(app_hub, "Gym", DAY.isoformat())
    app_hub.delete(f"/tasks/{t2['id']}")
    assert google.events[CAL][gcal.event_id(t2["id"])]["status"] == "cancelled"
    # an undated task never touches Google
    n = len(google.api_calls())
    t3 = _task(app_hub, "Someday")
    app_hub.patch(f"/tasks/{t3['id']}", json={"title": "Someday maybe"})
    app_hub.delete(f"/tasks/{t3['id']}")
    assert len(google.api_calls()) == n


def test_signed_done_link_pushes_too(app_hub, google) -> None:
    from hub_api.routers.notify import done_sig
    from tests.hub_api.conftest import TOKEN

    t = _task(app_hub, "Call", DAY.isoformat(), "15:00")
    r = app_hub.client.post(f"/notify/done/{t['id']}?sig={done_sig(TOKEN, t['id'])}")
    assert r.status_code == 200
    assert google.events[CAL][gcal.event_id(t["id"])]["summary"] == "✓ Call"


def test_voice_note_tasks_go_through_the_same_push(app_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notes_mod, "local_today", lambda: DAY)
    monkeypatch.setattr(notes_mod, "local_now", lambda: datetime.combine(DAY, datetime.min.time(), TZ).replace(hour=8))
    fa.TRANSCRIPTS["groq"] = "demain à 15h appeler Karim"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "title": "Karim", "body": "Appeler Karim", "tags": [],
        "tasks": [{"title": "Appeler Karim", "due": "2030-03-13", "due_time": "15:00", "priority": "normal"},
                  {"title": "Sans date", "due": None}],
    }))
    r = app_hub.post("/notes/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")}, data={"mode": "both"})
    assert r.status_code == 201, r.text
    dated, undated = r.json()["tasks"]
    ev = google.events[CAL][gcal.event_id(dated["id"])]
    assert ev["summary"] == "Appeler Karim" and ev["start"]["dateTime"] == "2030-03-13T15:00:00"
    assert gcal.event_id(undated["id"]) not in google.events[CAL]


def test_mcp_tasks_push(make_hub, google) -> None:
    mcp = MCP(make_hub(mcp_plugins()).client)
    task = mcp.ok("tasks_add", title="From Claude", due="2030-03-12", due_time="11:00")
    assert google.events[CAL][gcal.event_id(task["id"])]["summary"] == "From Claude"
    mcp.ok("tasks_complete", id=task["id"])
    assert google.events[CAL][gcal.event_id(task["id"])]["summary"] == "✓ From Claude"


def test_push_failure_and_timeout_never_block(app_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    google.fail_status = 500
    t = _task(app_hub, "Flaky", DAY.isoformat())
    assert t["title"] == "Flaky"
    status, error = _sync_rows()[t["id"]]
    assert status == "failed" and "HTTP 500" in (error or "")

    google.fail_status = None
    google.delay = 3.0
    monkeypatch.setattr(gcal, "PUSH_TIMEOUT_S", 0.3)
    started = time.monotonic()
    t2 = _task(app_hub, "Slow", DAY.isoformat())
    assert time.monotonic() - started < 2.0
    assert _sync_rows()[t2["id"]] == ("failed", "timed out")
    google.delay = 0
    st = app_hub.get("/gcal/status").json()
    assert {e["task_id"] for e in st["errors"]} == {t["id"], t2["id"]}
    assert st["counts"]["failed"] == 2


# ------------------------------------------------------------------ reconcile


def test_reconcile_upserts_skips_unchanged_and_deletes_orphans(app_hub, google) -> None:
    google.fail_status = 503
    a = _task(app_hub, "A", DAY.isoformat(), "08:00")
    b = _task(app_hub, "B", DAY.isoformat())
    _task(app_hub, "No date")
    google.fail_status = None
    assert not google.events[CAL]
    # a task done 20 days ago stays out; an orphaned task event and a foreign event in the calendar
    old = _task(app_hub, "Old", "2030-01-01")
    fa.sql("UPDATE app_tasks SET done = 1, done_at = ? WHERE id = ?",
           [(datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), old["id"]])
    orphan = gcal.task_event({"id": "gone123", "title": "Gone", "due": DAY.isoformat()}, "x")
    google.events[CAL][orphan["id"]] = orphan
    google.events[CAL]["agent1"] = {"id": "agent1", "summary": "Lunch", "start": {"date": DAY.isoformat()},
                                    "end": {"date": "2030-03-13"},
                                    "extendedProperties": {"private": {"omnifreeKind": "agent"}}}

    out = app_hub.post("/gcal/sync").json()
    assert out["configured"] is True
    assert (out["upserted"], out["unchanged"], out["deleted"], out["failed"], out["remaining"]) == (2, 0, 1, 0, 0)
    assert out["orphans_checked"] is True
    assert google.events[CAL][orphan["id"]]["status"] == "cancelled"
    assert google.events[CAL]["agent1"].get("status") != "cancelled"
    assert google.events[CAL][gcal.event_id(old["id"])].get("status") != "cancelled"  # done long ago: kept as history
    assert {gcal.event_id(a["id"]), gcal.event_id(b["id"])} <= set(google.events[CAL])
    lists = [p for m, p, params in google.calls if m == "GET" and p.endswith("/events")]
    assert lists and [c[2] for c in google.calls if c[0] == "GET" and c[1].endswith("/events")][0][
        "privateExtendedProperty"] == "omnifreeKind=task"

    again = app_hub.post("/gcal/sync").json()
    assert (again["upserted"], again["unchanged"], again["deleted"]) == (0, 2, 0)
    full = app_hub.post("/gcal/sync", headers={"X-OmniFree-Full": "1"}).json()
    assert full["upserted"] == 2
    st = app_hub.get("/gcal/status").json()
    assert st["last_sync"]["upserted"] == 2 and st["last_sync"]["synced_at"]


def test_reconcile_respects_the_time_budget(app_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    google.fail_status = 503
    for i in range(3):
        _task(app_hub, f"T{i}", DAY.isoformat())
    google.fail_status = None
    real = time.monotonic
    ticks = iter(range(10_000))
    monkeypatch.setattr(gcal._time, "monotonic", lambda: real() + next(ticks) * 2.0)  # every check costs 2 s
    out = app_hub.post("/gcal/sync", headers={"X-OmniFree-Budget": "5"}).json()
    assert out["remaining"] > 0 and out["orphans_checked"] is False
    assert out["upserted"] + out["remaining"] == 3


# ------------------------------------------------------------------ agenda feed


def test_events_feed_filters_own_events_and_caches(app_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcal_router, "local_today", lambda: DAY)
    _task(app_hub, "My task", DAY.isoformat(), "10:00")  # an OmniFree event in CAL: left out
    google.add_owner_event("Standup", "2030-03-12T09:00:00+01:00", "2030-03-12T09:15:00+01:00", location="Zoom")
    google.add_owner_event("Holiday", "2030-03-14", "2030-03-15")
    google.add_owner_event("Too late", "2030-03-25T09:00:00Z")
    google.events[CAL]["agent1"] = {"id": "agent1", "summary": "Lunch with Sara",
                                    "start": {"dateTime": "2030-03-13T12:00:00Z"},
                                    "end": {"dateTime": "2030-03-13T13:00:00Z"}, "status": "confirmed"}
    out = app_hub.get("/gcal/events?days=7").json()
    assert out["configured"] is True and out["errors"] == []
    assert [e["title"] for e in out["events"]] == ["Standup", "Lunch with Sara", "Holiday"]
    standup = out["events"][0]
    assert standup["calendar"] == "Owner" and standup["calendar_id"] == PRIMARY and standup["all_day"] is False
    assert standup["start"].startswith("2030-03-12T") and standup["location"] == "Zoom"
    assert standup["link"].startswith("https://calendar.google.com/")
    assert set(standup) == {"id", "calendar", "calendar_id", "title", "start", "end", "all_day", "location", "link"}
    holiday = out["events"][2]
    assert holiday["all_day"] is True and holiday["start"] == "2030-03-14" and holiday["end"] == "2030-03-15"
    n = len(google.calls)
    app_hub.get("/gcal/events?days=7")
    assert len(google.calls) == n  # 5-minute cache

    google.fail_status = 500
    gcal.reset()
    broken = app_hub.get("/gcal/events?days=7").json()
    assert broken["events"] == [] and {e["calendar"] for e in broken["errors"]} == {PRIMARY, CAL}


def test_status_reports_roles_and_adds_shared_calendars_to_the_list(app_hub, google) -> None:
    st = app_hub.get("/gcal/status").json()
    assert st["configured"] is True and st["writable"] is True and st["sa_email"] == SA_EMAIL
    assert st["calendar_id"] == CAL and st["read_ids"] == [PRIMARY, CAL]
    roles = {c["id"]: c for c in st["calendars"]}
    assert roles[CAL]["access_role"] == "owner" and roles[CAL]["ok"] and roles[CAL]["write"]
    assert roles[PRIMARY]["access_role"] == "reader" and roles[PRIMARY]["ok"] and not roles[PRIMARY]["write"]
    assert ("POST", "/users/me/calendarList", {"id": PRIMARY}) in google.calls  # calendarList.insert on first use
    google.shared.pop(PRIMARY)
    google.listed.pop(PRIMARY)
    gcal.reset()
    st = app_hub.get("/gcal/status").json()
    primary = {c["id"]: c for c in st["calendars"]}[PRIMARY]
    assert primary["ok"] is False and primary["error"] == "not shared with the service account"


# ------------------------------------------------------------------ pushes


TOPIC = "omnifree-test-topic-gcal"


@pytest.fixture
def ntfy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"m{len(log)}"})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


def test_morning_push_has_an_agenda_section(app_hub, google, ntfy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify_mod, "local_today", lambda: DAY)
    _task(app_hub, "Pay rent", DAY.isoformat())
    google.add_owner_event("Standup", "2030-03-12T09:00:00+01:00", "2030-03-12T09:15:00+01:00")
    google.add_owner_event("Birthday", "2030-03-12", "2030-03-13")
    google.add_owner_event("Tomorrow", "2030-03-13T09:00:00+01:00")
    out = app_hub.post("/notify/run?kind=morning&preview=1").json()
    lines = out["message"].splitlines()
    assert out["title"] == "Bonjour : 1 tâche et 2 événements aujourd'hui"
    assert lines[:4] == ["Agenda :", "• Journée Birthday", f"• {_local_hhmm('2030-03-12T09:00:00+01:00')} Standup",
                         "Tâches :"]
    assert lines[4] == "• Pay rent"
    assert "Tomorrow" not in out["message"]


def _local_hhmm(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(TZ).strftime("%H:%M")


def test_reminders_include_timed_events_deduped(app_hub, google, ntfy, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2030, 3, 12, 8, 50, tzinfo=TZ)
    monkeypatch.setattr(notify_mod, "local_now", lambda: now)
    soon = (now + timedelta(minutes=10)).isoformat()
    google.add_owner_event("Standup", soon, (now + timedelta(minutes=25)).isoformat(), location="Zoom")
    google.add_owner_event("Later", (now + timedelta(hours=2)).isoformat())
    google.add_owner_event("All day", "2030-03-12", "2030-03-13")
    _task(app_hub, "Call", "2030-03-12", (now + timedelta(minutes=5)).strftime("%H:%M"))
    out = app_hub.post("/notify/run?kind=reminders").json()
    assert out["count"] == 2
    assert [m["title"] for m in ntfy] == ["⏰ Call", "📅 Standup"]
    assert ntfy[1]["message"].startswith(f"{now + timedelta(minutes=10):%H:%M}–")
    assert "(dans 10 min)" in ntfy[1]["message"] and "📍 Zoom" in ntfy[1]["message"]
    again = app_hub.post("/notify/run?kind=reminders").json()
    assert again["sent"] is False and len(ntfy) == 2
    keys = [r[0] for r in fa.sql("SELECT key FROM app_notify_log WHERE key LIKE 'event:%'")]
    assert keys == [f"event:own1:{(now + timedelta(minutes=10)).astimezone(timezone.utc):%Y-%m-%dT%H:%M}"]


def test_reminders_survive_a_calendar_outage(app_hub, google, ntfy, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2030, 3, 12, 8, 50, tzinfo=TZ)
    monkeypatch.setattr(notify_mod, "local_now", lambda: now)
    _task(app_hub, "Call", "2030-03-12", "08:55")
    google.fail_status = 500
    assert app_hub.post("/notify/run?kind=reminders").json()["count"] == 1


def test_digest_counts_calendar_events(app_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub_api.routers import digest as digest_mod

    monkeypatch.setattr(digest_mod, "local_today", lambda: DAY)
    google.add_owner_event("Standup", "2030-03-10T09:00:00+01:00")
    google.add_owner_event("Holiday", "2030-03-12", "2030-03-13")
    google.add_owner_event("Next week", "2030-03-20T09:00:00+01:00")
    out = app_hub.get("/digest?period=week").json()
    assert out["stats"]["calendar"] == {"events": 2, "all_day": 1}


# ------------------------------------------------------------------ MCP


def test_mcp_calendar_tools(make_hub, google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcal_router, "local_today", lambda: DAY)
    monkeypatch.setattr(gcal_router, "local_now", lambda: datetime(2030, 3, 12, 8, 0, tzinfo=TZ))
    mcp = MCP(make_hub(mcp_plugins()).client)
    google.add_owner_event("Standup", "2030-03-12T09:00:00+01:00")
    assert [e["title"] for e in mcp.ok("calendar_events", days=3)["events"]] == ["Standup"]

    ev = mcp.ok("calendar_add_event", title="Lunch", start="2030-03-13T12:30", location="Café")
    body = google.events[CAL][ev["id"]]
    assert body["start"] == {"dateTime": "2030-03-13T12:30:00", "timeZone": "Africa/Casablanca"}
    assert body["end"] == {"dateTime": "2030-03-13T13:30:00", "timeZone": "Africa/Casablanca"}
    assert body["location"] == "Café" and body["extendedProperties"]["private"] == {"omnifreeKind": "agent"}
    assert ev["title"] == "Lunch" and ev["all_day"] is False

    day = mcp.ok("calendar_add_event", title="Trip", start="2030-03-20", end="2030-03-22", all_day=True)
    assert google.events[CAL][day["id"]]["start"] == {"date": "2030-03-20"}
    assert google.events[CAL][day["id"]]["end"] == {"date": "2030-03-23"}
    phrase = mcp.ok("calendar_add_event", title="Call mom", start="demain à 15h")
    assert google.events[CAL][phrase["id"]]["start"]["dateTime"] == "2030-03-13T15:00:00"
    # the new events show in the agenda right away (cache dropped)
    titles = [e["title"] for e in mcp.ok("calendar_events", days=3)["events"]]
    assert titles == ["Standup", "Lunch", "Call mom"]

    bad = mcp.call("calendar_add_event", title="Bad", start="2030-03-13T12:00", end="2030-03-13T11:00")
    assert bad["isError"] is True and "end must be after start" in bad["content"][0]["text"]
    bad = mcp.call("calendar_add_event", title="Bad", start="someday maybe")
    assert bad["isError"] is True and "could not understand" in bad["content"][0]["text"]
