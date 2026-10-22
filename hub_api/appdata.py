"""Shared plumbing for the personal apps (notes, tasks, expenses, links, drops).

* :class:`AppDB` runs parameterised SQL on the apps database (Cloudflare D1 through the
  ``cloudflare_d1`` plugin by default, setting ``apps_db_provider``) and creates each
  schema idempotently (``CREATE TABLE IF NOT EXISTS``) once per process.
  The adapter source is injectable: anything with the DBAdapter ``query``/``execute``
  signatures returning :class:`core.interfaces.QueryResult` works (tests use SQLite).
* Small helpers shared by the app routers: ids, timestamps, the Africa/Casablanca
  "today", bounded upload reads and robust JSON extraction from LLM output.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Protocol

from fastapi import HTTPException, Request, UploadFile

from core.interfaces import QueryResult
from hub_api.state import HubState

__all__ = [
    "AppDB",
    "SQLAdapter",
    "get_appdb",
    "appdb_for",
    "new_id",
    "random_code",
    "now_iso",
    "iso",
    "local_tz",
    "local_now",
    "local_today",
    "read_upload",
    "extract_json",
    "BASE62",
]

BASE62 = string.digits + string.ascii_letters
_READ_CHUNK = 1024 * 1024


class SQLAdapter(Protocol):
    """The subset of :class:`core.interfaces.DBAdapter` the apps need."""

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult: ...

    async def execute(self, statement: str, params: list[Any] | dict[str, Any] | None = None) -> QueryResult: ...


AdapterSource = Callable[[], Awaitable[SQLAdapter]]

_MAX_ROWS = 10_000


class AppDB:
    """Parameterised SQL (``?`` placeholders) with lazily created schemas."""

    def __init__(self, source: AdapterSource, *, timeout_s: float = 30.0, hub_state: HubState | None = None) -> None:
        self._source = source
        self._timeout_s = timeout_s
        self.hub_state = hub_state  # the app's HubState, for hooks that push from an insert path (budget alerts)
        self._ready: set[str] = set()
        self._lock = asyncio.Lock()

    async def _adapter(self) -> SQLAdapter:
        return await self._source()

    async def ensure(self, name: str, statements: Sequence[str]) -> None:
        """Run ``statements`` (DDL) once per process for schema ``name``."""
        if name in self._ready:
            return
        async with self._lock:
            if name in self._ready:
                return
            adapter = await self._adapter()
            for stmt in statements:
                await asyncio.wait_for(adapter.execute(stmt, None), self._timeout_s)
            self._ready.add(name)

    async def add_columns(self, name: str, table: str, columns: Sequence[tuple[str, str]]) -> None:
        """Idempotent ``ALTER TABLE ... ADD COLUMN`` (SQLite/D1 have no ``IF NOT EXISTS`` for columns).

        Runs once per process for migration ``name``; a "duplicate column" error means the column is
        already there and is ignored, anything else propagates.
        """
        if name in self._ready:
            return
        async with self._lock:
            if name in self._ready:
                return
            adapter = await self._adapter()
            for column, decl in columns:
                try:
                    await asyncio.wait_for(adapter.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}", None), self._timeout_s)
                except TimeoutError:
                    raise
                except Exception as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            self._ready.add(name)

    async def fetch(self, sql: str, params: Sequence[Any] = (), *, limit: int = _MAX_ROWS) -> list[dict[str, Any]]:
        adapter = await self._adapter()
        result = await asyncio.wait_for(adapter.query(sql, list(params), limit), self._timeout_s)
        return [dict(zip(result.columns, row, strict=False)) for row in result.rows]

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.fetch(sql, params, limit=1)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a mutating statement; returns the affected row count (0 when unknown)."""
        adapter = await self._adapter()
        result = await asyncio.wait_for(adapter.execute(sql, list(params)), self._timeout_s)
        return int(result.affected_rows or 0)


def appdb_for(app: Any) -> AppDB:
    """The app-wide AppDB (created on first use and kept on ``app.state.appdb``).

    Shared by the HTTP dependency below and by callers outside a request (the MCP server).
    """
    db: AppDB | None = getattr(app.state, "appdb", None)
    if db is None:
        state: HubState = app.state.hub

        async def source() -> SQLAdapter:
            return await state.router.adapter(state.hub.apps_db_provider)  # type: ignore[return-value]

        db = AppDB(source, timeout_s=state.hub.db_timeout_s, hub_state=state)
        app.state.appdb = db
    return db


def get_appdb(request: Request) -> AppDB:
    """FastAPI dependency: one AppDB per app (override with ``app.dependency_overrides``)."""
    return appdb_for(request.app)


# --------------------------------------------------------------------------- ids and time


def random_code(length: int) -> str:
    return "".join(secrets.choice(BASE62) for _ in range(length))


def new_id() -> str:
    return random_code(12)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_LOCAL_TZ: tzinfo | None = None
LOCAL_TZ_SOURCE = "unloaded"  # tzdata-wheel | system | fixed-utc+1 (shown on /healthz)


def local_tz() -> tzinfo:
    """Africa/Casablanca, read from the ``tzdata`` package first.

    The system database can be years old (Vercel's runtime still has Morocco on UTC+0 in October),
    so the pinned ``tzdata`` wheel wins; then the system database; then a fixed UTC+1.
    """
    global _LOCAL_TZ, LOCAL_TZ_SOURCE
    if _LOCAL_TZ is None:
        try:
            from importlib import resources
            from zoneinfo import ZoneInfo

            with resources.files("tzdata.zoneinfo").joinpath("Africa/Casablanca").open("rb") as fh:
                _LOCAL_TZ = ZoneInfo.from_file(fh, key="Africa/Casablanca")
            LOCAL_TZ_SOURCE = "tzdata-wheel"
        except Exception as exc:  # noqa: BLE001 - tzdata wheel missing
            LOCAL_TZ_SOURCE = f"system ({type(exc).__name__})"
            try:
                from zoneinfo import ZoneInfo

                _LOCAL_TZ = ZoneInfo("Africa/Casablanca")
            except Exception:  # noqa: BLE001 - no tz database at all on slim images
                _LOCAL_TZ = timezone(timedelta(hours=1), "UTC+01")
                LOCAL_TZ_SOURCE = "fixed-utc+1"
    return _LOCAL_TZ


def local_now() -> datetime:
    return datetime.now(local_tz())


def local_today() -> date:
    return local_now().date()


# --------------------------------------------------------------------------- uploads / LLM output


async def read_upload(file: UploadFile, limit: int) -> bytes:
    """Read an upload fully, answering 413 past ``limit`` bytes and 422 when empty."""
    buf = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, detail=f"file exceeds {limit} bytes")
    if not buf:
        raise HTTPException(422, detail="empty file")
    return bytes(buf)


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: str | None) -> dict[str, Any] | None:
    """Best-effort JSON object from an LLM answer (code fences, surrounding prose)."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        try:
            value = json.loads(cand)
        except ValueError:
            start, end = cand.find("{"), cand.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                value = json.loads(cand[start : end + 1])
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return None
