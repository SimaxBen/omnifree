"""/notes/meeting: long meeting recordings -> transcript -> summary note + action-item tasks.

A meeting is recorded in the PWA as a series of self-contained audio files of about 10 minutes (the recorder is
restarted every 10 minutes, so each part has its own container header and can be transcribed alone: byte ranges
of a compressed file cannot be), each one sent straight to the Drive through ``POST /drive/upload-url`` (B2/R2
presigned PUT), which avoids the ~4.5 MB serverless request limit.

* ``POST /notes/meeting`` ``{"meeting_id"?, "audio_keys": [{"key", "provider"?}], "final": true, "title"?,
  "language"?, "notebook"?}`` (``audio_key`` for a single file): creates the meeting (or adds parts to it), then
  transcribes the parts not done yet, one after the other, while the call's time budget lasts (a part over the
  25 MB Whisper limit is skipped). It is resumable: the answer says how many parts remain and the PWA calls again
  with the same ``meeting_id`` until ``status`` is ``done``. Once every part is transcribed and ``final`` is true, ONE
  LLM call writes the title, summary, decisions and action items; the note goes to the notebook "Meetings" (the
  full transcript in a collapsed ``:::details`` section) and each action item becomes a task (owner and due date
  kept). Calls with ``final: false`` only transcribe (the PWA sends the parts while the meeting is still running).
* ``GET /notes/meeting/{meeting_id}``: the same status, without doing any work.

Every call stays inside the 60 s serverless limit: it transcribes one part, then more only while less than
``START_BEFORE_S`` have passed; each transcription is bounded, and the summary runs in the next call when too little
time is left. A lease
(``locked_until``) keeps two calls from working on the same meeting at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.interfaces import ChatMessage, ChatRequest, TranscriptionRequest
from hub_api import gcal
from hub_api.appdata import AppDB, extract_json, get_appdb, iso, local_now, new_id, now_iso
from hub_api.routers import notes as notes_router
from hub_api.routers.drive import normalise_key
from hub_api.state import HubState, get_state

__all__ = ["router", "SCHEMA", "meeting_body", "parse_summary", "clip_transcript"]

log = logging.getLogger("omnifree.hub.meetings")

router = APIRouter(tags=["notes"])

NOTEBOOK = "Meetings"
MAX_PARTS = 36  # 6 hours of 10-minute parts
WHISPER_MAX_BYTES = 25 * 1024 * 1024  # Groq Whisper limit per call
START_BEFORE_S = 24.0  # a new part is only started this early in the call...
TRANSCRIBE_TIMEOUT_S = 24.0  # ...and each one (download + transcription) is bounded
SUMMARY_BEFORE_S = 25.0  # the summary only starts this early, else the next call does it
LLM_TIMEOUT_S = 22.0
TASK_PUSH_BEFORE_S = 48.0  # Google Calendar pushes for the new tasks stop here (the hourly reconcile catches up)
LEASE_S = 58
MAX_ATTEMPTS = 2  # per part
MAX_TRANSCRIPT_CHARS = 48_000  # sent to the LLM (head and tail kept when longer)
MAX_ACTIONS = 20

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_meetings ("
    " id TEXT PRIMARY KEY, title TEXT, language TEXT, notebook TEXT NOT NULL DEFAULT 'Meetings',"
    " status TEXT NOT NULL, parts TEXT NOT NULL DEFAULT '[]', final INTEGER NOT NULL DEFAULT 0,"
    " note_id TEXT, task_ids TEXT NOT NULL DEFAULT '[]', summary TEXT, error TEXT, locked_until TEXT,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
)


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await notes_router.ensure_schema(db)
    await db.ensure("meetings", SCHEMA)
    return db


# --------------------------------------------------------------------------- pure helpers


def clip_transcript(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The transcript for the LLM: whole when short enough, else its head and tail around a marker."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half].rstrip()}\n\n[… {len(text) - 2 * half} characters of the middle left out …]\n\n{text[-half:].lstrip()}"


def _strings(value: Any, limit: int = 20, width: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(v).strip()[:width] for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return out[:limit]


def parse_summary(data: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Validate the LLM's meeting summary; a usable fallback (no summary, no tasks) when it is missing."""
    day = now.date()
    fallback_title = f"Meeting {day.isoformat()}"
    if not isinstance(data, dict):
        return {"title": fallback_title, "summary": "", "decisions": [], "actions": [], "tags": [], "llm": False}
    title = data.get("title")
    title = title.strip()[:120] if isinstance(title, str) and title.strip() else fallback_title
    summary = data.get("summary")
    if isinstance(summary, list):
        summary = "\n".join(f"- {s}" for s in _strings(summary))
    summary = summary.strip() if isinstance(summary, str) else ""
    actions: list[dict[str, Any]] = []
    raw = data.get("action_items") or data.get("actions") or []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"task": item}
        if not isinstance(item, dict):
            continue
        text = item.get("task") or item.get("title") or item.get("action")
        if not isinstance(text, str) or not text.strip():
            continue
        owner = item.get("owner")
        owner = owner.strip()[:80] if isinstance(owner, str) and owner.strip() and owner.strip().lower() not in (
            "null", "none", "unknown", "n/a", "-") else None
        due, due_time = notes_router.resolve_when(item.get("due"), item.get("due_time"), now)
        prio = str(item.get("priority") or "normal").strip().lower()
        actions.append({
            "title": text.strip()[:300], "owner": owner, "due": due, "due_time": due_time,
            "priority": prio if prio in ("low", "normal", "high") else "normal",
        })
        if len(actions) >= MAX_ACTIONS:
            break
    tags = [t for t in (str(x).strip().lstrip("#").lower()[:40] for x in _strings(data.get("tags"), 8)) if t]
    decisions = _strings(data.get("decisions"))
    return {"title": title, "summary": summary, "decisions": decisions, "actions": actions, "tags": tags,
            "llm": bool(summary or decisions or actions)}  # an empty answer ("{}") is no summary


