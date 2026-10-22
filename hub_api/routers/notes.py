"""/notes, /tasks: markdown notes in notebooks, tasks, and voice notes (audio -> transcript -> LLM -> note/tasks).

Data lives in the apps database (D1, see :mod:`hub_api.appdata`). Voice notes keep the audio in
the Drive under ``voice/<yyyy>/<mm>/<id>.<ext>``; relative due dates ("tomorrow", "friday",
"demain", "ghda") are resolved against today in Africa/Casablanca.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from core.interfaces import ChatMessage, ChatRequest, ProviderError, TranscriptionRequest
from hub_api import gcal, idempotency
from hub_api.appdata import AppDB, extract_json, get_appdb, local_now, local_today, new_id, now_iso, read_upload
from hub_api.state import HubState, get_state

__all__ = [
    "router", "resolve_due", "resolve_when", "split_time", "normalize_time", "derive_title", "SCHEMA",
    "TASK_COLUMNS", "ensure_schema", "store_and_transcribe",
]

log = logging.getLogger("omnifree.hub.notes")

router = APIRouter(tags=["notes"])

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_notes ("
    " id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',"
    " notebook TEXT NOT NULL DEFAULT 'Inbox', tags TEXT NOT NULL DEFAULT '[]',"
    " source TEXT NOT NULL DEFAULT 'text', audio_key TEXT, pinned INTEGER NOT NULL DEFAULT 0,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_notes_notebook ON app_notes (notebook)",
    "CREATE TABLE IF NOT EXISTS app_tasks ("
    " id TEXT PRIMARY KEY, title TEXT NOT NULL, details TEXT, due TEXT,"
    " done INTEGER NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal', note_id TEXT,"
    " created_at TEXT NOT NULL, done_at TEXT)",
    "CREATE INDEX IF NOT EXISTS app_tasks_open ON app_tasks (done, due)",
)
# Added after the first release: ALTER TABLE ... ADD COLUMN when missing (see AppDB.add_columns).
TASK_COLUMNS = (("due_time", "TEXT"),)  # HH:MM, Africa/Casablanca; only set together with ``due``

Priority = Literal["low", "normal", "high"]
_PRIORITIES = ("low", "normal", "high")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def ensure_schema(db: AppDB) -> AppDB:
    """The notes/tasks tables, plus the columns added later (``due_time``)."""
    await db.ensure("notes", SCHEMA)
    await db.add_columns("notes_v2_due_time", "app_tasks", TASK_COLUMNS)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- helpers


def derive_title(body: str, limit: int = 80) -> str:
    for line in body.splitlines():
        text = re.sub(r"^(?:\s*(?:#+|[-*+]|\d+[.)]|>|\[[ xX]\])\s*)+", "", line).strip()
        text = re.sub(r"[*_`~]+", "", text).strip()
        if text:
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return "Untitled"


def _clean_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [t for t in re.split(r"[,\s]+", raw) if t]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for t in raw:
        tag = str(t).strip().lstrip("#").strip().lower()[:40]
        if tag and tag not in out:
            out.append(tag)
    return out[:20]


_WEEKDAYS: dict[str, int] = {}
for _idx, _names in enumerate(
    (
        ("monday", "lundi", "tnin", "letnin", "lethnin", "l'tnin", "itnin"),
        ("tuesday", "mardi", "tlat", "tlata", "ttlat", "ttlata"),
        ("wednesday", "mercredi", "larb3", "larbe3", "larbaa", "arbe3"),
        ("thursday", "jeudi", "lkhmis", "khmis", "lkhamis", "khamis"),
        ("friday", "vendredi", "jem3a", "jm3a", "jemaa", "jum3a", "jumu3a", "jmaa"),
        ("saturday", "samedi", "sebt", "sabt", "ssebt"),
        ("sunday", "dimanche", "l7ad", "had", "l7edd", "lhad"),
    )
):
    for _n in _names:
        _WEEKDAYS[_n] = _idx

_OFFSETS = {
    "today": 0, "aujourd'hui": 0, "aujourdhui": 0, "auj": 0, "lyoum": 0, "lyom": 0, "lyouma": 0, "elyoum": 0,
    "tonight": 0, "ce soir": 0,
    "tomorrow": 1, "demain": 1, "ghda": 1, "ghadda": 1, "ghedda": 1, "ghdda": 1, "bukra": 1,
    "day after tomorrow": 2, "the day after tomorrow": 2, "after tomorrow": 2, "apres-demain": 2,
    "apres demain": 2, "après-demain": 2, "après demain": 2, "b3d ghda": 2, "ba3d ghda": 2, "be3d ghda": 2,
    "next week": 7, "la semaine prochaine": 7, "semaine prochaine": 7, "simana jaya": 7, "ssimana jaya": 7,
}
_IN_DAYS_RE = re.compile(r"^(?:in|dans|f|men daba|mn daba)\s+(\d{1,3})\s*(?:days?|jours?|ayam|iyam|nhar)$")
_PREFIX_RE = re.compile(r"^(?:next|this|on|ce|cette|prochain|nhar|l'|le)\s+")
_SUFFIX_RE = re.compile(r"\s+(?:prochain|prochaine|jay|jaya|next)$")


def _relative(text: str, today: date) -> date | None:
    t = text.strip().lower().rstrip(".!")
    if t in _OFFSETS:
        return today + timedelta(days=_OFFSETS[t])
    m = _IN_DAYS_RE.match(t)
    if m:
        return today + timedelta(days=int(m.group(1)))
    t = _SUFFIX_RE.sub("", _PREFIX_RE.sub("", t)).strip()
    if t in _OFFSETS:
        return today + timedelta(days=_OFFSETS[t])
    if t in _WEEKDAYS:
        ahead = (_WEEKDAYS[t] - today.weekday()) % 7 or 7  # the next such day, never today
        return today + timedelta(days=ahead)
    return None


def resolve_due(value: Any, today: date) -> str | None:
    """Validate an LLM/user due date: ``YYYY-MM-DD`` or a relative expression, else None.

    Dates more than a year in the past or ten years ahead are treated as hallucinations.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "-"):
        return None
    if len(text) >= 10 and _DATE_RE.match(text[:10]):
        try:
            d = date.fromisoformat(text[:10])
        except ValueError:
            return None
    else:
        d = _relative(text, today)
        if d is None:  # "demain à 15h", "vendredi soir": the day part only
            rest, hhmm, says_today, _ = split_time(text)
            if rest:
                d = _relative(rest, today)
            elif hhmm and says_today:
                d = today
        if d is None:
            return None
    if not (today - timedelta(days=366) <= d <= today + timedelta(days=3650)):
        return None
    return d.isoformat()


