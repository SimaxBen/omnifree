"""Idempotent creates for the PWA's offline write queue: client ids and Idempotency-Key replays."""

from __future__ import annotations

import json

import pytest

from hub_api import idempotency
from tests.hub_api import fakes_apps as fa


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


CREATES = [
    ("/tasks", {"title": "Buy milk", "priority": "high"}, "/tasks?status=all", "tasks"),
    ("/notes", {"body": "# Idea\nwrite it down"}, "/notes", "notes"),
    ("/expenses", {"merchant": "Café", "total": 12.5, "date": "2026-10-20"}, "/expenses?month=2026-10", "expenses"),
    ("/money/incomes", {"amount": 900, "source": "Client", "date": "2026-10-20"}, "/money/incomes", "incomes"),
]


@pytest.mark.parametrize(("url", "body", "listing", "field"), CREATES)
def test_client_id_makes_creates_idempotent(app_hub, url, body, listing, field) -> None:
    cid = "c_offline_0001"
    r = app_hub.post(url, json={**body, "id": cid})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == cid and idempotency.REPLAY_HEADER.lower() not in r.headers

    again = app_hub.post(url, json={**body, "id": cid})  # the offline queue replays the same request
    assert again.status_code == 200 and again.headers[idempotency.REPLAY_HEADER] == "true"
    assert again.json() == r.json()
    rows = [x for x in app_hub.get(listing).json()[field] if x["id"] == cid]
    assert len(rows) == 1

    # Without an id the hub still generates one (12 base62 characters).
    plain = app_hub.post(url, json=body).json()
    assert plain["id"] != cid and len(plain["id"]) == 12


@pytest.mark.parametrize(("url", "body", "_listing", "_field"), CREATES)
def test_client_id_format_is_validated(app_hub, url, body, _listing, _field) -> None:
    for bad in ("short", "has space in it", "x" * 65, "semi;colon-id", "../../etc/x"):
        r = app_hub.post(url, json={**body, "id": bad})
        assert r.status_code == 422, (bad, r.text)


def test_replay_answers_the_current_row(app_hub) -> None:
    cid = "task_from_phone_1"
    app_hub.post("/tasks", json={"id": cid, "title": "Call the bank"})
    app_hub.patch(f"/tasks/{cid}", json={"done": True})
    again = app_hub.post("/tasks", json={"id": cid, "title": "Call the bank"})
    assert again.status_code == 200 and again.json()["done"] is True


def test_idempotency_key_header(app_hub) -> None:
    headers = {"Idempotency-Key": "key-123456789"}
    first = app_hub.post("/tasks", json={"title": "Water plants"}, headers=headers)
    assert first.status_code == 201
    second = app_hub.post("/tasks", json={"title": "Water plants"}, headers=headers)
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]
    assert [t["title"] for t in app_hub.get("/tasks").json()["tasks"]].count("Water plants") == 1

    # Keys are scoped per endpoint: the same key on /notes creates a note.
    note = app_hub.post("/notes", json={"body": "plants"}, headers=headers)
    assert note.status_code == 201

    # Deleted since: nothing is re-created, and the deleted content does not come back either.
    app_hub.delete(f"/tasks/{first.json()['id']}")
    third = app_hub.post("/tasks", json={"title": "Water plants"}, headers=headers)
    assert third.status_code == 410 and "Water plants" not in third.text
    assert app_hub.get("/tasks").json()["tasks"] == []
    stored = [r[0] for r in fa.sql("SELECT response FROM app_idempotency")]
    assert stored and not any("plants" in x for x in stored)  # only ids are kept

    assert app_hub.post("/tasks", json={"title": "x"}, headers={"Idempotency-Key": "bad key"}).status_code == 422


def test_idempotency_rows_expire(app_hub) -> None:
    app_hub.post("/tasks", json={"title": "Old"}, headers={"Idempotency-Key": "old-key-0001"})
    fa.sql("UPDATE app_idempotency SET created_at = '2020-01-01T00:00:00Z'")
    app_hub.post("/tasks", json={"title": "New"}, headers={"Idempotency-Key": "new-key-0001"})
    keys = [r[0] for r in fa.sql("SELECT key FROM app_idempotency")]
    assert keys == ["new-key-0001"]