def _minutes(parts: list[dict[str, Any]]) -> int | None:
    secs = sum(float(p.get("seconds") or 0) for p in parts)
    return round(secs / 60) if secs else None


def meeting_body(result: dict[str, Any], transcript: str, parts: list[dict[str, Any]], day: date) -> str:
    """The note's Markdown: summary, decisions, action items, then the transcript in a collapsed section."""
    lines: list[str] = []
    mins = _minutes(parts)
    meta = f"Meeting of {day.isoformat()}" + (f", about {mins} min" if mins else "") + f", {len(parts)} part" + (
        "s" if len(parts) != 1 else "")
    lines += [f"*{meta}*", ""]
    if result["summary"]:
        lines += ["## Summary", "", result["summary"], ""]
    if result["decisions"]:
        lines += ["## Decisions", ""] + [f"- {d}" for d in result["decisions"]] + [""]
    if result["actions"]:
        lines += ["## Action items", ""]
        for a in result["actions"]:
            bits = [b for b in (a["owner"] and f"**{a['owner']}**",
                                a["due"] and f"due {a['due']}{' ' + a['due_time'] if a['due_time'] else ''}") if b]
            lines.append(f"- [ ] {a['title']}" + (f" ({', '.join(bits)})" if bits else ""))
        lines.append("")
    failed = [i + 1 for i, p in enumerate(parts) if p.get("status") == "failed"]
    if failed:
        lines += [f"> Part{'s' if len(failed) > 1 else ''} {', '.join(map(str, failed))} could not be transcribed.", ""]
    # A line that starts with ":::" would end the collapsed section early.
    safe = "\n".join(("::" + ln[3:]) if ln.lstrip().startswith(":::") else ln for ln in transcript.splitlines())
    lines += ["## Transcript", "", ":::details Full transcript", safe or "(nothing was heard)", ":::"]
    return "\n".join(lines).strip() + "\n"


def _prompt(now: datetime) -> str:
    today = now.date()
    week = ", ".join(f"{(today + timedelta(days=i)):%A} = {(today + timedelta(days=i)).isoformat()}" for i in range(1, 8))
    return (
        "You summarise a meeting transcript (it may mix English, French and Moroccan Darija).\n"
        f"Today is {today:%A} {today.isoformat()} (Africa/Casablanca). Upcoming days: {week}.\n"
        "Answer with ONE JSON object only, no prose, no code fences:\n"
        '{"title": "short meeting title (max 8 words)", "summary": "5-10 lines of markdown bullets, the transcript\'s '
        'language", "decisions": ["each decision taken"], "action_items": [{"task": "imperative action", "owner": '
        '"who does it, or null", "due": "YYYY-MM-DD or the words used, or null", "due_time": "HH:MM or null", '
        '"priority": "low|normal|high"}], "tags": ["lowercase", "keywords"]}\n'
        "Rules: nothing invented; decisions and action items only when actually said (else empty lists); convert "
        "relative dates to YYYY-MM-DD with today's date."
    )


# --------------------------------------------------------------------------- storage


