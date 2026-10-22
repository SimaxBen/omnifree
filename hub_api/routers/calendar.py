"""/calendar: the tasks as an iCalendar (ICS) feed that Google Calendar, Apple Calendar or Outlook subscribe to.

No OAuth: ``GET /calendar/{token}.ics`` is PUBLIC (``public_router``, mounted without the bearer dependency)
and authenticated by an unguessable token in the path (vault key ``CALENDAR_FEED_TOKEN``, compared in constant
time; any mismatch, or no token configured, answers 404). ``GET /calendar/info`` (``router``, bearer protected)
gives the PWA the feed URL. Google Calendar itself is written directly (and instantly) by :mod:`hub_api.gcal`;
this feed stays as the fallback and for Apple Calendar / Outlook.

Feed content (RFC 5545: CRLF line ends, lines folded at 75 octets, TEXT escaping):

* open tasks with a due date, as all-day events (``DTSTART;VALUE=DATE``), UID ``task-<id>@omnifree``;
  a task with a ``due_time`` becomes a 30-minute timed event in UTC (``DTSTART:...Z``, converted from
  Africa/Casablanca with zoneinfo, so the Ramadan UTC+0 weeks are right) with a display alarm 10 minutes before;
* tasks done in the last 14 days, titled ``✓ <title>`` (``?done=0`` leaves them out);
* recurring incomes/expenses **only if** a table named like ``app_money_recurring%`` exists (see
  :mod:`hub_api.recurring`), as all-day events with an RRULE.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from hub_api.appdata import AppDB, get_appdb, iso, local_today, local_tz
from hub_api.auth import token_matches
from hub_api.recurring import recurring_items
from hub_api.routers.notes import TIME_RE
from hub_api.routers.notes import ensure_schema as ensure_notes
from hub_api.state import HubState, get_state

__all__ = [
    "router", "public_router", "TOKEN_KEY", "ics_escape", "fold_line", "build_ics", "task_events", "local_to_utc",
]

log = logging.getLogger("omnifree.hub.calendar")

router = APIRouter(prefix="/calendar", tags=["calendar"])
public_router = APIRouter(tags=["public"])

TOKEN_KEY = "CALENDAR_FEED_TOKEN"
DONE_DAYS = 14
MAX_TASKS = 2000
EVENT_MINUTES = 30  # length of a timed task event
ALARM_MINUTES = 10  # display alarm before a timed open task
_FOLD_AT = 75  # octets per line, CRLF excluded


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await ensure_notes(db)
    return db


# --------------------------------------------------------------------------- RFC 5545 helpers


def ics_escape(text: str) -> str:
    """TEXT value escaping: backslash, semicolon, comma and newlines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold_line(line: str) -> str:
    """Fold a content line at 75 octets (continuations start with one space), never inside a UTF-8 character."""
    chunks: list[str] = []
    current, size, limit = [], 0, _FOLD_AT
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            chunks.append("".join(current))
            current, size, limit = [], 0, _FOLD_AT - 1
        current.append(ch)
        size += n
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def _d(day: date) -> str:
    return day.strftime("%Y%m%d")


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def local_to_utc(day: date, hhmm: str) -> datetime:
    """A wall-clock time in Africa/Casablanca as an aware UTC datetime (UTC+1, UTC+0 during Ramadan)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=local_tz()).astimezone(timezone.utc)


def build_ics(events: list[dict[str, Any]], *, name: str = "OmniFree", now: datetime | None = None) -> str:
    """A VCALENDAR of VEVENTs. Each event: uid, summary and either ``date`` (all-day) or ``start`` (aware
    datetime, written in UTC) with ``minutes``; optional description/url/rrule/priority/status/alarm (minutes
    before the start, timed events only)."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OmniFree//Calendar feed//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:Africa/Casablanca",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events:
        lines += ["BEGIN:VEVENT", f"UID:{ev['uid']}", f"DTSTAMP:{stamp}"]
        start: datetime | None = ev.get("start")
        if start is not None:
            end = start + timedelta(minutes=int(ev.get("minutes") or EVENT_MINUTES))
            lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]
        else:
            day: date = ev["date"]
            lines += [f"DTSTART;VALUE=DATE:{_d(day)}", f"DTEND;VALUE=DATE:{_d(day + timedelta(days=1))}"]
        lines.append(f"SUMMARY:{ics_escape(ev['summary'])}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{ics_escape(ev['description'])}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        if ev.get("rrule"):
            lines.append(f"RRULE:{ev['rrule']}")
        if ev.get("priority"):
            lines.append(f"PRIORITY:{int(ev['priority'])}")
        if ev.get("status"):
            lines.append(f"STATUS:{ev['status']}")
        lines.append("TRANSP:TRANSPARENT")
        if start is not None and ev.get("alarm"):
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{ics_escape(ev['summary'])}",
                f"TRIGGER:-PT{int(ev['alarm'])}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + "\r\n" for line in lines)


# --------------------------------------------------------------------------- events


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _local_day(stamp: Any) -> date | None:
    if not isinstance(stamp, str):
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(local_tz()).date()


def task_events(rows: list[dict[str, Any]], link: str) -> list[dict[str, Any]]:
    """Events for task rows (open ones need a due date; done ones fall back to their done day).

    A due date with a ``due_time`` gives a timed event (UTC) with an alarm when the task is open; otherwise
    the event is all-day.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        done = bool(r.get("done"))
        due = _parse_date(r.get("due"))
        day = due or (_local_day(r.get("done_at")) if done else None)
        if day is None:
            continue
        details = (r.get("details") or "").strip()
        hhmm = r.get("due_time")
        start = local_to_utc(due, hhmm) if due and isinstance(hhmm, str) and TIME_RE.match(hhmm) else None
        out.append({
            "uid": f"task-{r['id']}@omnifree",
            "date": day,
            "start": start,
            "minutes": EVENT_MINUTES,
            "alarm": ALARM_MINUTES if start is not None and not done else None,
            "summary": f"✓ {r['title']}" if done else str(r["title"]),
            "description": f"{details}\n\n{link}" if details else link,
            "url": link,
            "priority": 1 if r.get("priority") == "high" and not done else None,
            "status": "CONFIRMED",
        })
    return out


def _money(amount: float, currency: str) -> str:
    text = f"{amount:,.0f}" if abs(amount - round(amount)) < 0.005 else f"{amount:,.2f}"
    return f"{text.replace(',', ' ').replace('.', ',')} {currency}"


def recurring_events(items: list[dict[str, Any]], today: date, link: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        start = it.get("start")
        if start is None and it.get("day"):
            start = today.replace(day=min(int(it["day"]), 28))
        if start is None:
            continue
        sign = "+" if it["kind"] == "income" else "−"
        out.append({
            "uid": f"money-{it['table']}-{it['id']}@omnifree",
            "date": start,
            "summary": f"↻ {sign}{_money(it['amount'], it['currency'])} {it['label']}",
            "description": f"{'Revenu' if it['kind'] == 'income' else 'Dépense'} récurrent(e)\n\n{link}",
            "url": link,
            "rrule": f"FREQ={it['frequency']}" + (f";UNTIL={it['until']:%Y%m%d}" if it.get("until") else ""),
        })
    return out


# --------------------------------------------------------------------------- routes


def feed_url(state: HubState) -> str | None:
    token = state.settings.secret(TOKEN_KEY)
    return f"{state.hub.public_base_url.rstrip('/')}/calendar/{token}.ics" if token else None


@router.get("/info")
async def calendar_info(state: HubState = Depends(get_state)) -> dict[str, Any]:
    url = feed_url(state)
    return {"configured": url is not None, "url": url, "token_key": TOKEN_KEY}


@public_router.api_route("/calendar/{token}.ics", methods=["GET", "HEAD"], include_in_schema=False)
async def calendar_feed(
    token: str,
    done: bool = Query(True, description="Include tasks done in the last 14 days"),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> Response:
    if not token_matches(state.settings.secret(TOKEN_KEY), token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
    base = state.hub.pwa_base_url.rstrip("/")
    tasks_link = f"{base}/#/notes/tasks"
    rows = await db.fetch(
        "SELECT * FROM app_tasks WHERE done = 0 AND due IS NOT NULL ORDER BY due, due_time, created_at LIMIT ?",
        [MAX_TASKS], limit=MAX_TASKS,
    )
    if done:
        since = iso(datetime.now(timezone.utc) - timedelta(days=DONE_DAYS))
        rows += await db.fetch(
            "SELECT * FROM app_tasks WHERE done = 1 AND done_at >= ? ORDER BY done_at DESC LIMIT ?",
            [since, MAX_TASKS], limit=MAX_TASKS,
        )
    events = task_events(rows, tasks_link)
    try:
        events += recurring_events(await recurring_items(db, local_today()), local_today(), f"{base}/#/more/money")
    except Exception as exc:  # noqa: BLE001 - optional part, never breaks the feed
        log.info("recurring money events skipped: %r", exc)
    return Response(
        content=build_ics(events).encode("utf-8"),
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, max-age=900", "Content-Disposition": 'inline; filename="omnifree.ics"'},
    )
