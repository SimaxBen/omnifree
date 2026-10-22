"""Task extras shared by the task routes, the voice notes and the Google Calendar push.

* checklists: ``app_tasks.checklist`` is a JSON list of ``{"id", "text", "done"}`` (normalized here);
* time blocking: :func:`free_slots` finds free slots inside working hours (09:00-20:00 Africa/Casablanca) around
  the busy intervals; :func:`busy_intervals` gathers them from the agenda (read calendars and the OmniFree
  calendar) plus the timed and blocked tasks; :func:`block_event` is the Google event body of a time block;
* voice appointments: :func:`appointment_event` is the event body for "rdv dentiste jeudi 10h".

Nothing here imports the routers (the notes router imports this module).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from hub_api import gcal
from hub_api.appdata import AppDB, local_tz, random_code

__all__ = [
    "CHECKLIST_MAX", "WORK_START", "WORK_END", "DEFAULT_MINUTES", "normalize_checklist", "checklist_of",
    "checklist_lines", "free_slots", "busy_intervals", "block_event", "appointment_event", "parse_local",
    "fmt_local",
]

log = logging.getLogger("omnifree.hub.taskplus")

CHECKLIST_MAX = 100
CHECKLIST_TEXT_MAX = 300
WORK_START = time(9, 0)
WORK_END = time(20, 0)
DEFAULT_MINUTES = 30  # a time block without an estimate
SLOT_STEP_MIN = 15
TASK_EVENT_MINUTES = gcal.EVENT_MINUTES  # a timed task occupies its slot in the calendar
BLOCK_COLOR = "9"  # "Blueberry" in Google's event palette
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_LOCAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})(?::\d{2})?$")


# --------------------------------------------------------------------------- checklists


def normalize_checklist(raw: Any) -> list[dict[str, Any]]:
    """``[{id, text, done}]`` from user/LLM input (strings or objects): blank items dropped, ids kept when valid
    and unique (new ones otherwise), at most :data:`CHECKLIST_MAX` items. Order is preserved (that is the order)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("checklist must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()[:CHECKLIST_TEXT_MAX]
        if not text:
            continue
        item_id = str(item.get("id") or "")
        if not _ID_RE.match(item_id) or item_id in seen:
            item_id = random_code(8)
        seen.add(item_id)
        out.append({"id": item_id, "text": text, "done": bool(item.get("done"))})
        if len(out) >= CHECKLIST_MAX:
            break
    return out


def checklist_of(value: Any) -> list[dict[str, Any]]:
    """The checklist of a task row (JSON text from the database, or an already decoded list)."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    try:
        return normalize_checklist(value)
    except ValueError:
        return []


def checklist_lines(value: Any) -> str:
    """"☑ done item / ☐ open item" lines for the Google Calendar event description ("" without a checklist)."""
    return "\n".join(f"{'☑' if i['done'] else '☐'} {i['text']}" for i in checklist_of(value))


# --------------------------------------------------------------------------- local times


def parse_local(value: str) -> datetime:
    """``YYYY-MM-DDTHH:MM`` (Africa/Casablanca wall-clock) -> an aware datetime; ValueError otherwise."""
    m = _LOCAL_RE.match(value.strip())
    if not m:
        raise ValueError("start must be YYYY-MM-DDTHH:MM (Africa/Casablanca time)")
    day = date.fromisoformat(m.group(1))
    h, mi = int(m.group(2)), int(m.group(3))
    if h > 23 or mi > 59:
        raise ValueError("start has an invalid time")
    return datetime.combine(day, time(h, mi), local_tz())


def fmt_local(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM`` in Africa/Casablanca."""
    return dt.astimezone(local_tz()).strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------- free slots


def _merge(busy: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted((s, e) for s, e in busy if e > s):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _ceil(dt: datetime, step_min: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt.second or dt.microsecond else timedelta())
    extra = (-dt.minute) % step_min
    return dt + timedelta(minutes=extra)


def free_slots(
    busy: list[tuple[datetime, datetime]],
    *,
    now: datetime,
    minutes: int,
    days: int = 7,
    day_start: time = WORK_START,
    day_end: time = WORK_END,
    limit: int = 8,
    per_day: int = 3,
) -> list[tuple[datetime, datetime]]:
    """Free ``minutes``-long slots from ``now`` over ``days`` days, inside [day_start, day_end) local time.

    Each free gap proposes its start, then one start per hour (or per block length, when longer) while the block
    still fits; at most ``per_day`` slots a day and ``limit`` in all, earliest first. Starts are on 15-minute marks.
    """
    tz = local_tz()
    now = now.astimezone(tz)
    length = timedelta(minutes=minutes)
    hop = timedelta(minutes=max(60, minutes))
    merged = _merge([(s.astimezone(tz), e.astimezone(tz)) for s, e in busy])
    out: list[tuple[datetime, datetime]] = []
    for i in range(days):
        day = now.date() + timedelta(days=i)
        lo = datetime.combine(day, day_start, tz)
        hi = datetime.combine(day, day_end, tz)
        cursor = max(lo, _ceil(now, SLOT_STEP_MIN))
        if cursor >= hi:
            continue
        gaps: list[tuple[datetime, datetime]] = []
        for s, e in merged:
            if e <= cursor or s >= hi:
                continue
            if s > cursor:
                gaps.append((cursor, s))
            cursor = max(cursor, _ceil(e, SLOT_STEP_MIN))
        if cursor < hi:
            gaps.append((cursor, hi))
        taken = 0
        for g_start, g_end in gaps:
            start = _ceil(g_start, SLOT_STEP_MIN)
            while start + length <= g_end and taken < per_day:
                out.append((start, start + length))
                taken += 1
                if len(out) >= limit:
                    return out
                start += hop
            if taken >= per_day:
                break
    return out


def _event_span(ev: dict[str, Any]) -> tuple[datetime, datetime] | None:
    if ev.get("all_day"):
        return None  # birthdays, holidays, trips: not a reason to refuse a 30-minute block
    try:
        s = datetime.fromisoformat(str(ev["start"]))
        e = datetime.fromisoformat(str(ev.get("end") or ev["start"]))
    except (KeyError, ValueError):
        return None
    if s.tzinfo is None:
        s, e = s.replace(tzinfo=local_tz()), e.replace(tzinfo=local_tz())
    return s, max(e, s)


async def busy_intervals(
    state: Any, db: AppDB, start: datetime, end: datetime, *, skip_task: str | None = None,
    skip_event: str | None = None, timeout_s: float = 8.0,
) -> tuple[list[tuple[datetime, datetime]], list[dict[str, Any]]]:
    """Busy intervals in [start, end): the agenda (read calendars, plus the OmniFree calendar when it is not one of
    them), the timed open tasks and the other time blocks. Returns ``(busy, errors)``; a calendar that cannot be
    read is reported in ``errors`` and left out (the tasks still count)."""
    busy: list[tuple[datetime, datetime]] = []
    errors: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    try:
        found, errs = await asyncio.wait_for(gcal.busy_events(state, start, end), timeout_s)
        events, errors = found, list(errs)
    except Exception as exc:  # noqa: BLE001 - the slots still come from the tasks
        errors.append({"calendar": "google", "error": "timed out" if isinstance(exc, TimeoutError) else str(exc)})
    for ev in events:
        if skip_event and ev.get("id") == skip_event:
            continue
        span = _event_span(ev)
        if span:
            busy.append(span)
    tz = local_tz()
    rows = await db.fetch(
        "SELECT id, due, due_time, estimate, block_start, block_end FROM app_tasks WHERE done = 0"
        " AND ((due IS NOT NULL AND due_time IS NOT NULL AND due >= ? AND due <= ?) OR block_start IS NOT NULL)",
        [start.astimezone(tz).date().isoformat(), end.astimezone(tz).date().isoformat()],
    )
    for r in rows:
        if r["id"] == skip_task:
            continue
        if r.get("block_start") and r.get("block_end"):
            try:
                busy.append((parse_local(r["block_start"]), parse_local(r["block_end"])))
            except ValueError:
                pass
        if r.get("due") and r.get("due_time"):
            try:
                s = parse_local(f"{r['due']}T{r['due_time']}")
            except ValueError:
                continue
            busy.append((s, s + timedelta(minutes=int(r.get("estimate") or TASK_EVENT_MINUTES))))
    return busy, errors


# --------------------------------------------------------------------------- event bodies


def _wall(dt: datetime) -> dict[str, str]:
    return {"dateTime": dt.astimezone(local_tz()).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": gcal.TZ_NAME}


def block_event(task: dict[str, Any], start: datetime, end: datetime, link: str) -> dict[str, Any]:
    """Google event body of a task's time block (an opaque event: the time is taken)."""
    parts = [p for p in ((task.get("details") or "").strip(), checklist_lines(task.get("checklist")), link) if p]
    return {
        "summary": str(task["title"]),
        "description": "\n\n".join(parts),
        "start": _wall(start),
        "end": _wall(end),
        "colorId": BLOCK_COLOR,
        "transparency": "opaque",
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 5}]},
        "source": {"title": "OmniFree", "url": link},
        "extendedProperties": {"private": {"omnifreeKind": "block", "omnifreeBlockTaskId": str(task["id"])}},
    }


def appointment_event(title: str, start: datetime, minutes: int, *, location: str | None = None,
                      description: str | None = None) -> dict[str, Any]:
    """Google event body of an appointment heard in a voice note."""
    body: dict[str, Any] = {
        "summary": title,
        "start": _wall(start),
        "end": _wall(start + timedelta(minutes=minutes)),
        "extendedProperties": {"private": {"omnifreeKind": "voice"}},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    return body