def _loads(value: Any) -> list[Any]:
    try:
        out = json.loads(value) if isinstance(value, str) else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _status_out(row: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    parts = _loads(row.get("parts"))
    done = sum(1 for p in parts if p.get("status") in ("done", "failed"))
    return {
        "meeting_id": row["id"],
        "status": row["status"],
        "final": bool(row.get("final")),
        "parts_total": len(parts),
        "parts_done": done,
        "parts_failed": sum(1 for p in parts if p.get("status") == "failed"),
        "remaining": len(parts) - done,
        "note_id": row.get("note_id"),
        "task_ids": _loads(row.get("task_ids")),
        "error": row.get("error"),
        **(extra or {}),
    }


async def _get(db: AppDB, meeting_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_meetings WHERE id = ?", [meeting_id])
    if row is None:
        raise HTTPException(404, detail="meeting not found")
    return row


async def _save(db: AppDB, meeting_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE app_meetings SET {sets} WHERE id = ?", [*fields.values(), meeting_id])


async def _lease(db: AppDB, meeting_id: str) -> bool:
    now = datetime.now(timezone.utc)
    taken = await db.execute(
        "UPDATE app_meetings SET locked_until = ? WHERE id = ? AND (locked_until IS NULL OR locked_until < ?)",
        [iso(now + timedelta(seconds=LEASE_S)), meeting_id, iso(now)],
    )
    return taken > 0


# --------------------------------------------------------------------------- work


async def _transcribe_part(state: HubState, part: dict[str, Any], language: str | None) -> str:
    key = part["key"]
    provider = part.get("provider")
    if not provider:
        obj = await state.router.storage_locate(key)
        if obj is None:
            raise FileNotFoundError(f"{key} is not on any storage backend")
        provider = obj.backend
        part["provider"] = provider
    stat = await state.router.storage_stat(key, provider)
    if stat is not None and stat.size and stat.size > WHISPER_MAX_BYTES:
        raise ValueError(f"part is {stat.size} bytes, over the 25 MB transcription limit")
    audio = await state.router.storage_get(key, provider)
    if len(audio) > WHISPER_MAX_BYTES:
        raise ValueError(f"part is {len(audio)} bytes, over the 25 MB transcription limit")
    if not audio:
        return ""
    name = key.rsplit("/", 1)[-1] or "part.webm"
    tr = await state.router.transcribe(TranscriptionRequest(audio=audio, filename=name, language=language or None))
    return (tr.text or "").strip()


async def _summarise(state: HubState, transcript: str, now: datetime) -> dict[str, Any]:
    if not transcript.strip():
        return parse_summary(None, now)
    request = ChatRequest(
        messages=[ChatMessage(role="system", content=_prompt(now)),
                  ChatMessage(role="user", content=f"Transcript:\n{clip_transcript(transcript)}")],
        temperature=0.2,
        max_tokens=2000,
        response_format={"type": "json_object"},
    )
    try:
        resp = await asyncio.wait_for(state.router.chat(request), LLM_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - ProviderError, timeout: keep the transcript, no summary
        log.warning("meeting summary failed, saving the transcript only: %r", exc)
        return parse_summary(None, now)
    return parse_summary(extract_json(resp.content), now)


async def _finish(state: HubState, db: AppDB, row: dict[str, Any], parts: list[dict[str, Any]],
                  started: float) -> dict[str, Any]:
    now = local_now()
    transcript = "\n\n".join(p.get("text") or "" for p in parts if p.get("text")).strip()
    result = await _summarise(state, transcript, now)
    if row.get("title"):
        result["title"] = str(row["title"])[:120]
    body = meeting_body(result, transcript, parts, now.date())
    note = await notes_router._insert_note(
        db, body=body, title=result["title"], notebook=row.get("notebook") or NOTEBOOK,
        tags=["meeting", *result["tags"]], pinned=False, source="meeting", audio_key=parts[0]["key"] if parts else None,
    )
    tasks: list[dict[str, Any]] = []
    for a in result["actions"]:
        details = "\n".join(b for b in (a["owner"] and f"Owner: {a['owner']}", f"From the meeting: {result['title']}") if b)
        task = await notes_router._insert_task(
            db, title=a["title"], details=details, due=a["due"], due_time=a["due_time"], priority=a["priority"],
            note_id=note["id"],
        )
        if _time.monotonic() - started < TASK_PUSH_BEFORE_S:
            await gcal.push_task(state, db, task["id"], task, created=True)  # bounded (~4 s), best effort
        tasks.append(task)
    await _save(db, row["id"], status="done", note_id=note["id"], task_ids=json.dumps([t["id"] for t in tasks]),
                summary=json.dumps({k: result[k] for k in ("summary", "decisions")}, ensure_ascii=False),
                error=None if result["llm"] or not transcript else "the summary could not be written; transcript saved")
    return {"note": note, "tasks": tasks, "summary": result["summary"], "decisions": result["decisions"]}


async def _work(state: HubState, db: AppDB, meeting_id: str, started: float) -> dict[str, Any]:
    row = await _get(db, meeting_id)
    parts = _loads(row.get("parts"))
    language = row.get("language")
    worked = 0
    for part in parts:
        if part.get("status") in ("done", "failed"):
            continue
        if worked and _time.monotonic() - started > START_BEFORE_S:
            break  # at least one part per call, then only while the budget lasts
        worked += 1
        try:
            part["text"] = await asyncio.wait_for(_transcribe_part(state, part, language), TRANSCRIBE_TIMEOUT_S)
            part["status"] = "done"
            part.pop("error", None)
        except Exception as exc:  # noqa: BLE001 - per part: retried once in a later call, then skipped
            part["attempts"] = int(part.get("attempts") or 0) + 1
            part["error"] = ("timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__)[:300]
            permanent = isinstance(exc, (ValueError, FileNotFoundError))
            part["status"] = "failed" if permanent or part["attempts"] >= MAX_ATTEMPTS else "pending"
            log.warning("meeting %s part %s: %s", meeting_id, part["key"], part["error"])
        await _save(db, meeting_id, parts=json.dumps(parts, ensure_ascii=False))
    pending = [p for p in parts if p.get("status") not in ("done", "failed")]
    extra: dict[str, Any] = {}
    if pending:
        status = "transcribing"
    elif not row.get("final"):
        status = "waiting"  # every part sent so far is transcribed; more may come
    elif worked and _time.monotonic() - started > SUMMARY_BEFORE_S:
        status = "summarizing"  # the next call (with its whole budget) writes the note
    else:
        await _save(db, meeting_id, status="summarizing")
        extra = await _finish(state, db, row, parts, started)
        status = "done"
    if status != "done":
        await _save(db, meeting_id, status=status)
    return _status_out(await _get(db, meeting_id), extra)


# --------------------------------------------------------------------------- routes


class AudioPart(BaseModel):
    key: str = Field(min_length=1, max_length=1024)
    provider: str | None = Field(None, max_length=100)
    seconds: float | None = Field(None, ge=0, le=6 * 3600)


class MeetingRequest(BaseModel):
    meeting_id: str | None = Field(None, max_length=40)
    audio_keys: list[AudioPart | str] = Field(default_factory=list, max_length=MAX_PARTS)
    audio_key: str | None = Field(None, max_length=1024)
    final: bool = True
    title: str | None = Field(None, max_length=120)
    language: str | None = Field(None, max_length=10)
    notebook: str | None = Field(None, max_length=100)


def _parts_in(body: MeetingRequest) -> list[dict[str, Any]]:
    raw: list[AudioPart] = [AudioPart(key=p) if isinstance(p, str) else p for p in body.audio_keys]
    if body.audio_key:
        raw.append(AudioPart(key=body.audio_key))
    return [{"key": normalise_key(p.key), "provider": p.provider or None, "seconds": p.seconds, "status": "pending"}
            for p in raw]


@router.post("/notes/meeting")
async def meeting(body: MeetingRequest, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    started = _time.monotonic()
    incoming = _parts_in(body)
    if body.meeting_id:
        row = await _get(db, body.meeting_id)
        meeting_id = row["id"]
        if row["status"] == "done":
            note = await db.fetch_one("SELECT * FROM app_notes WHERE id = ?", [row.get("note_id") or ""])
            return _status_out(row, {"note": notes_router._note_out(note) if note else None})
        parts = _loads(row.get("parts"))
        known = {p["key"] for p in parts}
        added = [p for p in incoming if p["key"] not in known]
        if len(parts) + len(added) > MAX_PARTS:
            raise HTTPException(422, detail=f"a meeting has at most {MAX_PARTS} parts")
        changes: dict[str, Any] = {}
        if added:
            changes["parts"] = json.dumps(parts + added, ensure_ascii=False)
        if body.final and not row.get("final"):
            changes["final"] = 1
        for field in ("title", "language", "notebook"):
            value = getattr(body, field)
            if value and value.strip():
                changes[field] = value.strip()
        if changes:
            await _save(db, meeting_id, **changes)
    else:
        if not incoming:
            raise HTTPException(422, detail="send audio_keys (the uploaded meeting parts) or audio_key")
        parts = list({p["key"]: p for p in reversed(incoming)}.values())[::-1]  # first occurrence of each key
        meeting_id = new_id()
        stamp = now_iso()
        await db.execute(
            "INSERT INTO app_meetings (id, title, language, notebook, status, parts, final, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'transcribing', ?, ?, ?, ?)",
            [meeting_id, (body.title or "").strip() or None, (body.language or "").strip() or None,
             (body.notebook or "").strip() or NOTEBOOK, json.dumps(parts, ensure_ascii=False), 1 if body.final else 0,
             stamp, stamp],
        )
    if not await _lease(db, meeting_id):
        return _status_out(await _get(db, meeting_id), {"busy": True})
    try:
        return await _work(state, db, meeting_id, started)
    finally:
        await db.execute("UPDATE app_meetings SET locked_until = NULL WHERE id = ?", [meeting_id])


@router.get("/notes/meeting/{meeting_id}")
async def meeting_status(meeting_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _status_out(await _get(db, meeting_id))