VOICE_JSON = {"title": "Memo", "body": "Buy bread", "tags": [], "tasks": [{"title": "Buy bread"}], "money": [],
              "money_only": False}


def test_voice_note_replay_with_form_key(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "buy bread"
    fa.CHAT_SCRIPT["groq"].append(json.dumps(VOICE_JSON))
    files = {"file": ("v.ogg", b"OggS-audio", "audio/ogg")}
    data = {"mode": "both", "idempotency_key": "voice-queued-0001"}
    first = app_hub.post("/notes/voice", files=files, data=data)
    assert first.status_code == 201, first.text
    second = app_hub.post("/notes/voice", files={"file": ("v.ogg", b"OggS-audio", "audio/ogg")}, data=data)
    assert second.status_code == 200
    a, b = first.json(), second.json()
    assert b["note"] == {"id": a["note"]["id"]} and b["tasks"] == [{"id": t["id"]} for t in a["tasks"]]
    assert b["audio_key"] == a["audio_key"] and b["transcript"] == "" and b["replayed"] is True
    assert len(fa.TRANSCRIBE_LOG) == 1  # transcribed once
    assert len(app_hub.get("/tasks").json()["tasks"]) == 1
    # The transcript and note body are not copied into the idempotency table.
    stored = " ".join(r[0] for r in fa.sql("SELECT response FROM app_idempotency"))
    assert "bread" not in stored.lower()


def test_money_voice_replay_with_header(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "taxi 20 dh"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": [{"type": "expense", "amount": 20, "currency": "MAD",
                                                           "merchant": "Taxi", "category": "transport"}]}))
    headers = {"Idempotency-Key": "money-voice-0001"}
    first = app_hub.post("/money/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")}, headers=headers)
    assert first.status_code == 201, first.text
    second = app_hub.post("/money/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")}, headers=headers)
    assert second.status_code == 200
    assert second.json()["expenses"] == [{"id": e["id"]} for e in first.json()["expenses"]]
    assert len(fa.TRANSCRIBE_LOG) == 1


def _pwa_create_fields() -> dict[str, set[str]]:
    """CREATE_FIELDS from the PWA's offline model: the fields it folds into a queued create."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "mobile_app/src/lib/offline/model.ts").read_text(encoding="utf-8")
    block = src[src.index("const CREATE_FIELDS"):]
    block = block[: block.index("};")]
    return {k: set(re.findall(r"'([a-z_]+)'", v)) for k, v in re.findall(r"(\w+): new Set\(\[([^\]]*)\]\)", block)}


def test_pwa_folds_only_fields_the_create_models_accept() -> None:
    from hub_api.routers.expenses import ExpenseCreate
    from hub_api.routers.money import IncomeCreate
    from hub_api.routers.notes import NoteCreate, TaskCreate

    models = {"task": TaskCreate, "note": NoteCreate, "expense": ExpenseCreate, "income": IncomeCreate}
    fields = _pwa_create_fields()
    assert set(fields) == set(models)
    for kind, model in models.items():
        assert fields[kind] == set(model.model_fields) - {"id"}, kind


def test_offline_task_ticked_before_sync_ends_done(app_hub) -> None:
    # What the queue sends for "create offline, tick it, then sync": the create (create fields only)
    # and the PATCH carrying `done`, which TaskCreate would silently drop.
    r = app_hub.post("/tasks", json={"id": "oOfflineTask001", "title": "Call the bank", "due": None, "due_time": None})
    assert r.status_code == 201, r.text
    assert app_hub.patch("/tasks/oOfflineTask001", json={"done": True}).status_code == 200
    done = {t["id"]: t for t in app_hub.get("/tasks?status=done").json()["tasks"]}
    assert done["oOfflineTask001"]["done"] is True
