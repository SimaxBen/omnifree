"""Task times: phrase parsing (FR/EN/Darija), the due_time column migration, the API and the voice pipeline."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.notes import normalize_time, resolve_due, resolve_when, split_time
from tests.hub_api import fakes_apps as fa

# Tuesday 20 October 2026, 16:00 in Casablanca.
NOW = datetime(2026, 10, 20, 16, 0, tzinfo=notes_mod.local_now().tzinfo)
TODAY = NOW.date()
TOMORROW = "2026-10-21"


@pytest.fixture(autouse=True)
def _frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(notes_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(notes_mod, "local_now", lambda: NOW)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        # the owner's examples
        ("demain à 15h", (TOMORROW, "15:00")),
        ("à 18h30", ("2026-10-20", "18:30")),  # still ahead today
        ("à 15h", (TOMORROW, "15:00")),  # already past at 16:00 and no day said -> tomorrow
        ("ce soir", ("2026-10-20", "20:00")),
        ("le matin", (TOMORROW, "09:00")),  # 09:00 has passed
        ("midi", (TOMORROW, "12:00")),
        ("vendredi 10h", ("2026-10-23", "10:00")),
        ("ghda m3a 3", (TOMORROW, "15:00")),  # bare 1-7 -> afternoon
        ("f 5 dyal l3chiya", ("2026-10-20", "17:00")),
        # French
        ("Demain matin", (TOMORROW, "09:00")),
        ("après-demain 16h30", ("2026-10-22", "16:30")),
        ("lundi prochain à 14h", ("2026-10-26", "14:00")),
        ("à 3h et demie", (TOMORROW, "15:30")),
        ("4h moins le quart", (TOMORROW, "15:45")),
        ("midi et demie", (TOMORROW, "12:30")),
        ("8h du soir", ("2026-10-20", "20:00")),
        ("2h du matin", (TOMORROW, "02:00")),
        ("cet après-midi à 4h", ("2026-10-20", "16:00")),
        ("ce matin", ("2026-10-20", "09:00")),  # explicitly today, even if past
        ("à 7", ("2026-10-20", "19:00")),
        ("à 07h", (TOMORROW, "07:00")),  # zero-padded: taken literally
        ("à 20", ("2026-10-20", "20:00")),
        ("minuit", (TOMORROW, "00:00")),
        ("dans 2 heures", ("2026-10-20", "18:00")),
        ("dans une heure", ("2026-10-20", "17:00")),
        ("dans 30 minutes", ("2026-10-20", "16:30")),
        ("la semaine prochaine à 10h", ("2026-10-27", "10:00")),
        ("Demain, à 9h15.", (TOMORROW, "09:15")),
        # English
        ("tomorrow at 3pm", (TOMORROW, "15:00")),
        ("tomorrow at 3 p.m.", (TOMORROW, "15:00")),
        ("next monday 9am", ("2026-10-26", "09:00")),
        ("on friday at noon", ("2026-10-23", "12:00")),
        ("tonight at 9", ("2026-10-20", "21:00")),
        ("this afternoon", ("2026-10-20", "15:00")),
        ("in 2 hours", ("2026-10-20", "18:00")),
        ("at 11:45", (TOMORROW, "11:45")),
        ("after tomorrow at 10", ("2026-10-22", "10:00")),
        # Darija
        ("ghda sbah", (TOMORROW, "09:00")),
        ("lyoum f 10 dyal sbah", ("2026-10-20", "10:00")),
        ("m3a 3 w nos", (TOMORROW, "15:30")),
        ("m3a 5 w rb3", ("2026-10-20", "17:15")),
        ("jem3a f 10", ("2026-10-23", "10:00")),
        ("nhar sebt m3a 11", ("2026-10-24", "11:00")),
        ("ba3d ghda f 4", ("2026-10-22", "16:00")),
        ("had lila", ("2026-10-20", "20:00")),
        ("ghda f lil", (TOMORROW, "20:00")),
        ("l3chiya", (TOMORROW, "15:00")),
        # ISO and day-only phrases
        ("2026-10-23 15:00", ("2026-10-23", "15:00")),
        ("2026-10-23T09:30", ("2026-10-23", "09:30")),
        ("2026-10-23T09:30:00Z", ("2026-10-23", None)),  # a UTC instant: the day only
        ("2026-10-23", ("2026-10-23", None)),
        ("demain", (TOMORROW, None)),
        ("f 3 ayam", ("2026-10-23", None)),
        ("dans 3 jours", ("2026-10-23", None)),
        ("l'tnin", ("2026-10-26", None)),
        # nonsense
        ("someday", (None, None)),
        ("le 15 novembre", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_resolve_when_phrases(phrase, expected) -> None:
    assert resolve_when(phrase, None, NOW) == expected


def test_rollover_depends_on_now() -> None:
    morning = NOW.replace(hour=8, minute=0)
    assert resolve_when("à 15h", None, morning) == ("2026-10-20", "15:00")
    assert resolve_when("le matin", None, morning) == ("2026-10-20", "09:00")
    assert resolve_when("midi", None, morning) == ("2026-10-20", "12:00")
    late = NOW.replace(hour=23, minute=30)
    assert resolve_when("ce soir", None, late) == ("2026-10-20", "20:00")  # "ce soir" stays today
    assert resolve_when("à 18h30", None, late) == (TOMORROW, "18:30")
    # Exactly now counts as passed: the reminder would be useless.
    assert resolve_when("à 16h", None, NOW) == (TOMORROW, "16:00")


def test_separate_time_argument() -> None:
    assert resolve_when("2026-10-23", "15:00", NOW) == ("2026-10-23", "15:00")
    assert resolve_when("vendredi", "9h", NOW) == ("2026-10-23", "09:00")
    assert resolve_when("demain à 10h", "11:00", NOW) == (TOMORROW, "11:00")  # the explicit time wins
    assert resolve_when(None, "18:30", NOW) == ("2026-10-20", "18:30")
    assert resolve_when(None, "08:00", NOW) == (TOMORROW, "08:00")
    assert resolve_when("2026-10-23", "garbage", NOW) == ("2026-10-23", None)
    assert resolve_when("someday", "15:00", NOW) == (None, None)
    assert resolve_when(None, None, NOW) == (None, None)


def test_normalize_and_split() -> None:
    assert normalize_time("15:00") == "15:00"
    assert normalize_time("9:05") == "09:05"
    assert normalize_time("15h") == "15:00"
    assert normalize_time("9h30") == "09:30"
    assert normalize_time("3pm") == "15:00"
    assert normalize_time("12am") == "00:00"
    assert normalize_time("ce soir") == "20:00"
    for bad in ("25:00", "x", "", None, "null", True, "demain 15h"):
        assert normalize_time(bad) is None
    assert split_time("demain à 15h") == ("demain", "15:00", False, None)
    assert split_time("tonight") == ("", "20:00", True, None)
    assert split_time("dans 3 jours") == ("dans 3 jours", None, False, None)
    assert split_time("payer 300 dh") == ("payer 300 dh", None, False, None)  # amounts are not hours


def test_date_resolvers_ignore_the_time_part() -> None:
    assert resolve_due("demain à 15h", TODAY) == TOMORROW
    assert resolve_due("vendredi soir", TODAY) == "2026-10-23"
    assert resolve_due("ce soir", TODAY) == "2026-10-20"
    assert resolve_due("à 15h", TODAY) is None  # no day in it
    # Money's past-date resolver: "hier soir" is yesterday, not today.
    assert money_mod.resolve_spent_date("hier soir", TODAY) == "2026-10-19"
    assert money_mod.resolve_spent_date("lbare7 f 8", TODAY) == "2026-10-19"
    assert money_mod.resolve_spent_date("lundi à 14h", TODAY) == "2026-10-19"


# ------------------------------------------------------------------ migration and API


def test_due_time_column_added_to_an_existing_table(app_hub) -> None:
    # A database created before due_time existed.
    fa.sql(
        "CREATE TABLE app_tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, details TEXT, due TEXT,"
        " done INTEGER NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal', note_id TEXT,"
        " created_at TEXT NOT NULL, done_at TEXT)"
    )
    fa.sql("INSERT INTO app_tasks (id, title, due, created_at) VALUES ('old1', 'Old task', '2026-10-21', '2026-10-01')")
    tasks = app_hub.get("/tasks").json()["tasks"]
    assert tasks == [{**tasks[0], "id": "old1", "due": "2026-10-21", "due_time": None}]
    cols = [r[1] for r in fa.sql("PRAGMA table_info(app_tasks)")]
    assert cols.count("due_time") == 1
    alters = [s for s in fa.SQL_LOG if s.startswith("ALTER TABLE app_tasks ADD COLUMN due_time")]
    for _ in range(3):
        app_hub.get("/tasks")
    assert len([s for s in fa.SQL_LOG if s.startswith("ALTER TABLE app_tasks")]) == len(alters) == 1  # once per process
    patched = app_hub.patch("/tasks/old1", json={"due_time": "09:30"}).json()
    assert patched["due_time"] == "09:30"


def test_migration_is_idempotent_on_a_fresh_table(app_hub) -> None:
    assert app_hub.post("/tasks", json={"title": "t", "due": "2026-10-21", "due_time": "10:00"}).status_code == 201
    # A second process (new AppDB) runs the ALTER again: "duplicate column" is ignored.
    app_hub.app.state.appdb = None
    assert app_hub.get("/tasks").status_code == 200


def test_task_api_due_time(app_hub) -> None:
    r = app_hub.post("/tasks", json={"title": "Dentist", "due": "2026-10-22", "due_time": "9:05"})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["due"] == "2026-10-22" and t["due_time"] == "09:05"
    assert app_hub.post("/tasks", json={"title": "x", "due_time": "10:00"}).status_code == 422  # time needs a day
    for bad in ("25:00", "10:60", "10", "noon", "10:00pm"):
        r = app_hub.post("/tasks", json={"title": "x", "due": "2026-10-22", "due_time": bad})
        assert r.status_code == 422, bad
        assert "HH:MM" in r.json()["detail"]
    untimed = app_hub.post("/tasks", json={"title": "Untimed", "due": "2026-10-22", "due_time": ""}).json()
    assert untimed["due_time"] is None

    # PATCH: set, change, clear the time; clearing the day clears the time.
    assert app_hub.patch(f"/tasks/{t['id']}", json={"due_time": "18:45"}).json()["due_time"] == "18:45"
    assert app_hub.patch(f"/tasks/{t['id']}", json={"due_time": None}).json()["due_time"] is None
    app_hub.patch(f"/tasks/{t['id']}", json={"due_time": "07:00"})
    cleared = app_hub.patch(f"/tasks/{t['id']}", json={"due": None}).json()
    assert cleared["due"] is None and cleared["due_time"] is None
    assert app_hub.patch(f"/tasks/{t['id']}", json={"due_time": "08:00"}).status_code == 422
    both = app_hub.patch(f"/tasks/{t['id']}", json={"due": "2026-10-24", "due_time": "08:00"}).json()
    assert both["due"] == "2026-10-24" and both["due_time"] == "08:00"
    assert app_hub.patch(f"/tasks/{t['id']}", json={"due": None, "due_time": "08:00"}).status_code == 422


def test_tasks_sorted_by_date_then_time(app_hub) -> None:
    ids = {}
    for title, due, at in (
        ("late", "2026-10-22", "18:00"), ("allday", "2026-10-22", None), ("early", "2026-10-22", "08:30"),
        ("before", "2026-10-21", "23:00"), ("nodate", None, None),
    ):
        body = {"title": title, **({"due": due} if due else {}), **({"due_time": at} if at else {})}
        ids[title] = app_hub.post("/tasks", json=body).json()["id"]
    order = [t["title"] for t in app_hub.get("/tasks").json()["tasks"]]
    assert order == ["before", "early", "late", "allday", "nodate"]


def test_voice_tasks_get_times(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "ghda m3a 3 3iyet l Karim, w f 5 dyal l3chiya sir l pharmacie, et vendredi 10h dentiste"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "title": "Rappels", "body": "- Karim\n- pharmacie\n- dentiste", "tags": [],
        "tasks": [
            # The LLM got the arithmetic wrong; the speaker's words win.
            {"title": "Appeler Karim", "due": "2026-10-20", "due_time": "03:00", "when": "ghda m3a 3"},
            {"title": "Pharmacie", "due": None, "due_time": None, "when": "f 5 dyal l3chiya"},
            {"title": "Dentiste", "due": "2026-10-23", "due_time": "10:00", "when": "vendredi 10h"},
            {"title": "Sans heure", "due": "tomorrow", "when": None},
            {"title": "Heure seule", "due": None, "due_time": "18:30"},
            {"title": "Mots inconnus", "due": "2026-10-25", "due_time": "11:00", "when": "quand j'ai le temps"},
            {"title": "Jour dit, heure à part", "due": "2026-10-23", "due_time": "14:00", "when": "vendredi"},
        ],
    }))
    r = app_hub.post("/notes/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")}, data={"mode": "both"})
    assert r.status_code == 201, r.text
    got = {t["title"]: (t["due"], t["due_time"]) for t in r.json()["tasks"]}
    assert got == {
        "Appeler Karim": (TOMORROW, "15:00"),
        "Pharmacie": ("2026-10-20", "17:00"),
        "Dentiste": ("2026-10-23", "10:00"),
        "Sans heure": (TOMORROW, None),
        "Heure seule": ("2026-10-20", "18:30"),
        "Mots inconnus": ("2026-10-25", "11:00"),  # unparseable words: the LLM's fields
        "Jour dit, heure à part": ("2026-10-23", "14:00"),
    }
    (_name, creq), = fa.CHAT_LOG
    system = creq.messages[0].content
    assert "the time is 16:00" in system and '"due_time"' in system and '"when"' in system
    assert "ghda m3a 3" in system and "ce soir" in system


def test_voice_prompt_without_clock() -> None:
    prompt = notes_mod._voice_prompt(date(2026, 10, 20))
    assert "the time is" not in prompt and "2026-10-20" in prompt