# --------------------------------------------------------------------------- times of day
#
# "demain à 15h", "à 18h30", "ce soir", "vendredi 10h", "tomorrow at 3pm", "ghda m3a 3", "f 5 dyal l3chiya".
# A bare hour from 1 to 7 with no morning word means the afternoon/evening (nobody plans "à 3h" at night);
# a zero-padded "07:00" is taken literally.

_PERIODS: list[tuple[str, str, bool]] = []  # (phrase, kind, says_today), longest phrase first
for _kind, _today_words, _words in (
    ("morning", ("ce matin", "this morning", "had sbah", "lyoum sbah"),
     ("du matin", "le matin", "au matin", "matin", "in the morning", "morning", "f sbah", "fs sbah", "f ssbah",
      "fe sbah", "dyal sbah", "dyal sba7", "sbah", "ssbah", "sba7", "ssba7", "sbe7", "fsba7")),
    ("noon", (), ("midi", "noon", "midday", "f nss nhar", "nss nhar", "nos nhar", "f nos nhar", "f ness nhar", "ness nhar")),
    ("afternoon", ("cet apres-midi", "cet après-midi", "cet aprem", "this afternoon", "had l3chiya", "had l3chia"),
     ("de l'apres-midi", "de l'après-midi", "l'apres-midi", "l'après-midi", "apres-midi", "après-midi",
      "apres midi", "après midi", "aprem", "in the afternoon", "afternoon", "f l3chiya", "fl3chiya", "dyal l3chiya",
      "l3chiya", "l3chia", "la3chiya", "f l3achiya", "l3achiya", "dyal l3chia", "f l3chia", "3chiya")),
    ("evening", ("ce soir", "tonight", "this evening", "had lila", "had lil", "lyoum f lil"),
     ("du soir", "le soir", "au soir", "soir", "in the evening", "evening", "at night", "f lil", "fl lil", "f llil",
      "dyal lil", "dyal llil", "fellil", "flil", "lil", "llil", "f lmghrib", "lmghrib", "mghrib")),
    ("night", ("cette nuit",), ("de la nuit", "la nuit", "nuit", "night")),
    ("midnight", (), ("minuit", "midnight", "nss lil", "f nss lil", "nos lil")),
):
    for _w in _today_words:
        _PERIODS.append((_w, _kind, True))
    for _w in _words:
        _PERIODS.append((_w, _kind, False))
_PERIODS.sort(key=lambda p: -len(p[0]))
_PERIOD_DEFAULT = {"morning": (9, 0), "noon": (12, 0), "afternoon": (15, 0), "evening": (20, 0), "night": (22, 0),
                   "midnight": (0, 0)}
