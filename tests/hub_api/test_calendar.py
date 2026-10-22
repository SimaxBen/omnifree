"""/calendar/{token}.ics: public ICS feed (token in the path), RFC 5545 folding/escaping, stable UIDs."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from hub_api.routers import calendar as cal_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.calendar import build_ics, fold_line, ics_escape
from tests.hub_api import fakes_apps as fa

FEED_TOKEN = "cal_9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c"


@pytest.fixture
def app_hub(make_hub, monkeypatch: pytest.MonkeyPatch):
    fa.reset()
    monkeypatch.setattr(notes_mod, "local_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(cal_mod, "local_today", lambda: date(2026, 10, 21))
    monkeypatch.setenv("CALENDAR_FEED_TOKEN", FEED_TOKEN)
    return make_hub(fa.apps_plugins())


def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_escape_and_fold() -> None:
    assert ics_escape("a,b;c\\d\nnext\r\nline") == "a\\,b\\;c\\\\d\\nnext\\nline"
    long = "SUMMARY:" + "é" * 100  # 2-byte characters must never be split
    folded = fold_line(long)
    parts = folded.split("\r\n")
    assert all(len(p.encode("utf-8")) <= 75 for p in parts)
    assert all(p.startswith(" ") for p in parts[1:])
    assert folded.replace("\r\n ", "") == long
    assert fold_line("short") == "short"
    ics = build_ics([{"uid": "u1", "date": date(2026, 1, 31), "summary": "x"}], now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "DTSTART;VALUE=DATE:20260131\r\nDTEND;VALUE=DATE:20260201\r\n" in ics
    assert "DTSTAMP:20260101T000000Z" in ics and ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", "")  # only CRLF line ends


def test_feed_content(app_hub) -> None:
    long_title = "Préparer le dossier, la demande; et le reste \\ " + "très long " * 8 + "fin"
    a = app_hub.post("/tasks", json={"title": long_title, "due": "2026-10-22", "details": "Ligne 1\nLigne 2",
                                     "priority": "high"}).json()
    app_hub.post("/tasks", json={"title": "Sans date"})
    d = app_hub.post("/tasks", json={"title": "Fait", "due": "2026-10-20"}).json()
    app_hub.patch(f"/tasks/{d['id']}", json={"done": True})
    nodue = app_hub.post("/tasks", json={"title": "Fini sans date"}).json()
    app_hub.patch(f"/tasks/{nodue['id']}", json={"done": True})
    old = app_hub.post("/tasks", json={"title": "Vieux", "due": "2026-08-01"}).json()
    app_hub.patch(f"/tasks/{old['id']}", json={"done": True})
    fa.sql("UPDATE app_tasks SET done_at = '2026-08-01T10:00:00Z' WHERE id = ?", [old["id"]])

    r = app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics")  # no bearer: public route
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/calendar") and "charset=utf-8" in r.headers["content-type"]
    raw = r.content.decode("utf-8")
    assert all(len(line.encode("utf-8")) <= 75 for line in raw.split("\r\n"))
    lines = _unfold(raw)
    assert lines[0] == "BEGIN:VCALENDAR" and "X-WR-CALNAME:OmniFree" in lines
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT1H" in lines and "X-PUBLISHED-TTL:PT1H" in lines
    assert lines.count("BEGIN:VEVENT") == 3  # open with due, done with due, done without due (done day)
    assert f"UID:task-{a['id']}@omnifree" in lines
    assert "SUMMARY:" + ics_escape(long_title) in lines
    assert "SUMMARY:Préparer le dossier\\, la demande\\; et le reste \\\\ très long" in "\n".join(lines)
    assert "DESCRIPTION:Ligne 1\\nLigne 2\\n\\nhttps://omnifree.ilyasouhnine.tech/#/notes/tasks" in lines
    assert "DTSTART;VALUE=DATE:20261022" in lines and "PRIORITY:1" in lines
    assert "SUMMARY:✓ Fait" in lines and "SUMMARY:✓ Fini sans date" in lines and "SUMMARY:✓ Vieux" not in lines
    assert "Sans date" not in raw.replace("Fini sans date", "")

    # Stable UIDs across fetches; ?done=0 leaves done tasks out.
    again = _unfold(app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").content.decode())
    assert [x for x in again if x.startswith("UID:")] == [x for x in lines if x.startswith("UID:")]
    open_only = app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics?done=0").text
    assert open_only.count("BEGIN:VEVENT") == 1


def test_token_mismatch_is_404(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    for path in ("/calendar/wrong.ics", f"/calendar/{FEED_TOKEN}x.ics", f"/calendar/{FEED_TOKEN[:-1]}.ics"):
        assert app_hub.client.get(path).status_code == 404
    monkeypatch.delenv("CALENDAR_FEED_TOKEN")
    assert app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").status_code == 404  # not configured: 404 too
    info = app_hub.get("/calendar/info").json()
    assert info == {"configured": False, "url": None, "token_key": "CALENDAR_FEED_TOKEN"}


def test_info_is_protected(app_hub) -> None:
    assert app_hub.client.get("/calendar/info").status_code == 401
    info = app_hub.get("/calendar/info").json()
    assert info["configured"] is True
    assert info["url"] == f"https://api.ilyasouhnine.tech/calendar/{FEED_TOKEN}.ics"


def test_recurring_money_only_when_table_exists(app_hub) -> None:
    app_hub.post("/tasks", json={"title": "T", "due": "2026-10-22"})
    assert "↻" not in app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").text  # no table: skipped gracefully
    fa.sql("CREATE TABLE app_money_recurring (id TEXT PRIMARY KEY, label TEXT, amount REAL, currency TEXT,"
           " type TEXT, cadence TEXT, next_date TEXT, day_of_month INTEGER)")
    fa.sql("INSERT INTO app_money_recurring VALUES ('s', 'Salaire', 8000, 'MAD', 'income', 'monthly', NULL, 25),"
           " ('n', 'Netflix', 65, 'MAD', 'expense', 'monthly', '2026-11-03', NULL),"
           " ('x', 'Bizarre', 'abc', 'MAD', 'expense', 'monthly', NULL, NULL)")
    lines = _unfold(app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").content.decode())
    assert "SUMMARY:↻ +8 000 MAD Salaire" in lines and "SUMMARY:↻ −65 MAD Netflix" in lines
    assert "UID:money-app_money_recurring-s@omnifree" in lines and lines.count("RRULE:FREQ=MONTHLY") == 2
    assert "DTSTART;VALUE=DATE:20261025" in lines and "DTSTART;VALUE=DATE:20261103" in lines


def test_money_page_recurring_items_in_feed(app_hub) -> None:
    r = app_hub.post("/money/recurring", json={"name": "Loyer", "amount": 3000, "day": 5, "start_month": "2026-09"})
    assert r.status_code == 201, r.text
    app_hub.post("/money/recurring", json={"name": "Ancien abo", "amount": 50, "start_month": "2025-01", "end_month": "2025-06"})
    r = app_hub.post("/money/incomes", json={"date": "2026-10-25", "amount": 12000, "source": "Salaire", "category": "salary",
                                            "recurring": True, "end_month": "2027-03"})
    assert r.status_code == 201, r.text
    app_hub.post("/money/incomes", json={"date": "2026-10-02", "amount": 500, "source": "Cadeau", "category": "gift"})
    lines = _unfold(app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").content.decode())
    assert "SUMMARY:↻ −3 000 MAD Loyer" in lines and "DTSTART;VALUE=DATE:20260905" in lines
    assert "SUMMARY:↻ +12 000 MAD Salaire" in lines and "RRULE:FREQ=MONTHLY;UNTIL=20270331" in lines
    assert not any("Ancien abo" in ln or "Cadeau" in ln for ln in lines)  # ended / one-off


# ------------------------------------------------------------------ timed tasks


def _event(lines: list[str], uid: str) -> list[str]:
    start = lines.index(f"UID:{uid}")
    return lines[start : lines.index("END:VEVENT", start) + 1]


def test_local_to_utc_follows_the_moroccan_offset() -> None:
    from hub_api.routers.calendar import local_to_utc

    # UTC+1 most of the year...
    assert local_to_utc(date(2026, 10, 22), "15:00") == datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)
    assert local_to_utc(date(2026, 10, 22), "00:30") == datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)
    # ...and UTC+0 around Ramadan (1 March 2026 is inside the 2026 switch).
    assert local_to_utc(date(2026, 3, 1), "15:00") == datetime(2026, 3, 1, 15, 0, tzinfo=timezone.utc)


def test_timed_tasks_are_timed_events_with_an_alarm(app_hub) -> None:
    timed = app_hub.post("/tasks", json={"title": "Dentiste", "due": "2026-10-22", "due_time": "15:00"}).json()
    ramadan = app_hub.post("/tasks", json={"title": "Iftar", "due": "2026-03-01", "due_time": "18:45"}).json()
    allday = app_hub.post("/tasks", json={"title": "Toute la journée", "due": "2026-10-23"}).json()
    done = app_hub.post("/tasks", json={"title": "Fait à l'heure", "due": "2026-10-20", "due_time": "09:00"}).json()
    app_hub.patch(f"/tasks/{done['id']}", json={"done": True})

    raw = app_hub.client.get(f"/calendar/{FEED_TOKEN}.ics").content.decode("utf-8")
    assert all(len(line.encode("utf-8")) <= 75 for line in raw.split("\r\n"))
    lines = _unfold(raw)

    ev = _event(lines, f"task-{timed['id']}@omnifree")
    assert "DTSTART:20261022T140000Z" in ev and "DTEND:20261022T143000Z" in ev  # 15:00 Casablanca = 14:00 UTC, 30 min
    assert not any(x.startswith("DTSTART;VALUE=DATE") for x in ev)
    alarm = ev[ev.index("BEGIN:VALARM") : ev.index("END:VALARM") + 1]
    assert alarm == ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Dentiste", "TRIGGER:-PT10M", "END:VALARM"]

    ev = _event(lines, f"task-{ramadan['id']}@omnifree")
    assert "DTSTART:20260301T184500Z" in ev and "DTEND:20260301T191500Z" in ev  # UTC+0 during Ramadan

    ev = _event(lines, f"task-{allday['id']}@omnifree")
    assert "DTSTART;VALUE=DATE:20261023" in ev and "DTEND;VALUE=DATE:20261024" in ev and "BEGIN:VALARM" not in ev

    ev = _event(lines, f"task-{done['id']}@omnifree")
    assert "SUMMARY:✓ Fait à l'heure" in ev and "DTSTART:20261020T080000Z" in ev
    assert "BEGIN:VALARM" not in ev  # no alarm for a done task


def test_build_ics_timed_event_shape() -> None:
    start = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)
    ics = build_ics([{"uid": "u", "date": date(2026, 10, 22), "start": start, "minutes": 45, "summary": "a,b", "alarm": 5}],
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    lines = ics.split("\r\n")
    assert "DTSTART:20261022T140000Z" in lines and "DTEND:20261022T144500Z" in lines
    assert "TRIGGER:-PT5M" in lines and "DESCRIPTION:a\\,b" in lines
    assert lines.index("END:VALARM") < lines.index("END:VEVENT")
