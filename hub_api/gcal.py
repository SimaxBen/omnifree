"""Google Calendar, two ways, through a Google service account (no OAuth consent, no google client libraries).

The owner shares two calendars with the service account's e-mail address:

* a calendar named "OmniFree" with "Make changes to events" (vault ``GOOGLE_CALENDAR_ID``): OmniFree writes one
  event per dated task there, instantly, and the MCP tool ``calendar_add_event`` adds events to it;
* their primary calendar (and any other) with "See all event details", listed in ``GOOGLE_CALENDAR_READ_IDS``
  (comma separated; empty = only ``GOOGLE_CALENDAR_ID``): OmniFree reads the agenda from them for the morning
  push, the reminders, the summary, the MCP tools and the PWA.

Vault keys: ``GOOGLE_CALENDAR_SA_JSON`` (the service-account key JSON, raw or base64), ``GOOGLE_CALENDAR_ID``,
``GOOGLE_CALENDAR_READ_IDS``. Without the key JSON everything here is a silent no-op.

Auth: an RS256 JWT (RFC 7523, signed by :mod:`plugins.gcp._auth`) is exchanged at
``https://oauth2.googleapis.com/token`` for an access token with the ``calendar`` scope, cached in-process until
a minute before it expires. Every call goes through ``httpx`` (``transport`` is injected by the tests).

Task events: the event id is ``"omnifree" + base32hex(task id)`` (lowercase, no padding: Google allows
``[a-v0-9]{5,1024}``), so a task maps to exactly one event without storing anything. Each one carries the
private extended properties ``omnifreeTaskId`` and ``omnifreeKind=task`` (the second one is what
``events.list(privateExtendedProperty=...)`` filters on when the reconcile looks for orphaned events). Events
OmniFree created (``omnifreeTaskId``) are left out of the agenda that is read back.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import httpx

from hub_api.appdata import AppDB, iso, local_tz, now_iso
from plugins.gcp._auth import DEFAULT_TOKEN_URI, build_jwt, parse_service_account

__all__ = [
    "SA_KEY", "ID_KEY", "READ_KEY", "SCOPE", "API", "TZ_NAME", "SCHEMA", "GCalConfig", "GCalError", "load_config",
    "event_id", "task_event", "ensure_schema", "push_task", "sync_all", "events_between", "agenda_day",
    "calendar_status", "add_event", "reset", "normalise_event", "transport",
]

log = logging.getLogger("omnifree.hub.gcal")

SA_KEY = "GOOGLE_CALENDAR_SA_JSON"
ID_KEY = "GOOGLE_CALENDAR_ID"
READ_KEY = "GOOGLE_CALENDAR_READ_IDS"
SCOPE = "https://www.googleapis.com/auth/calendar"
API = "https://www.googleapis.com/calendar/v3"
TZ_NAME = "Africa/Casablanca"
TASKS_PATH = "#/notes/tasks"
EVENT_MINUTES = 30  # a timed task is a 30-minute event...
POPUP_MINUTES = 10  # ...with a popup 10 minutes before (open tasks only)
HIGH_PRIORITY_COLOR = "11"  # "Tomato" in Google's event palette
HTTP_TIMEOUT_S = 10.0
PUSH_TIMEOUT_S = 4.0  # the task API never waits longer than this for Google
RECORD_TIMEOUT_S = 1.5  # writing a failure to app_gcal_sync after a timed-out push
EVENTS_CACHE_S = 300.0
DONE_DAYS = 14
ORPHAN_PAST_DAYS = 90  # the reconcile looks for orphaned task events in [today - 90 d, today + 2 y]
ORPHAN_FUTURE_DAYS = 731
_JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_gcal_sync ("
    " task_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, hash TEXT,"
    " synced_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_gcal_sync_status ON app_gcal_sync (status, synced_at)",
    "CREATE TABLE IF NOT EXISTS app_gcal_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
)

# Injected by tests (httpx.MockTransport); None = real network.
transport: httpx.AsyncBaseTransport | None = None

_tokens: dict[str, tuple[str, float]] = {}
_token_lock: asyncio.Lock | None = None
_events_cache: dict[str, tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = {}
_listed: set[str] = set()  # calendars already checked/added to the service account's calendarList


def reset() -> None:
    """Forget cached tokens and agenda reads (tests)."""
    global _token_lock
    _tokens.clear()
    _events_cache.clear()
    _listed.clear()
    _token_lock = None


class GCalError(RuntimeError):
    """Google refused a call or could not be reached."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GCalConfig:
    info: dict[str, Any] | None
    calendar_id: str | None
    read_ids: tuple[str, ...]
    error: str | None = None

    @property
    def configured(self) -> bool:
        """The key JSON parses: reading the agenda can work."""
        return self.info is not None

    @property
    def writable(self) -> bool:
        """Task events can be written (the key plus the OmniFree calendar id)."""
        return self.info is not None and bool(self.calendar_id)

    @property
    def sa_email(self) -> str | None:
        return str(self.info["client_email"]) if self.info else None

    @property
    def calendars(self) -> tuple[str, ...]:
        """The calendars the agenda is read from."""
        if self.read_ids:
            return self.read_ids
        return (self.calendar_id,) if self.calendar_id else ()