_PM_PERIODS = ("afternoon", "evening", "night")

_CONNECT = r"(?:à|a|at|@|vers|around|about|pour|for|by|avant|before|m3a|mea|ma3a|m3|f|fi|fel|l)"
_GLUE = r"(?:à|a|at|@|vers|around|about|pour|for|by|avant|before|m3a|mea|ma3a|m3|f|fi|fel|l|on|le|la|dyal|de|du|et|w|and)"
_HALF = r"(?:et\s+demie?|w\s*(?:nss|nos|nass|ness|nesf)|and\s+a\s+half)"
_QUARTER = r"(?:et\s+quart|w\s*(?:rb3|rbe3|rob3|rbo3)|quarter\s+past)"
_TO_QUARTER = r"(?:moins\s+(?:le\s+)?quart|(?:qell|9ell|qal|ghir)\s+(?:rb3|rbe3|rob3|rbo3)|quarter\s+to)"
_FRACTION = rf"(?:\s+(?P<frac>{_HALF}|{_QUARTER}|{_TO_QUARTER}))?"
_NOT_UNIT = r"(?!\s*(?:jours?|days?|nhar|ayam|iyam|semaines?|weeks?|mois|months?|ans?|years?|dh|mad|dirhams?|%|€|\$|\d|[/.-]\d))"
_TIME_RES = (
    # 15:00, 18h30, 18 h 30, 9.15 is not a time (too ambiguous)
    re.compile(rf"(?:\b{_CONNECT}\s+)?\b(?P<h>\d{{1,2}})\s*(?::|h)\s*(?P<m>\d{{2}})\b(?:\s*(?P<ampm>am|pm|a\.m\.|p\.m\.))?"),
    # 3pm, 3 p.m.
    re.compile(rf"(?:\b{_CONNECT}\s+)?\b(?P<h>\d{{1,2}})\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)(?![a-z])"),
    # 15h, 3 heures, 3 o'clock, 3 sa3a, optionally "et demie"
    re.compile(rf"(?:\b{_CONNECT}\s+)?\b(?P<h>\d{{1,2}})\s*(?:h|heures?|hours?|o'?clock|sa3a|s3a|sa3at)(?![a-z0-9]){_FRACTION}"),
    # "à 3", "m3a 3", "f 5", "at 7" (a connector is required for a bare number)
    re.compile(rf"\b{_CONNECT}\s+(?P<h>\d{{1,2}}){_NOT_UNIT}\b{_FRACTION}"),
)
_IN_HOURS_RE = re.compile(
    r"\b(?:in|dans|men\s+daba|mn\s+daba|ba3d|be3d|after)\s+(?:(?P<n>\d{1,3})\s*|(?P<one>une?|one|an|a)\s+)"
    r"(?P<unit>heures?|hours?|h|minutes?|mins?|min|sa3a|sa3at|d9i9a|d9aye9|dqiqa)\b"
)
_LEFTOVER_RE = re.compile(rf"^(?:{_GLUE}(?:\s+|$))+|(?:\s+{_GLUE})+$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)(?::[0-5]\d)?$")


def _norm(text: str) -> str:
    t = text.lower().replace("\u2019", "'").replace("\u2011", "-").replace("\u00a0", " ")
    t = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", t)
    t = re.sub(r"[!?;]+", " ", t)
    t = re.sub(r"(?<!\d)[.,](?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _cut(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", f"{text[:start]} {text[end:]}").strip()


