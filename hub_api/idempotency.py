"""Idempotent creates, so the PWA's offline write queue can replay a request without duplicating it.

Two mechanisms, either one is enough:

* a client-supplied ``id`` in the JSON body of ``POST /tasks``, ``/notes``, ``/expenses`` and
  ``/money/incomes``: validated against :data:`CLIENT_ID_RE` and used as the row id; a replay finds
  the row and answers it again (200 instead of 201, ``Idempotent-Replayed: true``);
* an ``Idempotency-Key`` header (or an ``idempotency_key`` form field on multipart uploads such as
  ``POST /notes/voice``): the key is kept in ``app_idempotency`` for :data:`KEEP_DAYS` days and a
  replay with the same key and scope is answered instead of run again.

Only ids are kept, never content (note bodies, transcripts, amounts): a create replay answers the row
as it is now (410 when it was deleted since), a voice replay answers the ids it created.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response, status

from core.interfaces import ProviderError
from hub_api.appdata import AppDB, iso, new_id, now_iso

__all__ = [
    "SCHEMA", "CLIENT_ID_RE", "KEEP_DAYS", "REPLAY_HEADER", "check_client_id", "header_key", "check_key",
    "ensure_schema", "lookup", "remember", "create_once", "run_once", "ids_only",
]

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_idempotency ("
    " scope TEXT NOT NULL, key TEXT NOT NULL, resource_id TEXT, response TEXT NOT NULL,"
    " created_at TEXT NOT NULL, PRIMARY KEY (scope, key))",
)

# Same alphabet as appdata.new_id (base62) plus "_" and "-"; long enough that guessing is pointless.
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{8,200}$")  # visible ASCII, no spaces
KEEP_DAYS = 14
REPLAY_HEADER = "Idempotent-Replayed"

Fetch = Callable[[str], Awaitable[dict[str, Any] | None]]


def check_client_id(value: str | None) -> str | None:
    """A client id for a new row: None, or 8-64 characters of ``A-Z a-z 0-9 _ -`` (else 422)."""
    if value is None or value == "":
        return None
    if not CLIENT_ID_RE.match(value):
        raise HTTPException(422, detail="id must be 8-64 characters of A-Z a-z 0-9 _ -")
    return value


def check_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _KEY_RE.match(value):
        raise HTTPException(422, detail="Idempotency-Key must be 8-200 visible ASCII characters")
    return value


def header_key(request: Request) -> str | None:
    """FastAPI dependency: the validated ``Idempotency-Key`` header, if any."""
    return check_key(request.headers.get("idempotency-key"))


async def ensure_schema(db: AppDB) -> AppDB:
    await db.ensure("idempotency", SCHEMA)
    return db


async def lookup(db: AppDB, scope: str, key: str) -> dict[str, Any] | None:
    await ensure_schema(db)
    row = await db.fetch_one("SELECT resource_id, response FROM app_idempotency WHERE scope = ? AND key = ?", [scope, key])
    if row is None:
        return None
    try:
        response = json.loads(row["response"])
    except (TypeError, ValueError):
        response = None
    return {"resource_id": row.get("resource_id"), "response": response}


async def remember(db: AppDB, scope: str, key: str, response: dict[str, Any], resource_id: str | None = None) -> None:
    """Keep ``response`` for ``key`` (first write wins) and drop entries older than KEEP_DAYS."""
    await ensure_schema(db)
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS))
    await db.execute("DELETE FROM app_idempotency WHERE created_at < ?", [cutoff])
    await db.execute(
        "INSERT OR IGNORE INTO app_idempotency (scope, key, resource_id, response, created_at) VALUES (?, ?, ?, ?, ?)",
        [scope, key, resource_id, json.dumps(response, default=str), now_iso()],
    )


def _replayed(response: Response | None) -> None:
    if response is not None:
        response.status_code = status.HTTP_200_OK
        response.headers[REPLAY_HEADER] = "true"


async def create_once(
    db: AppDB,
    *,
    scope: str,
    client_id: str | None,
    key: str | None,
    fetch: Fetch,
    insert: Callable[[str], Awaitable[dict[str, Any]]],
    response: Response | None = None,
) -> dict[str, Any]:
    """Create a row once: ``insert(row_id)`` unless the key or the client id shows it already exists.

    ``fetch(row_id)`` answers the current row (in its API shape) or None. A replay answers the row as
    it is now with status 200, or 410 when it was deleted since (deleted content never comes back).
    """
    client_id = check_client_id(client_id)
    key = check_key(key)
    if key:
        hit = await lookup(db, scope, key)
        if hit is not None:
            current = await fetch(hit["resource_id"]) if hit["resource_id"] else None
            if current is None:
                raise HTTPException(status.HTTP_410_GONE, detail="created by an earlier request with this key, deleted since")
            _replayed(response)
            return current
    if client_id:
        existing = await fetch(client_id)
        if existing is not None:
            _replayed(response)
            return existing
    row_id = client_id or new_id()
    try:
        out = await insert(row_id)
    except ProviderError as exc:  # two replays raced on the primary key
        if client_id and "UNIQUE" in str(exc).upper():
            existing = await fetch(client_id)
            if existing is not None:
                _replayed(response)
                return existing
        raise
    if key:
        await remember(db, scope, key, {"id": row_id}, row_id)
    return out


def ids_only(out: dict[str, Any]) -> dict[str, Any]:
    """What a replay of a non-row operation answers: the ids it created, none of their content.

    Rows (objects with an ``id``) become ``{"id": ...}``; lists of rows keep only those; plain scalars
    other than text (``audio_key``, counts) stay; the transcript and any other text are blanked.
    """
    slim: dict[str, Any] = {"replayed": True}
    for k, v in out.items():
        if isinstance(v, dict):
            slim[k] = {"id": v["id"]} if "id" in v else None
        elif isinstance(v, list):
            slim[k] = [{"id": x["id"]} for x in v if isinstance(x, dict) and "id" in x]
        elif isinstance(v, str):
            slim[k] = v if k.endswith("_key") or k.endswith("_id") else ""
        else:
            slim[k] = v
    return slim


async def run_once(
    db: AppDB,
    *,
    scope: str,
    key: str | None,
    run: Callable[[], Awaitable[dict[str, Any]]],
    response: Response | None = None,
) -> dict[str, Any]:
    """Run a non-row operation (a voice upload) once per key; a replay answers :func:`ids_only` of it."""
    key = check_key(key)
    if key:
        hit = await lookup(db, scope, key)
        if hit is not None and hit["response"] is not None:
            _replayed(response)
            return hit["response"]
    out = await run()
    if key:
        await remember(db, scope, key, ids_only(out))
    return out