def load_config(secret: Any) -> GCalConfig:
    """``secret`` is ``Settings.secret`` (env first, then the vault)."""
    raw = (secret(SA_KEY) or "").strip()
    calendar_id = (secret(ID_KEY) or "").strip() or None
    read_ids = tuple(dict.fromkeys(p.strip() for p in (secret(READ_KEY) or "").split(",") if p.strip()))
    if not raw:
        return GCalConfig(None, calendar_id, read_ids, f"vault key {SA_KEY} is not set")
    try:
        info = parse_service_account(raw, "google_calendar")
    except Exception as exc:  # noqa: BLE001 - NotConfigured with the reason
        return GCalConfig(None, calendar_id, read_ids, str(exc))
    return GCalConfig(info, calendar_id, read_ids, None if calendar_id else f"vault key {ID_KEY} is not set")


def _config(state: Any) -> GCalConfig | None:
    settings = getattr(state, "settings", None)
    if settings is None:  # a route function called directly, without its HubState
        return None
    return load_config(settings.secret)


# --------------------------------------------------------------------------- HTTP


def _client(timeout: float = HTTP_TIMEOUT_S) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=timeout)


async def access_token(cfg: GCalConfig) -> str:
    """A bearer token for the service account, cached until a minute before it expires."""
    global _token_lock
    info = cfg.info
    if info is None:
        raise GCalError(f"{SA_KEY} is not set")
    key = f"{info['client_email']}|{SCOPE}"
    cached = _tokens.get(key)
    if cached and cached[1] - 60 > _time.time():
        return cached[0]
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    async with _token_lock:
        cached = _tokens.get(key)
        if cached and cached[1] - 60 > _time.time():
            return cached[0]
        token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
        now = int(_time.time())
        claims = {"iss": info["client_email"], "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600}
        assertion = build_jwt(info, claims)
        try:
            async with _client() as client:
                response = await client.post(token_uri, data={"grant_type": _JWT_GRANT, "assertion": assertion})
        except httpx.HTTPError as exc:
            raise GCalError(f"token exchange failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise GCalError(f"token exchange failed: HTTP {response.status_code} {response.text[:200]}",
                            response.status_code)
        body = response.json()
        token = body.get("access_token")
        if not token:
            raise GCalError("token endpoint returned no access_token")
        _tokens[key] = (str(token), _time.time() + float(body.get("expires_in", 3600)))
        return str(token)


async def call(cfg: GCalConfig, method: str, path: str, *, params: dict[str, Any] | None = None,
               json_body: dict[str, Any] | None = None, ok: tuple[int, ...] = ()) -> dict[str, Any] | None:
    """One authorized Calendar API call; None for an empty answer or an accepted error status in ``ok``."""
    headers = {"Authorization": f"Bearer {await access_token(cfg)}"}
    try:
        async with _client() as client:
            response = await client.request(method, f"{API}{path}", params=params, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise GCalError(f"{method} {path}: {type(exc).__name__}") from exc
    if response.status_code in ok:
        return None
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            message = response.text
        raise GCalError(f"HTTP {response.status_code}: {str(message)[:200]}", response.status_code)
    if not response.content:
        return None
    return response.json()


def _events_path(calendar_id: str, event: str | None = None) -> str:
    from urllib.parse import quote

    base = f"/calendars/{quote(calendar_id, safe='')}/events"
    return f"{base}/{quote(event, safe='')}" if event else base


# --------------------------------------------------------------------------- task -> event


def event_id(task_id: str) -> str:
    """Deterministic Google event id for a task: ``omnifree`` + lowercase unpadded base32hex of the id."""
    return "omnifree" + base64.b32hexencode(task_id.encode("utf-8")).decode("ascii").rstrip("=").lower()


def _valid_day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _valid_time(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        h, m = (int(x) for x in value.split(":")[:2])
        return time(h, m)
    except (TypeError, ValueError):
        return None


def task_event(row: dict[str, Any], link: str) -> dict[str, Any] | None:
    """The Google event body for a task row, or None when the task has no date (no event)."""
    day = _valid_day(row.get("due"))
    if day is None:
        return None
    done = bool(row.get("done"))
    at = _valid_time(row.get("due_time"))
    details = (row.get("details") or "").strip()
    from hub_api.taskplus import checklist_lines  # taskplus imports this module

    parts = [details, checklist_lines(row.get("checklist")), link]  # one event per occurrence, never an RRULE
    body: dict[str, Any] = {
        "id": event_id(str(row["id"])),
        "summary": f"✓ {row['title']}" if done else str(row["title"]),
        "description": "\n\n".join(p for p in parts if p),
        "status": "confirmed",
        "transparency": "transparent",
        "source": {"title": "OmniFree", "url": link},
        "extendedProperties": {"private": {"omnifreeTaskId": str(row["id"]), "omnifreeKind": "task"}},
    }
    if at is not None:
        start = datetime.combine(day, at)
        end = start + timedelta(minutes=EVENT_MINUTES)
        body["start"] = {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ_NAME}
    else:
        body["start"] = {"date": day.isoformat()}
        body["end"] = {"date": (day + timedelta(days=1)).isoformat()}
    if at is not None and not done:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": POPUP_MINUTES}]}
    else:
        body["reminders"] = {"useDefault": False}
    if row.get("priority") == "high" and not done:
        body["colorId"] = HIGH_PRIORITY_COLOR
    return body


def _hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]


async def ensure_schema(db: AppDB) -> AppDB:
    await db.ensure("gcal", SCHEMA)
    return db


async def _record(db: AppDB, task_id: str, status: str, error: str | None = None, digest: str | None = None) -> None:
    await ensure_schema(db)
    await db.execute(
        "INSERT INTO app_gcal_sync (task_id, event_id, status, error, hash, synced_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(task_id) DO UPDATE SET event_id = excluded.event_id, status = excluded.status,"
        " error = excluded.error, hash = excluded.hash, synced_at = excluded.synced_at",
        [task_id, event_id(task_id), status, (error or None) and error[:500], digest, now_iso()],
    )


async def _upsert(cfg: GCalConfig, body: dict[str, Any], *, insert_first: bool) -> None:
    """Write the event: PUT (full replace) when it may exist, POST (insert with our id) when it may not."""
    assert cfg.calendar_id
    put = _events_path(cfg.calendar_id, body["id"])
    if insert_first:
        created = await call(cfg, "POST", _events_path(cfg.calendar_id), json_body=body, ok=(409,))
        if created is not None:
            return
        await call(cfg, "PUT", put, json_body=body)  # 409: the id exists already (or did: a deleted event)
        return
    updated = await call(cfg, "PUT", put, json_body=body, ok=(404,))
    if updated is None:
        await call(cfg, "POST", _events_path(cfg.calendar_id), json_body=body)


async def _delete(cfg: GCalConfig, task_id: str) -> None:
    assert cfg.calendar_id
    await call(cfg, "DELETE", _events_path(cfg.calendar_id, event_id(task_id)), ok=(404, 410))


def tasks_link(state: Any) -> str:
    base = str(getattr(getattr(state, "hub", None), "pwa_base_url", "") or "").rstrip("/")
    return f"{base}/{TASKS_PATH}" if base else TASKS_PATH


async def _push(cfg: GCalConfig, db: AppDB, task_id: str, row: dict[str, Any] | None, link: str,
                *, created: bool) -> str:
    body = task_event(row, link) if row is not None else None
    if body is None:
        await ensure_schema(db)
        prev = await db.fetch_one("SELECT status FROM app_gcal_sync WHERE task_id = ?", [task_id])
        if prev is None or prev.get("status") == "deleted":
            return "skipped"  # never had an event (no date): nothing to delete
        await _delete(cfg, task_id)
        await _record(db, task_id, "deleted")
        return "deleted"
    await ensure_listed(cfg, timeout_s=1.5)
    await _upsert(cfg, body, insert_first=created)
    await _record(db, task_id, "synced", digest=_hash(body))
    return "synced"


async def push_task(state: Any, db: AppDB, task_id: str, row: dict[str, Any] | None, *,
                    created: bool = False) -> str | None:
    """Mirror one task to Google Calendar right now (``row`` None = the task is gone). Best effort: never raises,
    never waits more than :data:`PUSH_TIMEOUT_S`; a failure is logged and recorded in ``app_gcal_sync``.

    Returns "synced", "deleted", "skipped" (no date and no event), "failed", or None when Google Calendar is not
    set up.
    """
    try:
        cfg = _config(state)
        if cfg is None or not cfg.writable:
            return None
        if created and (row is None or task_event(row, "") is None):
            return None  # a new task without a date: nothing exists, nothing to do
        link = tasks_link(state)
        return await asyncio.wait_for(_push(cfg, db, task_id, row, link, created=created), PUSH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the task itself is saved; the reconcile retries later
        error = "timed out" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        log.warning("google calendar push for task %s failed: %s", task_id, error)
        try:
            await asyncio.wait_for(_record(db, task_id, "failed", error), RECORD_TIMEOUT_S)
        except Exception as rec_exc:  # noqa: BLE001
            log.warning("could not record the google calendar failure: %r", rec_exc)
        return "failed"


# --------------------------------------------------------------------------- reconcile


async def _state_get(db: AppDB, key: str) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT value FROM app_gcal_state WHERE key = ?", [key])
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _state_set(db: AppDB, key: str, value: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO app_gcal_state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [key, json.dumps(value, ensure_ascii=False), now_iso()],
    )


async def sync_all(state: Any, db: AppDB, *, budget_s: float = 45.0, full: bool = False) -> dict[str, Any]:
    """Reconcile the OmniFree calendar with the tasks, sequentially, inside ``budget_s`` seconds.

    1. upsert every open task with a date and every dated task done in the last 14 days (skipped when the last
       successful write had the same content, unless ``full``);
    2. delete the task events (``omnifreeKind=task``) whose task no longer exists or lost its date.

    Returns counts; ``remaining`` > 0 (or ``orphans_checked`` False) when the budget ran out first.
    """
    from hub_api.routers.notes import ensure_schema as ensure_notes  # notes imports this module

    deadline = _time.monotonic() + budget_s
    cfg = _config(state)
    out: dict[str, Any] = {"configured": bool(cfg and cfg.writable), "upserted": 0, "unchanged": 0, "deleted": 0,
                           "failed": 0, "remaining": 0, "orphans_checked": False, "errors": []}
    if cfg is None or not cfg.writable:
        out["skipped"] = (cfg.error if cfg else None) or "not configured"
        return out
    await ensure_notes(db)
    await ensure_schema(db)
    await ensure_listed(cfg)
    link = tasks_link(state)
    since = iso(datetime.now(timezone.utc) - timedelta(days=DONE_DAYS))
    rows = await db.fetch(
        "SELECT * FROM app_tasks WHERE due IS NOT NULL AND (done = 0 OR done_at >= ?) ORDER BY due, due_time", [since]
    )
    known = {r["task_id"]: r for r in await db.fetch("SELECT task_id, status, hash FROM app_gcal_sync", [])}
    started = _time.monotonic()
    for i, row in enumerate(rows):
        if _time.monotonic() > deadline:
            out["remaining"] = len(rows) - i
            break
        body = task_event(row, link)
        if body is None:
            continue
        digest = _hash(body)
        prev = known.get(row["id"])
        if not full and prev and prev.get("status") == "synced" and prev.get("hash") == digest:
            out["unchanged"] += 1
            continue
        try:
            await _upsert(cfg, body, insert_first=prev is None)
            await _record(db, row["id"], "synced", digest=digest)
            out["upserted"] += 1
        except Exception as exc:  # noqa: BLE001 - keep going, report it
            out["failed"] += 1
            out["errors"].append(f"{row['id']}: {exc}"[:300])
            await _record(db, row["id"], "failed", str(exc))
    if not out["remaining"] and _time.monotonic() < deadline:
        try:
            out["deleted"] = await _delete_orphans(cfg, db, deadline)
            out["orphans_checked"] = True
        except TimeoutError:
            pass
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"orphans: {exc}"[:300])
    out["elapsed_s"] = round(_time.monotonic() - started, 2)
    out["synced_at"] = now_iso()
    out["errors"] = out["errors"][:20]
    await _state_set(db, "last_sync", {k: out[k] for k in ("upserted", "unchanged", "deleted", "failed", "remaining",
                                                           "orphans_checked", "errors", "synced_at")})
    return out


async def _delete_orphans(cfg: GCalConfig, db: AppDB, deadline: float) -> int:
    assert cfg.calendar_id
    now = datetime.now(timezone.utc)
    params: dict[str, Any] = {
        "privateExtendedProperty": "omnifreeKind=task", "timeMin": iso(now - timedelta(days=ORPHAN_PAST_DAYS)),
        "timeMax": iso(now + timedelta(days=ORPHAN_FUTURE_DAYS)), "maxResults": 250, "showDeleted": "false",
    }
    events: list[dict[str, Any]] = []
    while True:
        if _time.monotonic() > deadline:
            raise TimeoutError
        page = await call(cfg, "GET", _events_path(cfg.calendar_id), params=params) or {}
        events += [e for e in page.get("items") or [] if e.get("status") != "cancelled"]
        token = page.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    ids = {str(((e.get("extendedProperties") or {}).get("private") or {}).get("omnifreeTaskId") or "") for e in events}
    ids.discard("")
    if not ids:
        return 0
    marks = ", ".join("?" for _ in ids)
    alive = {r["id"] for r in await db.fetch(f"SELECT id FROM app_tasks WHERE id IN ({marks}) AND due IS NOT NULL",
                                             sorted(ids))}
    deleted = 0
    for task_id in sorted(ids - alive):
        if _time.monotonic() > deadline:
            raise TimeoutError
        await _delete(cfg, task_id)
        await _record(db, task_id, "deleted")
        deleted += 1
    return deleted


# --------------------------------------------------------------------------- reading the agenda


def _parse_dt(value: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def normalise_event(item: dict[str, Any], calendar_id: str, calendar_name: str | None) -> dict[str, Any] | None:
    """``{id, calendar, calendar_id, title, start, end, all_day, location, link}``; times in Africa/Casablanca."""
    start, end = item.get("start") or {}, item.get("end") or {}
    if start.get("dateTime"):
        s = _parse_dt(str(start["dateTime"]))
        e = _parse_dt(str(end.get("dateTime") or start["dateTime"]))
        if s is None:
            return None
        tz = local_tz()
        start_out, end_out, all_day = s.astimezone(tz).isoformat(), (e or s).astimezone(tz).isoformat(), False
    elif start.get("date"):
        start_out, end_out, all_day = str(start["date"]), str(end.get("date") or start["date"]), True
    else:
        return None
    return {
        "id": str(item.get("id") or ""),
        "calendar": calendar_name or calendar_id,
        "calendar_id": calendar_id,
        "title": str(item.get("summary") or "(sans titre)"),
        "start": start_out,
        "end": end_out,
        "all_day": all_day,
        "location": item.get("location") or None,
        "link": item.get("htmlLink") or None,
    }


def _own(item: dict[str, Any]) -> bool:
    return bool(((item.get("extendedProperties") or {}).get("private") or {}).get("omnifreeTaskId"))


async def _list_calendar(cfg: GCalConfig, calendar_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeMin": iso(start), "timeMax": iso(end), "singleEvents": "true",
                              "orderBy": "startTime", "maxResults": 250, "timeZone": TZ_NAME}
    out: list[dict[str, Any]] = []
    for _ in range(4):  # 1,000 events at most
        page = await call(cfg, "GET", _events_path(calendar_id), params=params) or {}
        name = page.get("summary")
        for item in page.get("items") or []:
            if item.get("status") == "cancelled" or _own(item):
                continue
            ev = normalise_event(item, calendar_id, name)
            if ev is not None:
                out.append(ev)
        token = page.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return out


def _sort_key(ev: dict[str, Any]) -> tuple[str, int, str]:
    # all-day events first on their day, then by start time
    return (ev["start"][:10], 0 if ev["all_day"] else 1, ev["start"])


async def events_between(state: Any, start: datetime, end: datetime, *,
                         use_cache: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Agenda events overlapping [start, end) from the read calendars (OmniFree's own task events left out).

    Returns ``(events, errors)``; errors are per calendar (``{"calendar", "error"}``). Cached 5 minutes in memory.
    Raises nothing; an unconfigured hub answers ``([], [])``.
    """
    cfg = _config(state)
    if cfg is None or not cfg.configured or not cfg.calendars:
        return [], []
    key = f"{'|'.join(cfg.calendars)}:{iso(start)}:{iso(end)}"
    hit = _events_cache.get(key)
    if use_cache and hit and hit[0] > _time.monotonic():
        return list(hit[1]), list(hit[2])
    results = await asyncio.gather(*(_list_calendar(cfg, c, start, end) for c in cfg.calendars), return_exceptions=True)
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for cal, res in zip(cfg.calendars, results, strict=True):
        if isinstance(res, BaseException):
            errors.append({"calendar": cal, "error": str(res) or type(res).__name__})
        else:
            events += res
    events.sort(key=_sort_key)
    if not errors:
        _events_cache[key] = (_time.monotonic() + EVENTS_CACHE_S, events, errors)
        if len(_events_cache) > 64:
            _events_cache.pop(next(iter(_events_cache)))
    return events, errors


def day_bounds(day: date, days: int = 1) -> tuple[datetime, datetime]:
    tz = local_tz()
    start = datetime.combine(day, time(0), tz)
    return start, datetime.combine(day + timedelta(days=days), time(0), tz)


async def agenda_day(state: Any, day: date, timeout_s: float = 6.0) -> list[dict[str, Any]]:
    """Events of one local day, best effort (empty on any failure or timeout)."""
    try:
        events, _ = await asyncio.wait_for(events_between(state, *day_bounds(day)), timeout_s)
    except Exception as exc:  # noqa: BLE001
        log.info("google calendar agenda unavailable: %r", exc)
        return []
    return events


# --------------------------------------------------------------------------- status / creating events


async def _access_role(cfg: GCalConfig, calendar_id: str) -> dict[str, Any]:
    """The service account's role on a calendar (its calendarList entry; added to the list when shared but absent)."""
    from urllib.parse import quote

    path = f"/users/me/calendarList/{quote(calendar_id, safe='')}"
    try:
        entry = await call(cfg, "GET", path, ok=(404,))
        if entry is None:  # shared calendars do not show up in a service account's list on their own
            entry = await call(cfg, "POST", "/users/me/calendarList", json_body={"id": calendar_id})
    except GCalError as exc:
        reason = "not shared with the service account" if exc.status_code in (403, 404) else str(exc)
        return {"id": calendar_id, "access_role": None, "summary": None, "error": reason}
    entry = entry or {}
    return {"id": calendar_id, "access_role": entry.get("accessRole"), "summary": entry.get("summaryOverride")
            or entry.get("summary"), "error": None}


async def ensure_listed(cfg: GCalConfig, timeout_s: float = 5.0) -> None:
    """Once per process: put the configured calendars in the service account's calendarList.

    Calendars shared TO a service account do not appear in its list until it calls calendarList.insert; reads and
    writes use the calendar id directly and never depend on the list, so this is only for the roles in /gcal/status
    (and is best effort).
    """
    todo = [c for c in dict.fromkeys([*([cfg.calendar_id] if cfg.calendar_id else []), *cfg.calendars])
            if c not in _listed]
    if not todo or not cfg.configured:
        return
    try:
        roles = await asyncio.wait_for(asyncio.gather(*(_access_role(cfg, c) for c in todo)), timeout_s)
    except Exception as exc:  # noqa: BLE001
        log.info("calendarList check skipped: %r", exc)
        return
    _listed.update(r["id"] for r in roles if r.get("access_role"))


async def calendar_status(state: Any, db: AppDB) -> dict[str, Any]:
    cfg = _config(state)
    assert cfg is not None
    out: dict[str, Any] = {
        "configured": cfg.configured, "writable": cfg.writable, "sa_email": cfg.sa_email,
        "calendar_id": cfg.calendar_id, "read_ids": list(cfg.calendars), "error": cfg.error,
        "keys": {"sa_json": SA_KEY, "calendar_id": ID_KEY, "read_ids": READ_KEY},
        "calendars": [], "last_sync": None, "errors": [], "counts": {},
    }
    await ensure_schema(db)
    out["last_sync"] = await _state_get(db, "last_sync")
    out["errors"] = await db.fetch(
        "SELECT task_id, error, synced_at FROM app_gcal_sync WHERE status = 'failed' ORDER BY synced_at DESC LIMIT 10",
        [], limit=10,
    )
    counts = await db.fetch("SELECT status, COUNT(*) AS n FROM app_gcal_sync GROUP BY status", [])
    out["counts"] = {r["status"]: int(r["n"]) for r in counts}
    last = await db.fetch_one("SELECT MAX(synced_at) AS t FROM app_gcal_sync WHERE status != 'failed'", [])
    out["last_push_at"] = (last or {}).get("t")
    if not cfg.configured:
        return out
    ids = list(dict.fromkeys([*([cfg.calendar_id] if cfg.calendar_id else []), *cfg.calendars]))
    try:
        roles = await asyncio.wait_for(asyncio.gather(*(_access_role(cfg, c) for c in ids)), 8.0)
    except TimeoutError:
        roles = [{"id": c, "access_role": None, "summary": None, "error": "timed out"} for c in ids]
    except GCalError as exc:  # the token exchange itself failed
        roles = [{"id": c, "access_role": None, "summary": None, "error": str(exc)} for c in ids]
    _listed.update(r["id"] for r in roles if r.get("access_role"))
    for r in roles:
        r["write"] = r["id"] == cfg.calendar_id
        r["read"] = r["id"] in cfg.calendars
        needed = ("owner", "writer") if r["write"] else ("owner", "writer", "reader")
        r["ok"] = r.get("access_role") in needed
    out["calendars"] = roles
    return out


async def add_event(state: Any, *, title: str, start: datetime | date, end: datetime | date | None = None,
                    location: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Create an event in the OmniFree calendar (dates = all day; datetimes are local wall-clock times)."""
    cfg = _config(state)
    if cfg is None or not cfg.writable:
        raise GCalError(f"Google Calendar is not set up (vault keys {SA_KEY} and {ID_KEY})")
    body: dict[str, Any] = {"summary": title, "extendedProperties": {"private": {"omnifreeKind": "agent"}}}
    if isinstance(start, datetime):
        stop = end if isinstance(end, datetime) else start + timedelta(hours=1)
        if stop <= start:
            raise ValueError("end must be after start")
        body["start"] = {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": stop.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ_NAME}
    else:
        last = end if isinstance(end, date) and not isinstance(end, datetime) else start
        if last < start:
            raise ValueError("end must not be before start")
        body["start"] = {"date": start.isoformat()}
        body["end"] = {"date": (last + timedelta(days=1)).isoformat()}  # end date is exclusive
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    assert cfg.calendar_id
    created = await call(cfg, "POST", _events_path(cfg.calendar_id), json_body=body) or {}
    _events_cache.clear()
    ev = normalise_event(created, cfg.calendar_id, None)
    return ev or {"id": created.get("id"), "title": title}


# --------------------------------------------------------------------------- time blocks / voice appointments
# (event bodies are built in :mod:`hub_api.taskplus`; these only talk to Google)

OWN_KINDS = ("agent", "voice", "block")  # events OmniFree may delete again (task events go through push_task)


def writable(state: Any) -> bool:
    cfg = _config(state)
    return bool(cfg and cfg.writable)


async def busy_events(state: Any, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The agenda of [start, end) for free-slot search: the read calendars (as :func:`events_between`) plus the
    OmniFree calendar when it is not one of them (agent, voice and time-block events live there)."""
    cfg = _config(state)
    if cfg is None or not cfg.configured:
        return [], []
    events, errors = await events_between(state, start, end)
    if cfg.calendar_id and cfg.calendar_id not in cfg.calendars:
        try:
            events = events + await _list_calendar(cfg, cfg.calendar_id, start, end)
        except Exception as exc:  # noqa: BLE001
            errors = [*errors, {"calendar": cfg.calendar_id, "error": str(exc) or type(exc).__name__}]
    return events, errors


async def insert_event(state: Any, body: dict[str, Any]) -> dict[str, Any]:
    """POST an event body to the OmniFree calendar; the normalized event (see :func:`normalise_event`)."""
    cfg = _config(state)
    if cfg is None or not cfg.writable:
        raise GCalError(f"Google Calendar is not set up (vault keys {SA_KEY} and {ID_KEY})")
    assert cfg.calendar_id
    created = await call(cfg, "POST", _events_path(cfg.calendar_id), json_body=body) or {}
    _events_cache.clear()
    ev = normalise_event(created, cfg.calendar_id, None)
    return ev or {"id": str(created.get("id") or ""), "title": body.get("summary")}


async def delete_event(state: Any, event: str, *, kinds: tuple[str, ...] | None = None) -> bool:
    """Delete an event of the OmniFree calendar; False when it is already gone. With ``kinds``, only an event whose
    ``omnifreeKind`` is one of them (GCalError 403 otherwise), so a stray id never deletes the owner's events."""
    cfg = _config(state)
    if cfg is None or not cfg.writable:
        raise GCalError(f"Google Calendar is not set up (vault keys {SA_KEY} and {ID_KEY})")
    assert cfg.calendar_id
    path = _events_path(cfg.calendar_id, event)
    if kinds is not None:
        found = await call(cfg, "GET", path, ok=(404, 410))
        if found is None or found.get("status") == "cancelled":
            return False
        kind = ((found.get("extendedProperties") or {}).get("private") or {}).get("omnifreeKind")
        if kind not in kinds:
            raise GCalError("this event was not created by OmniFree", 403)
    try:
        await call(cfg, "DELETE", path)
    except GCalError as exc:
        if exc.status_code in (404, 410):
            return False
        raise
    finally:
        _events_cache.clear()
    return True