def split_time(text: str, now: datetime | None = None) -> tuple[str, str | None, bool, date | None]:
    """Pull a time of day out of a date/time phrase.

    Returns ``(rest, "HH:MM" or None, says_today, absolute_day)``: ``rest`` is the phrase without the
    time words ("demain à 15h" -> "demain"); ``says_today`` is True for "ce soir", "tonight", "this
    morning"; ``absolute_day`` is set for relative durations ("dans 2 heures", needs ``now``).
    """
    t = _norm(text)
    if now is not None:
        m = _IN_HOURS_RE.search(t)
        if m:
            n = int(m.group("n")) if m.group("n") else 1
            unit = m.group("unit")
            delta = timedelta(hours=n) if unit[0] in "hs" else timedelta(minutes=n)
            if delta <= timedelta(days=7):
                at = now + delta
                return _cut(t, m.start(), m.end()), f"{at:%H:%M}", False, at.date()
    period: str | None = None
    says_today = False
    for phrase, kind, today_word in _PERIODS:
        m = re.search(rf"(?<![\w']){re.escape(phrase)}(?![\w'])", t)
        if m:
            period, says_today = kind, today_word
            t = _cut(t, m.start(), m.end())
            break
    hour: int | None = None
    minute = 0
    for rx in _TIME_RES:
        m = rx.search(t)
        if not m:
            continue
        h_txt = m.group("h")
        h, minute = int(h_txt), int(m.groupdict().get("m") or 0)
        ampm = (m.groupdict().get("ampm") or "").replace(".", "")
        frac = m.groupdict().get("frac") or ""
        if frac:
            if re.fullmatch(_HALF, frac):
                minute = 30
            elif re.fullmatch(_QUARTER, frac):
                minute = 15
            else:
                h, minute = h - 1, 45
        if h > 24 or minute > 59:
            continue
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        elif not ampm and 1 <= h <= 11:
            if period in _PM_PERIODS and not (period == "night" and h <= 4):
                h += 12
            elif period is None and h <= 7 and not h_txt.startswith("0"):
                h += 12  # "à 3h" = 15:00
        hour = h % 24
        t = _cut(t, m.start(), m.end())
        break
    if hour is None and period is not None:
        hour, minute = _PERIOD_DEFAULT[period]
        m = re.match(rf"^(?:{_HALF}|{_QUARTER})(?:\s+|$)", t)
        if m:  # "midi et demie"
            minute = 30 if re.match(_HALF, m.group(0)) else 15
            t = t[m.end():].strip()
    if hour is None and re.search(r"(?<![\w'])(?:midi|noon)(?![\w'])", t):
        hour, minute = 12, 0
    rest = _LEFTOVER_RE.sub("", t).strip()
    rest = _LEFTOVER_RE.sub("", rest).strip()
    return rest, (f"{hour:02d}:{minute:02d}" if hour is not None else None), says_today, None


