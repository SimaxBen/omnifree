"""/notes, /tasks and the voice-note pipeline (fake D1 on SQLite, scripted LLM, fake storage)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from hub_api.appdata import extract_json
from hub_api.routers import notes as notes_mod
from hub_api.routers.notes import derive_title, resolve_due
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)  # a Tuesday


@pytest.fixture(autouse=True)
def _reset_app_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(notes_mod, "local_today", lambda: TODAY)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


# ------------------------------------------------------------------ pure helpers


def test_resolve_due_relative_and_absolute() -> None:
    t = TODAY
    assert resolve_due("2026-10-23", t) == "2026-10-23"
    assert resolve_due("2026-10-23T09:00:00Z", t) == "2026-10-23"
    assert resolve_due("tomorrow", t) == "2026-10-21"
    assert resolve_due("Demain", t) == "2026-10-21"
    assert resolve_due("ghda", t) == "2026-10-21"
    assert resolve_due("today", t) == "2026-10-20"
    assert resolve_due("après-demain", t) == "2026-10-22"
    assert resolve_due("friday", t) == "2026-10-23"
    assert resolve_due("vendredi", t) == "2026-10-23"
    assert resolve_due("jem3a", t) == "2026-10-23"
    assert resolve_due("next monday", t) == "2026-10-26"
    assert resolve_due("lundi prochain", t) == "2026-10-26"
    assert resolve_due("tuesday", t) == "2026-10-27"  # the next one, never today
    assert resolve_due("in 3 days", t) == "2026-10-23"
    assert resolve_due("dans 2 jours", t) == "2026-10-22"
    assert resolve_due("next week", t) == "2026-10-27"
    for bad in (None, "", "null", "someday", "2026-13-01", "2020-01-01", "2099-01-01", 12):
        assert resolve_due(bad, t) is None


def test_derive_title_and_extract_json() -> None:
    assert derive_title("\n\n## **Groceries** list\n- milk") == "Groceries list"
    assert derive_title("- [ ] call mom") == "call mom"
    assert derive_title("   ") == "Untitled"
    assert len(derive_title("x" * 200)) == 80
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}
    assert extract_json("[1, 2]") is None
    assert extract_json("not json") is None
    assert extract_json(None) is None


# ------------------------------------------------------------------ notes CRUD


def test_notes_crud_search_and_notebooks(app_hub) -> None:
    r = app_hub.post("/notes", json={"body": "# Shopping\n- milk\n- eggs", "tags": ["#Home", "home", " errands "]})
    assert r.status_code == 201, r.text
    n1 = r.json()
    assert n1["title"] == "Shopping"
    assert n1["notebook"] == "Inbox"
    assert n1["tags"] == ["home", "errands"]
    assert n1["source"] == "text" and n1["audio_key"] is None and n1["pinned"] is False
    assert n1["created_at"].endswith("Z") and len(n1["id"]) == 12

    n2 = app_hub.post("/notes", json={"title": "Idea", "body": "100% *great* plan", "notebook": "Work"}).json()
    n3 = app_hub.post("/notes", json={"body": "third", "notebook": "Work", "pinned": True}).json()

    listing = app_hub.get("/notes").json()
    assert [n["id"] for n in listing["notes"]][0] == n3["id"]  # pinned first
    assert {n["id"] for n in listing["notes"]} == {n1["id"], n2["id"], n3["id"]}
    assert listing["notebooks"] == [{"name": "Inbox", "count": 1}, {"name": "Work", "count": 2}]

    assert [n["id"] for n in app_hub.get("/notes", params={"q": "EGGS"}).json()["notes"]] == [n1["id"]]
    assert [n["id"] for n in app_hub.get("/notes", params={"q": "errands"}).json()["notes"]] == [n1["id"]]
    assert [n["id"] for n in app_hub.get("/notes", params={"q": "100%"}).json()["notes"]] == [n2["id"]]
    assert app_hub.get("/notes", params={"q": "_"}).json()["notes"] == []  # LIKE wildcards are escaped
    assert len(app_hub.get("/notes", params={"notebook": "Work"}).json()["notes"]) == 2
    assert len(app_hub.get("/notes", params={"limit": 1}).json()["notes"]) == 1

    got = app_hub.get(f"/notes/{n2['id']}").json()
    assert got == n2

    r = app_hub.patch(f"/notes/{n2['id']}", json={"title": "Better idea", "tags": ["x"], "pinned": True, "notebook": "Ideas"})
    assert r.status_code == 200
    p = r.json()
    assert (p["title"], p["tags"], p["pinned"], p["notebook"]) == ("Better idea", ["x"], True, "Ideas")
    assert p["body"] == n2["body"] and p["created_at"] == n2["created_at"]
    assert app_hub.patch(f"/notes/{n2['id']}", json={"title": None}).status_code == 422
    assert app_hub.patch("/notes/nope", json={"title": "x"}).status_code == 404

    assert app_hub.delete(f"/notes/{n2['id']}").status_code == 204
    assert app_hub.get(f"/notes/{n2['id']}").status_code == 404
    assert app_hub.delete(f"/notes/{n2['id']}").status_code == 404


def test_note_validation(app_hub) -> None:
    assert app_hub.post("/notes", json={"body": "   "}).status_code == 422
    assert app_hub.post("/notes", json={"title": "only a title"}).status_code == 201


def test_schema_created_once_per_process(app_hub) -> None:
    for _ in range(3):
        app_hub.get("/notes")
        app_hub.get("/tasks")
    creates = [s for s in fa.SQL_LOG if s.startswith("CREATE TABLE IF NOT EXISTS app_notes")]
    assert len(creates) == 1


# ------------------------------------------------------------------ tasks


def test_tasks_crud_and_ordering(app_hub) -> None:
    t_none = app_hub.post("/tasks", json={"title": "someday"}).json()
    t_late = app_hub.post("/tasks", json={"title": "later", "due": "2026-11-05", "priority": "low"}).json()
    t_soon = app_hub.post("/tasks", json={"title": "soon", "due": "2026-10-21", "priority": "high", "details": "d"}).json()
    assert t_soon["done"] is False and t_soon["done_at"] is None and t_soon["priority"] == "high"
    assert t_none["due"] is None and t_none["priority"] == "normal"

    assert app_hub.post("/tasks", json={"title": "x", "due": "tomorrow"}).status_code == 422
    assert app_hub.post("/tasks", json={"title": "x", "priority": "urgent"}).status_code == 422
    assert app_hub.post("/tasks", json={"title": ""}).status_code == 422

    open_ids = [t["id"] for t in app_hub.get("/tasks").json()["tasks"]]
    assert open_ids == [t_soon["id"], t_late["id"], t_none["id"]]  # due asc, nulls last
    before = app_hub.get("/tasks", params={"due_before": "2026-11-01"}).json()["tasks"]
    assert [t["id"] for t in before] == [t_soon["id"]]
    assert app_hub.get("/tasks", params={"due_before": "nope"}).status_code == 422

    r = app_hub.patch(f"/tasks/{t_soon['id']}", json={"done": True})
    assert r.json()["done"] is True and r.json()["done_at"]
    assert [t["id"] for t in app_hub.get("/tasks", params={"status": "done"}).json()["tasks"]] == [t_soon["id"]]
    assert len(app_hub.get("/tasks", params={"status": "all"}).json()["tasks"]) == 3
    assert t_soon["id"] not in [t["id"] for t in app_hub.get("/tasks").json()["tasks"]]

    r = app_hub.patch(f"/tasks/{t_soon['id']}", json={"done": False, "due": None, "title": "  renamed "})
    body = r.json()
    assert (body["done"], body["done_at"], body["due"], body["title"]) == (False, None, None, "renamed")
    assert app_hub.patch(f"/tasks/{t_soon['id']}", json={"due": "2026-02-30"}).status_code == 422
    assert app_hub.patch(f"/tasks/{t_soon['id']}", json={"done": None}).status_code == 422
    assert app_hub.patch("/tasks/missing", json={"done": True}).status_code == 404

    assert app_hub.delete(f"/tasks/{t_none['id']}").status_code == 204
    assert app_hub.delete(f"/tasks/{t_none['id']}").status_code == 404


def test_deleting_a_note_unlinks_its_tasks(app_hub) -> None:
    note = app_hub.post("/notes", json={"body": "with tasks"}).json()
    task = app_hub.post("/tasks", json={"title": "linked", "note_id": note["id"]}).json()
    assert task["note_id"] == note["id"]
    app_hub.delete(f"/notes/{note['id']}")
    assert app_hub.get("/tasks").json()["tasks"][0]["note_id"] is None


# ------------------------------------------------------------------ voice pipeline

VOICE_JSON = {
    "title": "Weekly errands",
    "body": "## Errands\n- pharmacy\n- call the bank",
    "tags": ["Errands", "bank"],
    "tasks": [
        {"title": "Go to the pharmacy", "due": "tomorrow", "priority": "high", "details": None},
        {"title": "Call the bank", "due": "friday", "priority": "urgent!", "details": "about the card"},
        {"title": "Pay rent", "due": "2026-11-01", "priority": "normal"},
        {"title": "", "due": None},
        "Buy bread",
    ],
}


def _voice(app_hub, mode: str = "auto", content: bytes = b"OggS-audio-bytes", **form):
    return app_hub.post(
        "/notes/voice",
        files={"file": ("memo.ogg", content, "audio/ogg")},
        data={"mode": mode, **form},
    )


def test_voice_note_full_pipeline(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "ghda pharmacy, and call the bank on friday about the card, pay rent first of november"
    fa.CHAT_SCRIPT["groq"].append("```json\n" + json.dumps(VOICE_JSON) + "\n```")
    r = _voice(app_hub, notebook="Personal", language="fr")
    assert r.status_code == 201, r.text
    out = r.json()

    key = out["audio_key"]
    assert key.startswith("voice/2026/10/") and key.endswith(".ogg")
    assert fakes.STORES["r2"][key] == (b"OggS-audio-bytes", "audio/ogg")
    assert out["transcript"].startswith("ghda pharmacy")
    (_name, treq), = fa.TRANSCRIBE_LOG
    assert treq.language == "fr" and treq.audio == b"OggS-audio-bytes"

    note = out["note"]
    assert note["title"] == "Weekly errands" and note["source"] == "voice" and note["audio_key"] == key
    assert note["notebook"] == "Personal" and note["tags"] == ["errands", "bank"]
    assert note["body"].startswith("## Errands")

    tasks = {t["title"]: t for t in out["tasks"]}
    assert set(tasks) == {"Go to the pharmacy", "Call the bank", "Pay rent", "Buy bread"}
    assert tasks["Go to the pharmacy"]["due"] == "2026-10-21"
    assert tasks["Go to the pharmacy"]["priority"] == "high"
    assert tasks["Call the bank"]["due"] == "2026-10-23"
    assert tasks["Call the bank"]["priority"] == "normal"
    assert tasks["Call the bank"]["details"] == "about the card"
    assert tasks["Pay rent"]["due"] == "2026-11-01"
    assert all(t["note_id"] == note["id"] for t in out["tasks"])

    # The prompt carries today's date and weekday (Africa/Casablanca) and the JSON contract.
    (_name, creq), = fa.CHAT_LOG
    system = creq.messages[0].content
    assert "2026-10-20" in system and "Tuesday" in system and "Africa/Casablanca" in system
    assert "Friday = 2026-10-23" in system
    assert creq.response_format == {"type": "json_object"}

    # Persisted: visible through the regular routes.
    assert app_hub.get(f"/notes/{note['id']}").json()["source"] == "voice"
    assert len(app_hub.get("/tasks").json()["tasks"]) == 4


def test_voice_bad_json_falls_back_to_plain_note(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "Remember the blue notebook idea."
    fa.CHAT_SCRIPT["groq"].append("Sorry, I cannot do that")
    out = _voice(app_hub).json()
    assert out["note"]["body"] == "Remember the blue notebook idea."
    assert out["note"]["title"] == "Remember the blue notebook idea."
    assert out["note"]["tags"] == [] and out["tasks"] == []


def test_voice_llm_unavailable_falls_back(app_hub) -> None:
    from core.interfaces import ProviderUnavailable

    fa.TRANSCRIPTS["groq"] = "plain memo"
    for name in ("groq", "vision_b", "gemini"):
        fa.CHAT_SCRIPT[name].append(ProviderUnavailable("down", provider=name))
    r = _voice(app_hub)
    assert r.status_code == 201
    assert r.json()["note"]["body"] == "plain memo"


@pytest.mark.parametrize(
    ("mode", "llm_tasks", "expect_note", "expect_tasks"),
    [
        ("note", [{"title": "t1"}], True, 0),
        ("tasks", [{"title": "t1"}, {"title": "t2"}], False, 2),
        ("tasks", [], True, 0),  # nothing actionable: keep the note rather than lose the memo
        ("both", [{"title": "t1"}], True, 1),
        ("auto", [], True, 0),
    ],
)
def test_voice_modes(app_hub, mode, llm_tasks, expect_note, expect_tasks) -> None:
    fa.TRANSCRIPTS["groq"] = "something"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"title": "T", "body": "B", "tags": [], "tasks": llm_tasks}))
    out = _voice(app_hub, mode=mode).json()
    assert (out["note"] is not None) is expect_note
    assert len(out["tasks"]) == expect_tasks
    if out["tasks"] and out["note"] is None:
        assert all(t["note_id"] is None for t in out["tasks"])


def test_voice_limits_and_empty_speech(make_hub) -> None:
    hub = make_hub(fa.apps_plugins())  # max_upload_bytes = 50_000 in the test hub
    r = _voice(hub, content=b"x" * 50_001)
    assert r.status_code == 413
    assert _voice(hub, content=b"").status_code == 422
    assert _voice(hub, mode="poem").status_code == 422
    fa.TRANSCRIPTS["groq"] = "   "
    r = _voice(hub)
    assert r.status_code == 422 and "speech" in r.json()["detail"]
