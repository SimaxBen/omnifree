"""/gcal: the Google Calendar integration (see :mod:`hub_api.gcal`).

* ``GET /gcal/status``: configured?, the service-account e-mail to share the calendars with, each calendar's
  access role as the service account sees it, the last reconcile and the recent push errors;
* ``POST /gcal/sync``: reconcile the OmniFree calendar with the tasks (the target of a QStash schedule; no query
  parameter needed). Options in headers, since a QStash target carries at most one query parameter:
  ``X-OmniFree-Full: 1`` rewrites every event even when unchanged, ``X-OmniFree-Budget: <seconds>`` (5-50,
  default 45) bounds the run so it fits the 60 s serverless limit; ``remaining`` says how many tasks were left;
* ``GET /gcal/events?days=7``: the agenda from the read calendars, OmniFree's own task events left out,
  cached 5 minutes;
* ``POST /gcal/events``: add an event to the OmniFree calendar (what the MCP tool ``calendar_add_event`` uses).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from hub_api import gcal
from hub_api.appdata import AppDB, get_appdb, local_now, local_today
from hub_api.state import HubState, get_state

__all__ = ["router", "EventCreate", "parse_when", "list_events", "create_event"]

router = APIRouter(prefix="/gcal", tags=["gcal"])

MAX_DAYS = 60


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await gcal.ensure_schema(db)


@router.get("/status")
async def gcal_status(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    return await gcal.calendar_status(state, db)


@router.post("/sync")
async def gcal_sync(
    full: bool = Query(False, description="Rewrite every event, even unchanged ones"),
    full_header: str | None = Header(None, alias="X-OmniFree-Full"),
    budget_header: float | None = Header(None, alias="X-OmniFree-Budget", ge=5, le=50),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Reconcile tasks -> Google Calendar (upserts, then deletes orphaned task events)."""
    force = full or (full_header or "").strip().lower() in ("1", "true", "yes")
    return await gcal.sync_all(state, db, budget_s=budget_header or 45.0, full=force)


async def list_events(state: HubState, days: int) -> dict[str, Any]:
    cfg = gcal.load_config(state.settings.secret)
    start, end = gcal.day_bounds(local_today(), days)
    events, errors = await gcal.events_between(state, start, end)
    return {"configured": cfg.configured, "from": start.isoformat(), "to": end.isoformat(), "events": events,
            "errors": errors}


@router.get("/events")
async def gcal_events(
    days: int = Query(7, ge=1, le=MAX_DAYS),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    """Upcoming events (today included) from ``GOOGLE_CALENDAR_READ_IDS``."""
    return await list_events(state, days)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    start: str = Field(description="YYYY-MM-DD (all day), YYYY-MM-DDTHH:MM local time, or a phrase ('demain à 15h')")
    end: str | None = Field(None, description="Same formats as start; default: 1 hour later, or the same day")
    all_day: bool = False
    location: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=8000)


_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2})(?::\d{2})?)?$")


def parse_when(text: str, now: datetime) -> datetime | date:
    """A local (Africa/Casablanca, naive) datetime, or a date when no time is given."""
    from hub_api.routers.notes import TIME_RE, resolve_when

    value = text.strip()
    m = _ISO_RE.match(value)
    if m:
        day = date.fromisoformat(m.group(1))
        if not m.group(2):
            return day
        tm = TIME_RE.match(m.group(2))
        if not tm:
            raise ValueError(f"invalid time in {text!r}")
        return datetime.combine(day, datetime.min.time()).replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
    due, at = resolve_when(value, None, now)
    if due is None:
        raise ValueError(f"could not understand {text!r}: use YYYY-MM-DD, YYYY-MM-DDTHH:MM or e.g. 'demain à 15h'")
    day = date.fromisoformat(due)
    if not at:
        return day
    h, mi = (int(x) for x in at.split(":"))
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=mi)


async def create_event(state: HubState, body: EventCreate) -> dict[str, Any]:
    now = local_now()
    start = parse_when(body.start, now)
    end = parse_when(body.end, now) if body.end else None
    if body.all_day:
        start = start.date() if isinstance(start, datetime) else start
        end = end.date() if isinstance(end, datetime) else end
    elif isinstance(start, datetime) and end is not None and not isinstance(end, datetime):
        raise ValueError("a timed start needs a timed end (YYYY-MM-DDTHH:MM)")
    elif isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
        raise ValueError("end must be after start")
    if isinstance(start, date) and not isinstance(start, datetime) and isinstance(end, datetime):
        end = end.date()
    return await gcal.add_event(state, title=body.title.strip(), start=start, end=end, location=body.location,
                                description=body.description)


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def gcal_add_event(body: EventCreate, state: HubState = Depends(get_state)) -> dict[str, Any]:
    try:
        return await create_event(state, body)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except gcal.GCalError as exc:
        code = status.HTTP_409_CONFLICT if exc.status_code is None else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(code, detail=str(exc)) from exc
