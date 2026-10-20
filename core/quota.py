"""Persistent usage counters (SQLite via aiosqlite) with minute/hour/day/month/total windows.

All windows are computed in UTC. A counter row is keyed by
(provider, metric, window, window_start); a new window simply starts a new
row, so "reset" is implicit and old rows are pruned on open().
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from core.interfaces import utcnow

__all__ = ["WINDOWS", "window_start", "window_reset_at", "QuotaStore"]

WINDOWS = ("minute", "hour", "day", "month", "total")


def window_start(window: str, now: datetime | None = None) -> str:
    now = (now or utcnow()).astimezone(timezone.utc)
    if window == "minute":
        return now.strftime("%Y-%m-%dT%H:%M")
    if window == "hour":
        return now.strftime("%Y-%m-%dT%H")
    if window == "day":
        return now.strftime("%Y-%m-%d")
    if window == "month":
        return now.strftime("%Y-%m")
    if window == "total":
        return "all"
    raise ValueError(f"unknown quota window {window!r}")


def window_reset_at(window: str, now: datetime | None = None) -> datetime | None:
    now = (now or utcnow()).astimezone(timezone.utc)
    if window == "minute":
        return now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if window == "hour":
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if window == "month":
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first + timedelta(days=32)).replace(day=1)
    if window == "total":
        return None
    raise ValueError(f"unknown quota window {window!r}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    provider     TEXT NOT NULL,
    metric       TEXT NOT NULL,
    window       TEXT NOT NULL,
    window_start TEXT NOT NULL,
    used         REAL NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (provider, metric, window, window_start)
);
"""


class QuotaStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> QuotaStore:
        if self._db is not None:
            return self
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._prune()
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> QuotaStore:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("QuotaStore is not open; call await store.open()")
        return self._db

    async def _prune(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        keep = {
            "minute": window_start("minute", now - timedelta(hours=1)),
            "hour": window_start("hour", now - timedelta(days=2)),
            "day": window_start("day", now - timedelta(days=62)),
            "month": window_start("month", now - timedelta(days=800)),
        }
        for window, cutoff in keep.items():
            await self.db.execute(
                "DELETE FROM counters WHERE window = ? AND window_start < ?", (window, cutoff)
            )

    async def add(
        self,
        provider: str,
        metric: str,
        amount: float,
        windows: Iterable[str] = ("day", "month"),
        now: datetime | None = None,
    ) -> None:
        now = now or utcnow()
        stamp = now.isoformat()
        async with self._lock:
            for window in set(windows):
                await self.db.execute(
                    """
                    INSERT INTO counters(provider, metric, window, window_start, used, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, metric, window, window_start)
                    DO UPDATE SET used = used + excluded.used, updated_at = excluded.updated_at
                    """,
                    (provider, metric, window, window_start(window, now), float(amount), stamp),
                )
            await self.db.commit()

    async def used(
        self, provider: str, metric: str, window: str, now: datetime | None = None
    ) -> float:
        async with self.db.execute(
            "SELECT used FROM counters WHERE provider=? AND metric=? AND window=? AND window_start=?",
            (provider, metric, window, window_start(window, now)),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0

    async def snapshot(
        self, provider: str | None = None, now: datetime | None = None
    ) -> list[dict[str, object]]:
        """Current-window counters (one dict per provider/metric/window)."""
        now = now or utcnow()
        current = {w: window_start(w, now) for w in WINDOWS}
        sql = "SELECT provider, metric, window, window_start, used FROM counters"
        args: tuple[object, ...] = ()
        if provider is not None:
            sql += " WHERE provider = ?"
            args = (provider,)
        async with self.db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, object]] = []
        for prov, metric, window, start, used in rows:
            if current.get(window) != start:
                continue
            out.append(
                {
                    "provider": prov,
                    "metric": metric,
                    "window": window,
                    "used": float(used),
                    "reset_at": window_reset_at(window, now),
                }
            )
        return out

    async def reset(self, provider: str, metric: str | None = None) -> None:
        async with self._lock:
            if metric is None:
                await self.db.execute("DELETE FROM counters WHERE provider = ?", (provider,))
            else:
                await self.db.execute(
                    "DELETE FROM counters WHERE provider = ? AND metric = ?", (provider, metric)
                )
            await self.db.commit()
