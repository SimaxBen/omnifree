"""/plan: an AI-proposed plan for today (Google agenda + open tasks -> ordered time blocks).

* ``POST /plan/today``: build a new plan for today and cache it in D1 (``app_plans``, one row per day). Inputs are
  today's Google Calendar events (:func:`hub_api.gcal.agenda_day`), the open tasks that matter today (overdue, due
  today, high priority due within a week; an estimate written in the title/details such as ``(45m)`` or
  ``~1h30`` is honoured, 30 minutes otherwise) and the free time between 08:00 and 21:00 Africa/Casablanca (from
  now, rounded up to the next quarter hour, when planning later in the day). ONE bounded LLM call orders the tasks
  into the free slots; its answer is validated (known task ids, inside the window, no overlap) and the
  deterministic greedy planner is used whenever the call fails or the answer is unusable. Timed tasks stay at
  their own time. Re-planning clears the calendar blocks booked by the previous plan of the day. No body needed
  (a QStash schedule can call it); ``{"use_llm": false}`` or the header ``X-OmniFree-Llm: 0`` skips the LLM.
* ``GET /plan/today``: the cached plan (``status`` "none" when there is none).
* ``POST /plan/today/accept`` ``{"book": bool}``: mark the plan accepted; with ``book`` the flexible task blocks
  become events in the OmniFree Google calendar, tagged with the private extended properties
  ``omnifreeKind=plan`` and ``omnifreePlanDay=<YYYY-MM-DD>`` (bounded so the call fits the 60 s limit).
* ``DELETE /plan/today``: "Clear plan": deletes the tagged calendar events of the day and the cached plan.

Blocks: ``{start, end, kind: event|task|break, ref_id, title, reason, fixed}``; ``start``/``end`` are local
``HH:MM``. The morning push shows a short "Plan :" line when today's plan exists (:func:`morning_line`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from core.interfaces import ChatMessage, ChatRequest
from hub_api import gcal
from hub_api.appdata import AppDB, extract_json, get_appdb, local_now, local_tz, now_iso
from hub_api.routers.notes import ensure_schema as ensure_notes
from hub_api.state import HubState, get_state

__all__ = [
    "router", "SCHEMA", "estimate_minutes", "free_gaps", "greedy_plan", "validate_llm_plan", "morning_line",
    "hhmm", "to_minutes",
]

log = logging.getLogger("omnifree.hub.plan")

router = APIRouter(prefix="/plan", tags=["plan"])

DAY_START = 8 * 60  # 08:00 local
DAY_END = 21 * 60  # 21:00 local
LUNCH = (13 * 60, 13 * 60 + 45)
DEFAULT_MINUTES = 30
MIN_MINUTES, MAX_MINUTES = 10, 240
BREAK_AFTER = 90  # minutes of back-to-back task blocks before a break...
BREAK_MINUTES = 15  # ...of this length
MAX_TASKS = 25  # candidates sent to the LLM / placed by the planner
LLM_TIMEOUT_S = 20.0
AGENDA_TIMEOUT_S = 6.0
BOOK_BUDGET_S = 35.0  # booking the blocks into Google Calendar
CLEAR_TIMEOUT_S = 12.0
PLAN_KIND = "plan"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_plans ("
    " day TEXT PRIMARY KEY, blocks TEXT NOT NULL, unscheduled TEXT NOT NULL DEFAULT '[]',"
    " source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', events TEXT NOT NULL DEFAULT '[]',"
    " window_start TEXT, window_end TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
)


async def ensure_schema(db: AppDB) -> AppDB:
    await ensure_notes(db)
    await db.ensure("plans", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- pure helpers


def hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


_HHMM_RE = re.compile(r"^\s*([01]?\d|2[0-4])[:hH]([0-5]\d)\s*$")


def to_minutes(value: Any) -> int | None:
    """``"09:30"`` / ``"9h30"`` -> 570; None when not a time of day."""
    if not isinstance(value, str):
        return None
    m = _HHMM_RE.match(value)
    if not m:
        return None
    total = int(m.group(1)) * 60 + int(m.group(2))
    return total if total <= 24 * 60 else None


_EST_RES = (
    # "1h30", "1 h 30", "2h", "1.5h", "90 min", "45m", "~20 mn", "est: 1h"
    re.compile(r"(?<![\w.])(\d{1,2}(?:[.,]\d)?)\s*h(?:ours?|rs?|eures?)?\s*(\d{1,2})?(?:\s*(?:m|min|mn))?(?![\w])", re.I),
    re.compile(r"(?<![\w.])(\d{1,3})\s*(?:m|min|mins|minutes?|mn)(?![\w])", re.I),
)
_EST_CONTEXT = re.compile(r"[(\[~≈]|\best(?:imate)?\b|\bdur(?:ée|ee|ation)?\b|⏱", re.I)


def estimate_minutes(task: dict[str, Any]) -> int:
    """A task's duration: an estimate written in the title or details ("(45m)", "~1h30", "est: 2h", "durée 20 min"),
    else :data:`DEFAULT_MINUTES`; clamped to [10, 240]."""
    for text in (str(task.get("title") or ""), str(task.get("details") or "")):
        if not text or not _EST_CONTEXT.search(text):
            continue
        for rx in _EST_RES:
            m = rx.search(text)
            if not m:
                continue
            if rx is _EST_RES[0]:
                minutes = round(float(m.group(1).replace(",", ".")) * 60) + int(m.group(2) or 0)
            else:
                minutes = int(m.group(1))
            if minutes > 0:
                return max(MIN_MINUTES, min(MAX_MINUTES, minutes))
    return DEFAULT_MINUTES


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_gaps(start: int, end: int, busy: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The free intervals of [start, end) around the ``busy`` ones (minutes of the day)."""
    gaps: list[tuple[int, int]] = []
    cur = start
    for s, e in _merge(busy):
        if e <= cur:
            continue
        if s >= end:
            break
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
    if cur < end:
        gaps.append((cur, end))
    return gaps


