"""POST /drive/transcribe: a Drive audio file -> Whisper (faked) -> a note linking back to the file."""

from __future__ import annotations

import asyncio

import pytest

from hub_api.routers import drive as drive_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.drive import transcript_note
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _put(key: str, data: bytes = b"OggS-meeting-audio", provider: str = "r2") -> None:
    fakes.STORES[provider][key] = (data, "audio/ogg")


def test_transcribe_creates_note_with_link_back(app_hub) -> None:
    _put("recordings/Team_sync 2026.ogg")
    fa.TRANSCRIPTS["groq"] = "  We agreed to ship the previews on Friday.  "
    r = app_hub.post(
        "/drive/transcribe",
        json={"provider": "r2", "key": "recordings/Team_sync 2026.ogg", "notebook": "Work", "language": "en"},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["transcript"] == "We agreed to ship the previews on Friday."
    note = out["note"]
    assert note["title"] == "Team sync 2026 (transcript)"
    assert note["notebook"] == "Work"
    assert note["source"] == "voice" and note["audio_key"] == "recordings/Team_sync 2026.ogg"
    assert note["tags"] == ["transcript"]
    assert note["body"].startswith("We agreed to ship the previews on Friday.")
    assert "`recordings/Team_sync 2026.ogg` on r2" in note["body"]

    (_name, req), = fa.TRANSCRIBE_LOG
    assert req.audio == b"OggS-meeting-audio"
    assert req.filename == "Team_sync 2026.ogg"
    assert req.language == "en"
    # Persisted like any other note, and the file was not copied anywhere.
    assert app_hub.get(f"/notes/{note['id']}").json()["audio_key"] == "recordings/Team_sync 2026.ogg"
    assert list(fakes.STORES["r2"]) == ["recordings/Team_sync 2026.ogg"]


def test_transcribe_defaults_to_inbox_and_auto_language(app_hub) -> None:
    _put("memo.m4a")
    fa.TRANSCRIPTS["groq"] = "hello"
    out = app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "memo.m4a"}).json()
    assert out["note"]["notebook"] == "Inbox"
    assert out["note"]["title"] == "memo (transcript)"
    assert fa.TRANSCRIBE_LOG[0][1].language is None


def test_transcribe_refuses_files_over_whisper_limit(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_mod, "TRANSCRIBE_MAX_BYTES", 10)
    _put("big.mp3", b"x" * 11)
    r = app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "big.mp3"})
    assert r.status_code == 413
    assert "transcription takes audio up to" in r.json()["detail"]
    assert fa.TRANSCRIBE_LOG == []  # refused before calling Whisper


def test_transcribe_errors(app_hub) -> None:
    r = app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "missing.ogg"})
    assert r.status_code >= 400 and r.status_code != 500
    assert app_hub.post("/drive/transcribe", json={"provider": "nope", "key": "a.ogg"}).status_code == 404
    assert app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "../etc/passwd"}).status_code == 400
    assert app_hub.post("/drive/transcribe", json={"provider": "r2"}).status_code == 422

    _put("silence.ogg")
    fa.TRANSCRIPTS["groq"] = "   "
    r = app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "silence.ogg"})
    assert r.status_code == 422 and "speech" in r.json()["detail"]
    assert app_hub.get("/notes").json()["notes"] == []

    _put("empty.ogg", b"")
    assert app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "empty.ogg"}).status_code == 422


def test_transcribe_is_bounded_in_time(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(*_args, **_kwargs) -> str:
        await asyncio.sleep(5)
        return "late"

    monkeypatch.setattr(drive_mod, "TRANSCRIBE_BUDGET_S", 0.0)  # budget floor is 1 s
    monkeypatch.setattr(notes_mod, "transcribe_audio", slow)
    _put("long.ogg")
    r = app_hub.post("/drive/transcribe", json={"provider": "r2", "key": "long.ogg"})
    assert r.status_code == 504
    assert "too long" in r.json()["detail"]
    assert app_hub.get("/notes").json()["notes"] == []


def test_transcript_note_title_and_body() -> None:
    title, body = transcript_note("a/b/voice_memo.final.webm", "b2", "text")
    assert title == "voice memo.final (transcript)"
    assert body == "text\n\n---\n_Transcribed from the Drive file `a/b/voice_memo.final.webm` on b2._"
    assert transcript_note(".hidden", "r2", "t")[0] == ".hidden (transcript)"
