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