def _block(start: int, end: int, kind: str, ref_id: str | None, title: str, reason: str,
           fixed: bool = False) -> dict[str, Any]:
    return {"start": hhmm(start), "end": hhmm(end), "kind": kind, "ref_id": ref_id, "title": title,
            "reason": reason, "fixed": fixed}


def _task_reason(task: dict[str, Any], today: date) -> str:
    due = str(task.get("due") or "")
    bits: list[str] = []
    if due and due < today.isoformat():
        bits.append(f"overdue since {due}")
    elif due == today.isoformat():
        bits.append("due today")
    elif due:
        bits.append(f"due {due}")
    if task.get("priority") == "high":
        bits.append("high priority")
    return ", ".join(bits) or "open task"


def greedy_plan(
    window: tuple[int, int], busy: list[tuple[int, int]], tasks: list[dict[str, Any]], today: date,
    *, lunch: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic plan: fill the free gaps in time order with the tasks in priority order (first that fits),
    a 15-minute break after 90 minutes of back-to-back tasks and a lunch break at 13:00 when that time is free.

    ``tasks`` are already ordered (most important first) and carry ``minutes``. Returns ``(blocks, unscheduled)``
    without the events (the caller adds them).
    """
    start, end = window
    blocks: list[dict[str, Any]] = []
    busy = list(busy)
    if lunch and start <= LUNCH[0] and LUNCH[1] <= end and not any(s < LUNCH[1] and e > LUNCH[0] for s, e in busy):
        blocks.append(_block(*LUNCH, "break", None, "Lunch", "a real break in the middle of the day"))
        busy.append(LUNCH)
    remaining = list(tasks)
    for g_start, g_end in free_gaps(start, end, busy):
        cur, run = g_start, 0
        while remaining:
            if run >= BREAK_AFTER and cur + BREAK_MINUTES <= g_end:
                blocks.append(_block(cur, cur + BREAK_MINUTES, "break", None, "Break", f"after {run} min of work"))
                cur, run = cur + BREAK_MINUTES, 0
            pick = next((t for t in remaining if t["minutes"] <= g_end - cur), None)
            if pick is None:
                break
            remaining.remove(pick)
            blocks.append(_block(cur, cur + pick["minutes"], "task", pick["id"], pick["title"], _task_reason(pick, today)))
            cur += pick["minutes"]
            run += pick["minutes"]
    unscheduled = [{"id": t["id"], "title": t["title"], "reason": "no free slot long enough today"} for t in remaining]
    return blocks, unscheduled


def validate_llm_plan(
    data: dict[str, Any] | None, window: tuple[int, int], busy: list[tuple[int, int]], tasks: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]] | None:
    """The LLM's task/break blocks that are usable (known task once, inside the window, no overlap with the busy
    time or with each other); None when the answer is unusable (no JSON, or not even half of the blocks valid)."""
    if not isinstance(data, dict):
        return None
    raw = data.get("blocks")
    if not isinstance(raw, list):
        return None
    by_id = {t["id"]: t for t in tasks}
    taken: list[tuple[int, int]] = list(busy)
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    proposed = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or ("task" if item.get("task_id") else "break")).lower()
        if kind == "event":
            continue  # events are ours, never the LLM's
        proposed += 1
        s, e = to_minutes(item.get("start")), to_minutes(item.get("end"))
        if s is None or e is None or not (window[0] <= s < e <= window[1]) or not (5 <= e - s <= MAX_MINUTES):
            continue
        if any(s < te and e > ts for ts, te in taken):
            continue
        reason = item.get("reason")
        reason = reason.strip()[:160] if isinstance(reason, str) and reason.strip() else ""
        if kind == "break":
            if e - s > 75:
                continue
            title = item.get("title")
            title = title.strip()[:60] if isinstance(title, str) and title.strip() else "Break"
            out.append(_block(s, e, "break", None, title, reason or "rest"))
        else:
            task_id = str(item.get("task_id") or item.get("ref_id") or "")
            task = by_id.get(task_id)
            if task is None or task_id in used:
                continue
            used.add(task_id)
            out.append(_block(s, e, "task", task_id, task["title"], reason or _task_reason(task, today)))
        taken.append((s, e))
    if proposed and len(out) * 2 < proposed:
        return None
    if tasks and not any(b["kind"] == "task" for b in out) and free_gaps(*window, busy):
        return None  # nothing placed although there was room: the greedy plan is better
    return out


# --------------------------------------------------------------------------- inputs


def _event_span(ev: dict[str, Any], day: date) -> tuple[int, int] | None:
    """Minutes of ``day`` covered by a timed event (clipped to the day), None for all-day or other days."""
    if ev.get("all_day"):
        return None
    tz = local_tz()
    try:
        s = datetime.fromisoformat(str(ev["start"])).astimezone(tz)
        e = datetime.fromisoformat(str(ev.get("end") or ev["start"])).astimezone(tz)
    except (KeyError, ValueError):
        return None
    lo, hi = datetime.combine(day, time(0), tz), datetime.combine(day + timedelta(days=1), time(0), tz)
    if e <= lo or s >= hi:
        return None
    s, e = max(s, lo), min(e, hi)
    start = int((s - lo).total_seconds() // 60)
    end = int((e - lo).total_seconds() // 60)
    return (start, max(end, start + 5))


async def _candidates(db: AppDB, today: date) -> list[dict[str, Any]]:
    """Open tasks that matter today: overdue, due today, or high priority (undated or due within a week)."""
    week = (today + timedelta(days=7)).isoformat()
    rows = await db.fetch(
        "SELECT id, title, details, due, due_time, priority, created_at FROM app_tasks WHERE done = 0"
        " AND ((due IS NOT NULL AND due <= ?) OR (priority = 'high' AND (due IS NULL OR due <= ?)))"
        " ORDER BY (due IS NULL), due, CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at"
        " LIMIT ?",
        [today.isoformat(), week, MAX_TASKS * 2],
    )
    t = today.isoformat()

    def rank(r: dict[str, Any]) -> tuple[int, int, str, str]:
        due = r.get("due") or ""
        bucket = 0 if due and due < t else 1 if due == t else 2
        prio = {"high": 0, "normal": 1}.get(str(r.get("priority")), 2)
        return (bucket, prio, due or "9999", str(r.get("created_at") or ""))

    return sorted(rows, key=rank)


def _now_minutes(now: datetime, today: date) -> int:
    if now.date() != today:
        return 0 if now.date() < today else 24 * 60
    m = now.hour * 60 + now.minute + (1 if now.second else 0)
    return -(-m // 15) * 15  # next quarter hour


def _llm_prompt(today: date, window: tuple[int, int], busy: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> str:
    return (
        "You plan one person's day. Place their tasks into the FREE time only, as ordered time blocks.\n"
        f"Day: {today:%A} {today.isoformat()} (Africa/Casablanca). Planning window: {hhmm(window[0])}-{hhmm(window[1])}.\n"
        "Rules: never overlap a busy block or another block; stay inside the window; each task at most once, using "
        "its minutes (you may shorten a task to fit, never below 15 min); most important first (overdue, due today, "
        "high priority) and demanding work earlier in the day; add a short break (10-15 min) after about 90 minutes "
        "of back-to-back work and a lunch break around 13:00 if free; leave out tasks that do not fit.\n"
        'Answer with ONE JSON object only: {"blocks": [{"kind": "task|break", "task_id": "id or null", '
        '"start": "HH:MM", "end": "HH:MM", "reason": "few words, why here"}]}\n'
        f"Busy: {json.dumps(busy, ensure_ascii=False)}\n"
        f"Tasks: {json.dumps(tasks, ensure_ascii=False)}"
    )


async def _llm_blocks(
    state: HubState, today: date, window: tuple[int, int], busy_spans: list[tuple[int, int]],
    busy_titles: list[dict[str, Any]], tasks: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    brief = [
        {"id": t["id"], "title": t["title"][:120], "minutes": t["minutes"], "priority": t.get("priority") or "normal",
         "due": t.get("due")}
        for t in tasks
    ]
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_llm_prompt(today, window, busy_titles, brief)),
            ChatMessage(role="user", content="Plan my day."),
        ],
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    try:
        resp = await asyncio.wait_for(state.router.chat(request), LLM_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - ProviderError, timeout: the greedy plan takes over
        log.info("day plan LLM unavailable, using the greedy plan: %r", exc)
        return None
    return validate_llm_plan(extract_json(resp.content), window, busy_spans, tasks, today)


# --------------------------------------------------------------------------- storage


def _loads(value: Any, default: Any) -> Any:
    try:
        out = json.loads(value) if isinstance(value, str) else default
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def _plan_out(row: dict[str, Any] | None, day: date, calendar: dict[str, bool] | None = None) -> dict[str, Any]:
    if row is None:
        return {"day": day.isoformat(), "status": "none", "source": None, "blocks": [], "unscheduled": [],
                "booked": 0, "window": None, "generated_at": None, "calendar": calendar}
    events = _loads(row.get("events"), [])
    return {
        "day": row["day"],
        "status": row.get("status") or "proposed",
        "source": row.get("source"),
        "blocks": _loads(row.get("blocks"), []),
        "unscheduled": _loads(row.get("unscheduled"), []),
        "booked": len(events),
        "window": {"start": row.get("window_start"), "end": row.get("window_end")} if row.get("window_start") else None,
        "generated_at": row.get("created_at"),
        "calendar": calendar,
    }


async def _get(db: AppDB, day: date) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM app_plans WHERE day = ?", [day.isoformat()])


def _calendar(state: HubState) -> dict[str, bool]:
    cfg = gcal.load_config(state.settings.secret)
    return {"configured": cfg.configured, "writable": cfg.writable}


async def _clear_events(state: HubState, day: date, known: list[str]) -> tuple[int, str | None]:
    """Delete the plan blocks booked for ``day`` (found by tag, plus the ids we stored). Best effort, bounded."""
    async def run() -> int:
        start, end = gcal.day_bounds(day)
        deleted = set(await gcal.delete_tagged(state, f"omnifreePlanDay={day.isoformat()}", start, end))
        for event in known:
            if event not in deleted:
                await gcal.delete_event(state, event)
                deleted.add(event)
        return len(deleted)

    try:
        return await asyncio.wait_for(run(), CLEAR_TIMEOUT_S), None
    except Exception as exc:  # noqa: BLE001 - the plan row is cleared anyway
        log.warning("could not remove the booked plan blocks of %s: %r", day, exc)
        return 0, "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- routes


class PlanRequest(BaseModel):
    use_llm: bool = True


class AcceptRequest(BaseModel):
    book: bool = False


async def build_plan(state: HubState, db: AppDB, *, use_llm: bool = True) -> dict[str, Any]:
    now = local_now()
    today = now.date()
    previous = await _get(db, today)
    booked_before = _loads(previous.get("events"), []) if previous else []
    cleared_error: str | None = None
    if booked_before:
        _, cleared_error = await _clear_events(state, today, booked_before)

    window = (max(DAY_START, _now_minutes(now, today)), DAY_END)
    events = await gcal.agenda_day(state, today, AGENDA_TIMEOUT_S)
    events = [e for e in events if e.get("id") not in set(booked_before)]
    event_blocks: list[dict[str, Any]] = []
    busy: list[tuple[int, int]] = []
    busy_titles: list[dict[str, Any]] = []
    for ev in events:
        span = _event_span(ev, today)
        if span is None or span[1] <= window[0] or span[0] >= window[1]:
            continue
        event_blocks.append(_block(span[0], span[1], "event", ev.get("id") or None, str(ev.get("title") or "Event"),
                                   str(ev.get("calendar") or "Calendar"), fixed=True))
        busy.append(span)
        busy_titles.append({"start": hhmm(span[0]), "end": hhmm(span[1]), "what": str(ev.get("title") or "")[:80]})

    fixed_blocks: list[dict[str, Any]] = []
    flexible: list[dict[str, Any]] = []
    for row in await _candidates(db, today):
        task = {**row, "minutes": estimate_minutes(row)}
        at = to_minutes(row.get("due_time")) if row.get("due") == today.isoformat() else None
        if at is not None and at >= window[0] and at < window[1]:
            end = min(at + task["minutes"], 24 * 60)
            fixed_blocks.append(_block(at, end, "task", row["id"], row["title"], f"due at {hhmm(at)}", fixed=True))
            busy.append((at, end))
            busy_titles.append({"start": hhmm(at), "end": hhmm(end), "what": f"task: {row['title'][:80]}"})
        elif len(flexible) < MAX_TASKS:
            flexible.append(task)

    source = "greedy"
    placed: list[dict[str, Any]] | None = None
    unscheduled: list[dict[str, Any]] = []
    if window[0] >= window[1]:
        placed = []
        unscheduled = [{"id": t["id"], "title": t["title"], "reason": "the day is over"} for t in flexible]
    elif use_llm and flexible:
        placed = await _llm_blocks(state, today, window, busy, busy_titles, flexible)
        if placed is not None:
            source = "llm"
            used = {b["ref_id"] for b in placed if b["kind"] == "task"}
            unscheduled = [{"id": t["id"], "title": t["title"], "reason": "left out of today's plan"}
                           for t in flexible if t["id"] not in used]
    if placed is None:
        placed, unscheduled = greedy_plan(window, busy, flexible, today, lunch=bool(flexible))

    blocks = sorted(event_blocks + fixed_blocks + placed, key=lambda b: (b["start"], b["end"]))
    stamp = now_iso()
    row = {
        "day": today.isoformat(), "blocks": json.dumps(blocks, ensure_ascii=False),
        "unscheduled": json.dumps(unscheduled, ensure_ascii=False), "source": source, "status": "proposed",
        "events": "[]", "window_start": hhmm(window[0]), "window_end": hhmm(window[1]),
        "created_at": stamp, "updated_at": stamp,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_plans ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        " ON CONFLICT(day) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "day"),
        [row[c] for c in cols],
    )
    out = _plan_out(row, today, _calendar(state))
    if booked_before:
        out["cleared_previous"] = cleared_error is None
    return out


@router.get("/today")
async def get_plan(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_now().date()
    return _plan_out(await _get(db, today), today, _calendar(state))


@router.post("/today")
async def post_plan(
    body: PlanRequest | None = None,
    llm_header: str | None = Header(None, alias="X-OmniFree-Llm"),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    use_llm = (body.use_llm if body is not None else True) and (llm_header or "1").strip().lower() not in ("0", "false", "no")
    return await build_plan(state, db, use_llm=use_llm)


@router.post("/today/accept")
async def accept_plan(
    body: AcceptRequest | None = None, state: HubState = Depends(get_state), db: AppDB = Depends(_db)
) -> dict[str, Any]:
    today = local_now().date()
    row = await _get(db, today)
    if row is None:
        raise HTTPException(404, detail="no plan for today yet: POST /plan/today first")
    booked = _loads(row.get("events"), [])
    errors: list[str] = []
    if body is not None and body.book:
        cfg = gcal.load_config(state.settings.secret)
        if not cfg.writable:
            raise HTTPException(409, detail=cfg.error or "Google Calendar is not set up for writing")
        if booked:  # accepting twice re-books cleanly
            await _clear_events(state, today, booked)
            booked = []
        deadline = _time.monotonic() + BOOK_BUDGET_S
        for block in _loads(row.get("blocks"), []):
            if block.get("kind") != "task" or block.get("fixed"):
                continue
            left = deadline - _time.monotonic()
            if left <= 1:
                errors.append("time budget reached: some blocks were not booked")
                break
            s, e = to_minutes(block.get("start")), to_minutes(block.get("end"))
            if s is None or e is None or e <= s:
                continue
            start = datetime.combine(today, time(0)) + timedelta(minutes=s)
            try:
                ev = await asyncio.wait_for(gcal.add_event(
                    state, title=f"▸ {block.get('title') or 'Task'}", start=start, end=start + timedelta(minutes=e - s),
                    description=f"Planned by OmniFree: {block.get('reason') or ''}".strip(),
                    private={"omnifreeKind": PLAN_KIND, "omnifreePlanDay": today.isoformat(),
                             "omnifreePlanTask": str(block.get("ref_id") or "")},
                ), left)
            except Exception as exc:  # noqa: BLE001 - keep going, report it
                errors.append(f"{block.get('title')}: {'timed out' if isinstance(exc, TimeoutError) else exc}"[:200])
                continue
            if ev.get("id"):
                booked.append(str(ev["id"]))
    await db.execute(
        "UPDATE app_plans SET status = 'accepted', events = ?, updated_at = ? WHERE day = ?",
        [json.dumps(booked), now_iso(), today.isoformat()],
    )
    out = _plan_out(await _get(db, today), today, _calendar(state))
    out["errors"] = errors
    return out


@router.delete("/today")
async def clear_plan(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_now().date()
    row = await _get(db, today)
    known = _loads(row.get("events"), []) if row else []
    deleted, error = await _clear_events(state, today, known) if (known or _calendar(state)["writable"]) else (0, None)
    await db.execute("DELETE FROM app_plans WHERE day = ?", [today.isoformat()])
    return {"day": today.isoformat(), "cleared": True, "deleted_events": deleted, "error": error}


# --------------------------------------------------------------------------- morning push


async def morning_line(db: AppDB, day: date, limit: int = 3) -> str | None:
    """``Plan : 09:00 Rapport · 10:30 Appeler Karim (+2)`` when ``day`` has a plan with task blocks, else None.
    Never raises (the table may not exist yet)."""
    try:
        row = await db.fetch_one("SELECT blocks FROM app_plans WHERE day = ?", [day.isoformat()])
    except Exception as exc:  # noqa: BLE001 - no plan table yet
        log.debug("no day plan for the morning push: %r", exc)
        return None
    if row is None:
        return None
    tasks = [b for b in _loads(row.get("blocks"), []) if isinstance(b, dict) and b.get("kind") == "task"]
    if not tasks:
        return None
    shown = " · ".join(f"{b.get('start')} {b.get('title')}" for b in tasks[:limit])
    more = f" (+{len(tasks) - limit})" if len(tasks) > limit else ""
    return f"Plan : {shown}{more}"