def normalize_time(value: Any) -> str | None:
    """``HH:MM`` from "15:00", "15h", "9h30", "3pm", "ce soir"... else None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "-"):
        return None
    m = TIME_RE.match(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    rest, hhmm, _, _ = split_time(text)
    return hhmm if hhmm and not rest else None


def resolve_when(due: Any, due_time: Any, now: datetime) -> tuple[str | None, str | None]:
    """A task's ``(due, due_time)`` from an LLM/user date and time (either may hold a whole phrase).

    ``now`` is the local (Africa/Casablanca) time. A time with no day means today, or tomorrow when that
    time has already passed ("à 18h30" said at 19:00). "ce soir" / "tonight" always mean today.
    """
    today = now.date()
    day: str | None = None
    hhmm = normalize_time(due_time)
    says_today = False
    text = "" if due is None else str(due).strip()
    if text and text.lower() not in ("null", "none", "n/a", "-"):
        iso = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2})(?::\d{2})?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$", text)
        if iso:
            day = resolve_due(iso.group(1), today)
            if iso.group(2) and not iso.group(3):
                hhmm = hhmm or normalize_time(iso.group(2))
        else:
            rest, found, says_today, absolute = split_time(text, now)
            hhmm = hhmm or found
            if found is None:  # a day only: "demain", "f 3 ayam"
                day = resolve_due(text, today)
                if day is None:
                    return None, None
            elif absolute is not None:
                day = absolute.isoformat()
            elif rest:
                day = resolve_due(rest, today)
                if day is None:
                    return None, None  # words we do not understand: no guess at all
    if day is None and hhmm is not None:
        if says_today:
            day = today.isoformat()
        else:
            h, m = (int(x) for x in hhmm.split(":"))
            passed = (h, m) <= (now.hour, now.minute)
            day = (today + timedelta(days=1 if passed else 0)).isoformat()
    return day, (hhmm if day else None)


def _note_out(row: dict[str, Any]) -> dict[str, Any]:
    try:
        tags = json.loads(row.get("tags") or "[]")
    except ValueError:
        tags = []
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row.get("body") or "",
        "notebook": row.get("notebook") or "Inbox",
        "tags": tags if isinstance(tags, list) else [],
        "source": row.get("source") or "text",
        "audio_key": row.get("audio_key"),
        "pinned": bool(row.get("pinned")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _task_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "details": row.get("details"),
        "due": row.get("due"),
        "due_time": row.get("due_time") if row.get("due") else None,
        "done": bool(row.get("done")),
        "priority": row.get("priority") or "normal",
        "note_id": row.get("note_id"),
        "created_at": row["created_at"],
        "done_at": row.get("done_at"),
    }


async def _get_note(db: AppDB, note_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_notes WHERE id = ?", [note_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="note not found")
    return row


async def _get_task(db: AppDB, task_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_tasks WHERE id = ?", [task_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="task not found")
    return row


async def _insert_note(
    db: AppDB,
    *,
    body: str,
    title: str | None,
    notebook: str | None,
    tags: list[str],
    pinned: bool,
    source: str = "text",
    audio_key: str | None = None,
    row_id: str | None = None,
) -> dict[str, Any]:
    now = now_iso()
    row = {
        "id": row_id or new_id(),
        "title": (title or "").strip() or derive_title(body),
        "body": body,
        "notebook": (notebook or "").strip() or "Inbox",
        "tags": json.dumps(_clean_tags(tags)),
        "source": source,
        "audio_key": audio_key,
        "pinned": 1 if pinned else 0,
        "created_at": now,
        "updated_at": now,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_notes ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _note_out(row)


async def _insert_task(
    db: AppDB,
    *,
    title: str,
    details: str | None = None,
    due: str | None = None,
    due_time: str | None = None,
    priority: str = "normal",
    note_id: str | None = None,
    row_id: str | None = None,
) -> dict[str, Any]:
    row = {
        "id": row_id or new_id(),
        "title": title.strip(),
        "details": details or None,
        "due": due,
        "due_time": due_time if due else None,
        "done": 0,
        "priority": priority if priority in _PRIORITIES else "normal",
        "note_id": note_id,
        "created_at": now_iso(),
        "done_at": None,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_tasks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _task_out(row)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _due_param(value: str | None) -> str | None:
    if value is None:
        return None
    if not _DATE_RE.match(value):
        raise HTTPException(422, detail="due must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(422, detail="due must be a valid date") from exc


def _time_param(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    m = TIME_RE.match(value.strip())
    if not m:
        raise HTTPException(422, detail="due_time must be HH:MM (24 h, Africa/Casablanca)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# --------------------------------------------------------------------------- notes


class NoteCreate(BaseModel):
    id: str | None = Field(None, description="client-generated id (offline queue); a replay answers the existing note")
    title: str | None = Field(None, max_length=300)
    body: str = Field("", max_length=1_000_000)
    notebook: str | None = Field(None, max_length=100)
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False


class NotePatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    body: str | None = Field(None, max_length=1_000_000)
    notebook: str | None = Field(None, max_length=100)
    tags: list[str] | None = None
    pinned: bool | None = None
    source: Literal["text", "voice"] | None = None
    audio_key: str | None = None


@router.get("/notes")
async def list_notes(
    notebook: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if notebook:
        where.append("notebook = ?")
        params.append(notebook)
    if q and q.strip():
        like = _like(q.strip())
        where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    sql = "SELECT * FROM app_notes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pinned DESC, created_at DESC, id DESC LIMIT ?"
    rows = await db.fetch(sql, [*params, limit], limit=limit)
    books = await db.fetch(
        "SELECT notebook AS name, COUNT(*) AS count FROM app_notes GROUP BY notebook ORDER BY notebook"
    )
    return {
        "notes": [_note_out(r) for r in rows],
        "notebooks": [{"name": b["name"], "count": int(b["count"])} for b in books],
    }


@router.post("/notes", status_code=status.HTTP_201_CREATED)
async def create_note(
    body: NoteCreate,
    db: AppDB = Depends(_db),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI; None when called directly (MCP)
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if not body.body.strip() and not (body.title or "").strip():
        raise HTTPException(422, detail="a note needs a body or a title")

    async def fetch(note_id: str) -> dict[str, Any] | None:
        row = await db.fetch_one("SELECT * FROM app_notes WHERE id = ?", [note_id])
        return None if row is None else _note_out(row)

    async def insert(note_id: str) -> dict[str, Any]:
        return await _insert_note(
            db, body=body.body, title=body.title, notebook=body.notebook, tags=body.tags, pinned=body.pinned,
            row_id=note_id,
        )

    key = idempotency.header_key(request) if request is not None else None
    return await idempotency.create_once(
        db, scope="notes", client_id=body.id, key=key, fetch=fetch, insert=insert, response=response
    )


@router.post("/notes/voice", status_code=status.HTTP_201_CREATED)
async def voice_note(
    file: UploadFile = File(...),
    notebook: str | None = Form(None),
    mode: Literal["note", "tasks", "both", "auto"] = Form("auto"),
    language: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """``Idempotency-Key`` (header or form field): a replayed upload answers the first result again."""
    idem = idempotency_key or (request.headers.get("idempotency-key") if request is not None else None)
    return await idempotency.run_once(
        db, scope="notes.voice", key=idem, response=response,
        run=lambda: _voice_note(file, notebook, mode, language, state, db),
    )


async def _voice_note(
    file: UploadFile, notebook: str | None, mode: str, language: str | None, state: HubState, db: AppDB
) -> dict[str, Any]:
    today = local_today()
    key, transcript = await store_and_transcribe(state, file, today, language)

    now = local_now()
    structured = await _structure(state, transcript, today, language, now if now.date() == today else None)
    tasks_in = structured["tasks"] if mode != "note" else []
    # "auto" also records money the speaker spent or received; a memo that is only about money
    # ("j'ai payé 45 dh de taxi") then makes no note.
    money_in = structured["money"] if mode == "auto" else []
    money_only = bool(money_in) and structured["money_only"] and not tasks_in
    save_note = (mode in ("note", "both", "auto") and not money_only) or (mode == "tasks" and not tasks_in)

    note: dict[str, Any] | None = None
    if save_note:
        note = await _insert_note(
            db,
            body=structured["body"],
            title=structured["title"],
            notebook=notebook,
            tags=structured["tags"],
            pinned=False,
            source="voice",
            audio_key=key,
        )
    tasks = [
        await _insert_task(
            db,
            title=t["title"],
            details=t["details"],
            due=t["due"],
            due_time=t["due_time"],
            priority=t["priority"],
            note_id=note["id"] if note else None,
        )
        for t in tasks_in
    ]
    for task in tasks:  # same instant Google Calendar push as POST /tasks
        await gcal.push_task(state, db, task["id"], task, created=True)
    created: dict[str, list[dict[str, Any]]] = {"expenses": [], "incomes": []}
    if money_in:
        from hub_api.routers import money  # money imports this module

        created = await money.create_entries(db, money_in, audio_key=key)
    return {"transcript": transcript, "note": note, "tasks": tasks, "audio_key": key, **created}


async def store_and_transcribe(
    state: HubState, file: UploadFile, today: date, language: str | None
) -> tuple[str, str]:
    """Keep the recording under ``voice/<yyyy>/<mm>/`` in the Drive and transcribe it: (key, transcript).

    Answers 422 when no speech is heard. Shared with ``POST /money/voice``.
    """
    audio = await read_upload(file, state.hub.max_upload_bytes)
    audio_id = new_id()
    ext = _audio_ext(file.filename, file.content_type)
    key = f"voice/{today:%Y}/{today:%m}/{audio_id}.{ext}"
    content_type = file.content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
    meta = {"original-filename": file.filename} if file.filename else None
    await state.router.storage_put(key, audio, content_type, meta)

    tr = await state.router.transcribe(
        TranscriptionRequest(audio=audio, filename=f"{audio_id}.{ext}", language=language or None)
    )
    transcript = (tr.text or "").strip()
    if not transcript:
        raise HTTPException(422, detail="no speech detected in the recording")
    return key, transcript


_AUDIO_EXT = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
    "audio/x-m4a": "m4a", "audio/m4a": "m4a", "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/wave": "wav", "audio/flac": "flac", "video/webm": "webm", "video/mp4": "mp4",
}


def _audio_ext(filename: str | None, content_type: str | None) -> str:
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", ext):
            return ext
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return _AUDIO_EXT.get(base, "webm")


def _voice_prompt(today: date, now: datetime | None = None) -> str:
    week = ", ".join(f"{(today + timedelta(days=i)):%A} = {(today + timedelta(days=i)).isoformat()}" for i in range(1, 8))
    clock = f", the time is {now:%H:%M}" if now is not None and now.date() == today else ""
    return (
        "You turn a voice memo transcript into a clean note and a list of tasks.\n"
        f"Today is {today:%A} {today.isoformat()}{clock} (time zone Africa/Casablanca). Upcoming days: {week}.\n"
        "The speaker may use English, French, Moroccan Darija or a mix (e.g. 'demain' / 'ghda' = tomorrow).\n"
        "Answer with ONE JSON object only, no prose, no code fences, with exactly these keys:\n"
        '{"title": "short title (max 8 words)", "body": "the memo rewritten as clean markdown, same language, '
        'nothing invented", "tags": ["lowercase", "keywords"], "tasks": [{"title": "imperative action", '
        '"due": "YYYY-MM-DD or null", "due_time": "HH:MM (24 h) or null", '
        '"when": "the exact words the speaker used for the day and/or time, or null", '
        '"priority": "low|normal|high", "details": "string or null"}], '
        '"money": [{"type": "expense|income", "amount": number, "currency": "ISO 4217 code (dh/dirham = MAD) or null", '
        '"merchant": "who was paid, or who paid", "category": "string", "date": "YYYY-MM-DD or null", '
        '"note": "string or null"}], "money_only": true|false}\n'
        "Rules: tasks only for concrete actionable items the speaker must do (otherwise an empty list); "
        "convert every relative date (tomorrow, friday, next week ...) to an absolute YYYY-MM-DD using today's date; "
        "due is null when no date is mentioned; priority 'high' only when urgency is expressed. "
        "due_time only when a time of day is said: 'à 15h' = 15:00, '18h30' = 18:30, 'ce soir' / 'tonight' = 20:00, "
        "'le matin' / 'sbah' = 09:00, 'midi' / 'noon' = 12:00, 'l'après-midi' / 'l3chiya' = 15:00; a bare hour from "
        "1 to 7 without 'matin' / 'sbah' / 'am' is in the afternoon ('ghda m3a 3' = tomorrow 15:00, "
        "'f 5 dyal l3chiya' = 17:00). A time with no day is today, or tomorrow if that time has already passed. "
        "money: only amounts the speaker already spent or received (e.g. \"j'ai payé 45 dh de taxi\", "
        "\"I got my salary\"), never plans, prices or bills still to pay (those are tasks); otherwise an empty list. "
        "money_only is true when the memo is about nothing but those amounts."
    )


def _task_when(t: dict[str, Any], now: datetime) -> dict[str, str | None]:
    """due/due_time of one LLM task: the speaker's own words ("when") win over the LLM's date arithmetic."""
    when = t.get("when")
    if isinstance(when, str) and when.strip():
        due, due_time = resolve_when(when, None, now)
        if due:
            if due_time is None and t.get("due_time"):  # "vendredi" said, the time only in due_time
                due_time = normalize_time(t.get("due_time"))
            return {"due": due, "due_time": due_time}
    due, due_time = resolve_when(t.get("due"), t.get("due_time"), now)
    return {"due": due, "due_time": due_time}


async def _structure(
    state: HubState, transcript: str, today: date, language: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """LLM pass over the transcript; falls back to a plain note when the answer is unusable."""
    fallback: dict[str, Any] = {
        "title": derive_title(transcript), "body": transcript, "tags": [], "tasks": [], "money": [], "money_only": False,
    }
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_voice_prompt(today, now)),
            ChatMessage(role="user", content=f"Transcript{f' (language: {language})' if language else ''}:\n{transcript}"),
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    try:
        resp = await state.router.chat(request)
    except ProviderError as exc:
        log.warning("voice note structuring failed, keeping the raw transcript: %s", exc)
        return fallback
    data = extract_json(resp.content)
    if data is None:
        log.warning("voice note structuring returned no JSON, keeping the raw transcript")
        return fallback
    body = data.get("body")
    body = body.strip() if isinstance(body, str) and body.strip() else transcript
    title = data.get("title")
    title = title.strip()[:300] if isinstance(title, str) and title.strip() else derive_title(body)
    tasks: list[dict[str, Any]] = []
    raw_tasks = data.get("tasks")
    for t in raw_tasks if isinstance(raw_tasks, list) else []:
        if isinstance(t, str):
            t = {"title": t}
        if not isinstance(t, dict) or not isinstance(t.get("title"), str) or not t["title"].strip():
            continue
        prio = str(t.get("priority") or "normal").strip().lower()
        details = t.get("details")
        tasks.append(
            {
                "title": t["title"].strip()[:300],
                **_task_when(t, now or datetime.combine(today, datetime.min.time())),
                "priority": prio if prio in _PRIORITIES else "normal",
                "details": details.strip() if isinstance(details, str) and details.strip() else None,
            }
        )
    money: list[dict[str, Any]] = []
    if isinstance(data.get("money"), list) and data["money"]:
        from hub_api.routers.money import parse_money_entries  # money imports this module

        money = parse_money_entries({"entries": data["money"]}, today)
    return {
        "title": title, "body": body, "tags": _clean_tags(data.get("tags")), "tasks": tasks,
        "money": money, "money_only": data.get("money_only") is True,
    }


@router.get("/notes/{note_id}")
async def get_note(note_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _note_out(await _get_note(db, note_id))


@router.patch("/notes/{note_id}")
async def patch_note(note_id: str, body: NotePatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    await _get_note(db, note_id)
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field != "audio_key":
            raise HTTPException(422, detail=f"{field} cannot be null")
        if field == "tags":
            value = json.dumps(_clean_tags(value))
        elif field == "pinned":
            value = 1 if value else 0
        elif field == "title":
            value = value.strip() or "Untitled"
        elif field == "notebook":
            value = value.strip() or "Inbox"
        changes[field] = value
    if changes:
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_notes SET {sets} WHERE id = ?", [*changes.values(), note_id])
    return _note_out(await _get_note(db, note_id))


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get_note(db, note_id)
    await db.execute("DELETE FROM app_notes WHERE id = ?", [note_id])
    await db.execute("UPDATE app_tasks SET note_id = NULL WHERE note_id = ?", [note_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- tasks


class TaskCreate(BaseModel):
    id: str | None = Field(None, description="client-generated id (offline queue); a replay answers the existing task")
    title: str = Field(min_length=1, max_length=300)
    details: str | None = Field(None, max_length=20_000)
    due: str | None = None
    due_time: str | None = Field(None, description="HH:MM, Africa/Casablanca; needs a due date")
    priority: Priority = "normal"
    note_id: str | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=300)
    details: str | None = Field(None, max_length=20_000)
    due: str | None = None
    due_time: str | None = None
    done: bool | None = None
    priority: Priority | None = None
    note_id: str | None = None


@router.get("/tasks")
async def list_tasks(
    status_: Literal["open", "done", "all"] = Query("open", alias="status"),
    due_before: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status_ == "open":
        where.append("done = 0")
    elif status_ == "done":
        where.append("done = 1")
    if due_before:
        where.append("due IS NOT NULL AND due < ?")
        params.append(_due_param(due_before))
    sql = "SELECT * FROM app_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if status_ == "done":
        sql += " ORDER BY done_at DESC, created_at DESC"
    else:
        sql += " ORDER BY done ASC, (due IS NULL) ASC, due ASC, (due_time IS NULL) ASC, due_time ASC, created_at ASC"
    sql += " LIMIT ?"
    rows = await db.fetch(sql, [*params, limit], limit=limit)
    return {"tasks": [_task_out(r) for r in rows]}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    db: AppDB = Depends(_db),
    state: HubState | None = Depends(get_state),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI; None when called directly (MCP)
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if not body.title.strip():
        raise HTTPException(422, detail="title cannot be blank")
    due = _due_param(body.due)
    due_time = _time_param(body.due_time)
    if due_time and not due:
        raise HTTPException(422, detail="due_time needs a due date")

    async def fetch(task_id: str) -> dict[str, Any] | None:
        row = await db.fetch_one("SELECT * FROM app_tasks WHERE id = ?", [task_id])
        return None if row is None else _task_out(row)

    async def insert(task_id: str) -> dict[str, Any]:
        task = await _insert_task(
            db,
            title=body.title,
            details=body.details,
            due=due,
            due_time=due_time,
            priority=body.priority,
            note_id=body.note_id,
            row_id=task_id,
        )
        await gcal.push_task(state, db, task["id"], task, created=True)  # best effort, bounded (~4 s)
        return task

    key = idempotency.header_key(request) if request is not None else None
    return await idempotency.create_once(
        db, scope="tasks", client_id=body.id, key=key, fetch=fetch, insert=insert, response=response
    )


# Fields the Google Calendar event is built from: changing only these re-pushes the event.
_EVENT_FIELDS = frozenset(("title", "details", "due", "due_time", "done", "priority"))


@router.patch("/tasks/{task_id}")
async def patch_task(
    task_id: str, body: TaskPatch, db: AppDB = Depends(_db), state: HubState | None = Depends(get_state)
) -> dict[str, Any]:
    current = await _get_task(db, task_id)
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field in ("title", "done", "priority"):
            raise HTTPException(422, detail=f"{field} cannot be null")
        if field == "due":
            value = _due_param(value)
            if value is None:
                changes["due_time"] = None  # no day, no time
        elif field == "due_time":
            value = _time_param(value)
            if value and not (body.due if "due" in body.model_fields_set else current.get("due")):
                raise HTTPException(422, detail="due_time needs a due date")
        elif field == "title":
            value = value.strip()
            if not value:
                raise HTTPException(422, detail="title cannot be blank")
        elif field == "done":
            was_done = bool(current.get("done"))
            if value and not was_done:
                changes["done_at"] = now_iso()
            elif not value:
                changes["done_at"] = None
            value = 1 if value else 0
        changes[field] = value
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_tasks SET {sets} WHERE id = ?", [*changes.values(), task_id])
    task = _task_out(await _get_task(db, task_id))
    if _EVENT_FIELDS & set(changes):
        await gcal.push_task(state, db, task_id, task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str, db: AppDB = Depends(_db), state: HubState | None = Depends(get_state)
) -> Response:
    await _get_task(db, task_id)
    await db.execute("DELETE FROM app_tasks WHERE id = ?", [task_id])
    await gcal.push_task(state, db, task_id, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
