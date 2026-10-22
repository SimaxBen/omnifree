"""/notes/meeting: resumable transcription of uploaded meeting parts, one LLM summary, the Meetings note and tasks."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from hub_api.appdata import local_tz
from hub_api.routers import meetings as meet_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.meetings import clip_transcript, meeting_body, parse_summary
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)  # a Tuesday
NOW = datetime(2026, 10, 20, 11, 0, tzinfo=local_tz())

SUMMARY = {
    "title": "Budget sync",
    "summary": "- Reviewed Q4 spend\n- Agreed on cuts",
    "decisions": ["Cancel the unused SaaS seats"],
    "action_items": [
        {"task": "Send the revised budget", "owner": "Sara", "due": "friday", "due_time": None, "priority": "high"},
        {"task": "Book the follow-up", "owner": None, "due": "2026-10-27", "due_time": "10:00"},
        "Update the wiki",
    ],
    "tags": ["budget", "#Q4"],
}


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(meet_mod, "local_now", lambda: NOW)
    monkeypatch.setattr(notes_mod, "local_today", lambda: TODAY)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _upload(hub, key: str, data: bytes = b"OggS-fake-audio") -> dict:
    r = hub.post("/drive/upload", files={"file": ("part.webm", data, "audio/webm")}, data={"key": key})
    assert r.status_code == 201, r.text
    return {"key": r.json()["key"], "provider": r.json()["backend"], "seconds": 600}


# ------------------------------------------------------------------ pure helpers


def test_parse_summary_and_body() -> None:
    res = parse_summary(SUMMARY, NOW)
    assert res["title"] == "Budget sync" and res["llm"] is True
    assert res["tags"] == ["budget", "q4"]
    a1, a2, a3 = res["actions"]
    assert (a1["title"], a1["owner"], a1["due"], a1["priority"]) == ("Send the revised budget", "Sara", "2026-10-23", "high")
    assert (a2["owner"], a2["due"], a2["due_time"]) == (None, "2026-10-27", "10:00")
    assert (a3["title"], a3["due"]) == ("Update the wiki", None)
    fallback = parse_summary(None, NOW)
    assert fallback["title"] == "Meeting 2026-10-20" and fallback["actions"] == [] and fallback["llm"] is False

    parts = [{"key": "a", "seconds": 600, "status": "done"}, {"key": "b", "seconds": 300, "status": "failed"}]
    body = meeting_body(res, "Hello all.\n::: sneaky\nBye.", parts, TODAY)
    assert body.startswith("*Meeting of 2026-10-20, about 15 min, 2 parts*")
    assert "## Decisions\n\n- Cancel the unused SaaS seats" in body
    assert "- [ ] Send the revised budget (**Sara**, due 2026-10-23)" in body
    assert "- [ ] Book the follow-up (due 2026-10-27 10:00)" in body
    assert "> Part 2 could not be transcribed." in body
    assert body.rstrip().endswith(":::details Full transcript\nHello all.\n:: sneaky\nBye.\n:::")


def test_clip_transcript_keeps_head_and_tail() -> None:
    assert clip_transcript("short", 100) == "short"
    clipped = clip_transcript("A" * 60 + "B" * 60, 40)
    assert clipped.startswith("A" * 20) and clipped.endswith("B" * 20) and "80 characters" in clipped


# ------------------------------------------------------------------ routes


def test_meeting_parts_resumable_then_summary(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet_mod, "START_BEFORE_S", -1.0)  # one part per call
    monkeypatch.setattr(meet_mod, "SUMMARY_BEFORE_S", -1.0)  # summary in a call of its own
    parts = [_upload(app_hub, f"meetings/2026/10/m1/part-{i}.webm") for i in range(3)]
    fa.TRANSCRIPTS["groq"] = "We discussed the budget."
    fa.CHAT_SCRIPT["groq"].append(json.dumps(SUMMARY))

    r = app_hub.post("/notes/meeting", json={"audio_keys": parts, "language": "fr"})
    assert r.status_code == 200, r.text
    out = r.json()
    mid = out["meeting_id"]
    assert (out["status"], out["parts_total"], out["parts_done"], out["remaining"]) == ("transcribing", 3, 1, 2)
    assert fa.TRANSCRIBE_LOG[0][1].language == "fr" and fa.TRANSCRIBE_LOG[0][1].filename == "part-0.webm"
    out = app_hub.post("/notes/meeting", json={"meeting_id": mid}).json()
    assert out["remaining"] == 1
    out = app_hub.post("/notes/meeting", json={"meeting_id": mid}).json()
    assert (out["status"], out["remaining"]) == ("summarizing", 0)  # the last part used this call's budget
    assert app_hub.get(f"/notes/meeting/{mid}").json()["status"] == "summarizing"
    out = app_hub.post("/notes/meeting", json={"meeting_id": mid}).json()
    assert out["status"] == "done" and len(fa.TRANSCRIBE_LOG) == 3 and len(fa.CHAT_LOG) == 1

    note = out["note"]
    assert note["title"] == "Budget sync" and note["notebook"] == "Meetings" and note["source"] == "meeting"
    assert note["tags"] == ["meeting", "budget", "q4"] and note["audio_key"] == parts[0]["key"]
    assert "about 30 min, 3 parts" in note["body"] and ":::details Full transcript" in note["body"]
    assert note["body"].count("We discussed the budget.") == 3
    tasks = out["tasks"]
    assert [t["title"] for t in tasks] == ["Send the revised budget", "Book the follow-up", "Update the wiki"]
    assert tasks[0]["details"] == "Owner: Sara\nFrom the meeting: Budget sync" and tasks[0]["due"] == "2026-10-23"
    assert all(t["note_id"] == note["id"] for t in tasks) and tasks[1]["due_time"] == "10:00"
    assert sorted(out["task_ids"]) == sorted(t["id"] for t in tasks)
    prompt = fa.CHAT_LOG[0][1].messages[0].content
    assert "Tuesday 2026-10-20" in prompt

    # done is final: calling again does nothing new
    again = app_hub.post("/notes/meeting", json={"meeting_id": mid}).json()
    assert again["status"] == "done" and again["note"]["id"] == note["id"] and len(fa.CHAT_LOG) == 1


def test_meeting_parts_sent_while_recording(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "part text"
    p1 = _upload(app_hub, "meetings/m2/part-0.webm")
    out = app_hub.post("/notes/meeting", json={"audio_keys": [p1], "final": False, "title": "Weekly"}).json()
    assert out["status"] == "waiting" and out["final"] is False and fa.CHAT_LOG == []
    p2 = _upload(app_hub, "meetings/m2/part-1.webm")
    fa.CHAT_SCRIPT["groq"].append(RuntimeError("all providers down"))
    out = app_hub.post("/notes/meeting", json={"meeting_id": out["meeting_id"], "audio_keys": [p1, p2]}).json()
    assert out["status"] == "done" and out["parts_total"] == 2 and len(fa.TRANSCRIBE_LOG) == 2
    assert out["note"]["title"] == "Weekly" and out["tasks"] == []  # the summary failed: transcript only
    assert out["error"] == "the summary could not be written; transcript saved"
    assert "## Summary" not in out["note"]["body"] and "part text\n\npart text" in out["note"]["body"]


def test_meeting_errors_lease_and_oversized_parts(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    assert app_hub.post("/notes/meeting", json={}).status_code == 422
    assert app_hub.post("/notes/meeting", json={"meeting_id": "nope"}).status_code == 404
    assert app_hub.get("/notes/meeting/nope").status_code == 404
    assert app_hub.post("/notes/meeting", json={"audio_key": "../etc"}).status_code == 400

    monkeypatch.setattr(meet_mod, "WHISPER_MAX_BYTES", 10)
    big = _upload(app_hub, "meetings/m3/part-0.webm", b"x" * 50)
    fa.TRANSCRIPTS["groq"] = "ok"
    out = app_hub.post("/notes/meeting", json={"audio_keys": [big, "meetings/m3/missing.webm"]}).json()
    assert out["status"] == "done" and out["parts_failed"] == 2 and fa.TRANSCRIBE_LOG == []
    assert "Parts 1, 2 could not be transcribed." in out["note"]["body"]
    assert "(nothing was heard)" in out["note"]["body"]

    small = _upload(app_hub, "meetings/m4/part-0.webm", b"tiny")
    out = app_hub.post("/notes/meeting", json={"audio_keys": [small], "final": False}).json()
    fa.sql("UPDATE app_meetings SET locked_until = '2999-01-01T00:00:00Z' WHERE id = ?", [out["meeting_id"]])
    busy = app_hub.post("/notes/meeting", json={"meeting_id": out["meeting_id"]}).json()
    assert busy["busy"] is True and busy["status"] == "waiting"


def test_merge_parts_keeps_parts_added_meanwhile() -> None:
    mine = [{"key": "a", "status": "done", "text": "A"}, {"key": "b", "status": "pending"}]
    stored = [{"key": "a", "status": "pending"}, {"key": "b", "status": "pending"}, {"key": "c", "status": "pending"}]
    merged = meet_mod.merge_parts(stored, mine, {"a"})
    assert [(p["key"], p["status"]) for p in merged] == [("a", "done"), ("b", "pending"), ("c", "pending")]
    assert meet_mod.merge_parts([], mine, {"a"}) == [mine[0]]


def test_meeting_part_added_by_a_concurrent_call_is_not_lost(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet_mod, "START_BEFORE_S", -1.0)  # one part per call
    fa.TRANSCRIPTS["groq"] = "words"
    p1 = _upload(app_hub, "meetings/m5/part-0.webm")
    out = app_hub.post("/notes/meeting", json={"audio_keys": [p1], "final": False}).json()
    mid = out["meeting_id"]
    p2 = _upload(app_hub, "meetings/m5/part-1.webm")
    p3 = _upload(app_hub, "meetings/m5/part-2.webm")
    real = meet_mod._transcribe_part

    async def slow(state, part, language):  # a retried POST lands while this call is transcribing part 2
        text = await real(state, part, language)
        if part["key"] == p2["key"]:
            stored = fa.sql("SELECT parts FROM app_meetings WHERE id = ?", [mid])[0][0]
            parts = json.loads(stored) + [{**p3, "status": "pending"}]
            fa.sql("UPDATE app_meetings SET parts = ? WHERE id = ?", [json.dumps(parts), mid])
        return text

    monkeypatch.setattr(meet_mod, "_transcribe_part", slow)
    out = app_hub.post("/notes/meeting", json={"meeting_id": mid, "audio_keys": [p2], "final": False}).json()
    assert (out["parts_total"], out["parts_done"], out["status"]) == (3, 2, "transcribing")
    out = app_hub.post("/notes/meeting", json={"meeting_id": mid, "final": True}).json()
    assert out["status"] == "done" and out["parts_total"] == 3 and out["note"]["body"].count("words") == 3


def test_meeting_only_takes_meeting_audio_from_a_storage_backend(app_hub) -> None:
    receipt = app_hub.post("/drive/upload", files={"file": ("r.pdf", b"%PDF", "application/pdf")},
                           data={"key": "receipts/2026/r.pdf"}).json()
    r = app_hub.post("/notes/meeting", json={"audio_keys": [{"key": receipt["key"]}]})
    assert r.status_code == 422 and "not a meeting recording" in r.text
    assert app_hub.post("/notes/meeting", json={"audio_key": "meetings/x/notes.txt"}).status_code == 422
    assert app_hub.post("/notes/meeting", json={"audio_keys": [{"key": "meetings/x/p.webm", "provider": "groq"}]}).status_code == 400
    assert app_hub.post("/notes/meeting", json={"audio_keys": [{"key": "meetings/x/p.webm", "provider": "nope"}]}).status_code == 404
    assert fa.TRANSCRIBE_LOG == []

    p = _upload(app_hub, "meetings/m6/part-0.webm")
    fa.sql("DELETE FROM app_meetings")
    out = app_hub.post("/notes/meeting", json={"audio_keys": [{**p, "key": "meetings/m6/gone.webm"}]}).json()
    assert out["parts_failed"] == 1 and fa.TRANSCRIBE_LOG == []  # stat says missing: nothing downloaded


def test_meeting_done_answer_is_complete_and_finish_is_not_repeated(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    fa.TRANSCRIPTS["groq"] = "We discussed the budget."
    fa.CHAT_SCRIPT["groq"].append(json.dumps(SUMMARY))
    p = _upload(app_hub, "meetings/m7/part-0.webm")
    real = notes_mod._insert_task
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("D1 hiccup")
        return await real(*a, **k)

    monkeypatch.setattr(notes_mod, "_insert_task", flaky)
    with pytest.raises(RuntimeError):  # the call dies after the note and the first task
        app_hub.post("/notes/meeting", json={"audio_keys": [p]})
    monkeypatch.setattr(notes_mod, "_insert_task", real)
    mid = fa.sql("SELECT id FROM app_meetings")[0][0]

    out = app_hub.post("/notes/meeting", json={"meeting_id": mid}).json()  # the retry completes, no second note
    assert out["status"] == "done" and len(fa.CHAT_LOG) == 1
    assert fa.sql("SELECT COUNT(*) FROM app_notes WHERE source = 'meeting'")[0][0] == 1
    titles = ["Send the revised budget", "Book the follow-up", "Update the wiki"]
    assert [t["title"] for t in out["tasks"]] == titles  # the first one (made before the cut) is not made twice
    assert fa.sql("SELECT COUNT(*) FROM app_tasks")[0][0] == 3
    assert out["summary"].startswith("- Reviewed") and out["decisions"] == ["Cancel the unused SaaS seats"]

    again = app_hub.post("/notes/meeting", json={"meeting_id": mid, "final": True}).json()
    assert again["status"] == "done" and again["note"]["id"] == out["note"]["id"]
    assert again["decisions"] == ["Cancel the unused SaaS seats"] and again["summary"] == out["summary"]
    assert [t["id"] for t in again["tasks"]] == [t["id"] for t in out["tasks"]]
