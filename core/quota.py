"""Persistent usage counters (SQLite via aiosqlite) with minute/hour/day/month/total windows.

All windows are computed in UTC. A counter row is keyed by
(provider, metric, window, window_start); a new window simply starts a new
row, so "reset" is implicit and old rows are pruned on open().

``RedisQuotaStore`` is the shared (Upstash Redis) twin used when ``Settings.state_backend`` is
``upstash``: one key per (provider, metric, window, window_start) bumped with INCRBYFLOAT + EXPIRE,
so counters survive serverless cold starts and are shared between instances.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import aiosqlite

from core.interfaces import utcnow
from core.shared_state import KEY_PREFIX, SharedStateError, UpstashRest

if TYPE_CHECKING:
    from core.config import Settings

__all__ = [
    "WINDOWS",
    "window_start",
    "window_reset_at",
    "QuotaBackend",
    "QuotaStore",
    "RedisQuotaStore",
    "build_quota_store",
]

log = logging.getLogger("omnifree.quota")

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


class QuotaBackend(Protocol):
    """Public interface shared by QuotaStore (SQLite) and RedisQuotaStore (what SmartRouter uses)."""

    async def open(self) -> QuotaBackend: ...

    async def close(self) -> None: ...

    async def add(
        self, provider: str, metric: str, amount: float, windows: Iterable[str] = ..., now: datetime | None = ...
    ) -> None: ...

    async def used(self, provider: str, metric: str, window: str, now: datetime | None = ...) -> float: ...

    async def snapshot(self, provider: str | None = ..., now: datetime | None = ...) -> list[dict[str, object]]: ...

    async def reset(self, provider: str, metric: str | None = ...) -> None: ...


# Key lifetime per window: the window itself plus the retention QuotaStore._prune keeps.
_TTL_S: dict[str, int | None] = {
    "minute": 2 * 3600,
    "hour": 3 * 86400,
    "day": 63 * 86400,
    "month": 800 * 86400,
    "total": None,
}


class RedisQuotaStore:
    """Usage counters in Upstash Redis with the same interface as QuotaStore.

    Keys: ``omnifree:quota:<provider>:<metric>:<window>:<window_start>`` (float strings), plus
    ``omnifree:quota:idx:<provider>`` (set of ``metric|window``) and ``omnifree:quota:providers``
    so snapshot()/reset() never need SCAN. Reads and writes are fail-open: a Redis outage is
    logged and must not break the request being accounted (limits are then not enforced).
    """

    def __init__(self, client: UpstashRest, prefix: str = f"{KEY_PREFIX}:quota") -> None:
        self.client = client
        self.prefix = prefix

    async def open(self) -> RedisQuotaStore:
        return self

    async def close(self) -> None:
        """The REST client is shared with the rest of the hub; nothing to release here."""

    async def __aenter__(self) -> RedisQuotaStore:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ keys
    def key(self, provider: str, metric: str, window: str, now: datetime | None = None) -> str:
        return f"{self.prefix}:{provider}:{metric}:{window}:{window_start(window, now)}"

    def _index_key(self, provider: str) -> str:
        return f"{self.prefix}:idx:{provider}"

    @property
    def _providers_key(self) -> str:
        return f"{self.prefix}:providers"

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(value) if value is not None else 0.0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ interface
    async def add(
        self,
        provider: str,
        metric: str,
        amount: float,
        windows: Iterable[str] = ("day", "month"),
        now: datetime | None = None,
    ) -> None:
        """One /pipeline round trip: INCRBYFLOAT (+ EXPIRE) per window, then index the series."""
        now = now or utcnow()
        commands: list[list[object]] = []
        members: list[str] = []
        for window in sorted(set(windows)):
            key = self.key(provider, metric, window, now)
            commands.append(["INCRBYFLOAT", key, repr(float(amount))])
            ttl = _TTL_S[window]
            if ttl is not None:
                commands.append(["EXPIRE", key, ttl])
            members.append(f"{metric}|{window}")
        if not commands:
            return
        commands.append(["SADD", self._index_key(provider), *members])
        commands.append(["SADD", self._providers_key, provider])
        try:
            await self.client.pipeline(commands)
        except SharedStateError as exc:
            log.warning("quota counter update lost for %s/%s: %s", provider, metric, exc)

    async def used(
        self, provider: str, metric: str, window: str, now: datetime | None = None
    ) -> float:
        try:
            return self._float(await self.client.command("GET", self.key(provider, metric, window, now)))
        except SharedStateError as exc:
            log.warning("quota counter read failed for %s/%s/%s: %s", provider, metric, window, exc)
            return 0.0

    async def _series(self, provider: str | None) -> list[tuple[str, str, str]]:
        """(provider, metric, window) triples known to the index."""
        if provider is not None:
            providers = [provider]
        else:
            providers = sorted(await self.client.command("SMEMBERS", self._providers_key) or [])
        if not providers:
            return []
        indexes = await self.client.pipeline([["SMEMBERS", self._index_key(p)] for p in providers])
        out: list[tuple[str, str, str]] = []
        for prov, members in zip(providers, indexes, strict=True):
            for member in sorted(members or []):
                metric, _, window = str(member).rpartition("|")
                if metric and window in _TTL_S:
                    out.append((prov, metric, window))
        return out

    async def snapshot(
        self, provider: str | None = None, now: datetime | None = None
    ) -> list[dict[str, object]]:
        """Current-window counters (one dict per provider/metric/window)."""
        now = now or utcnow()
        try:
            series = await self._series(provider)
            if not series:
                return []
            values = await self.client.command("MGET", *(self.key(p, m, w, now) for p, m, w in series))
        except SharedStateError as exc:
            log.warning("quota snapshot failed: %s", exc)
            return []
        out: list[dict[str, object]] = []
        for (prov, metric, window), value in zip(series, values or [], strict=False):
            if value is None:
                continue
            out.append(
                {
                    "provider": prov,
                    "metric": metric,
                    "window": window,
                    "used": self._float(value),
                    "reset_at": window_reset_at(window, now),
                }
            )
        return out

    async def reset(self, provider: str, metric: str | None = None) -> None:
        """Delete the current-window counters (older windows expire on their own)."""
        series = [s for s in await self._series(provider) if metric is None or s[1] == metric]
        if not series:
            return
        await self.client.pipeline(
            [
                ["DEL", *(self.key(p, m, w) for p, m, w in series)],
                ["SREM", self._index_key(provider), *(f"{m}|{w}" for _, m, w in series)],
            ]
        )


def build_quota_store(settings: Settings, client: UpstashRest | None = None) -> QuotaStore | RedisQuotaStore:
    """RedisQuotaStore when a shared-state client is given, else the SQLite QuotaStore."""
    if client is not None:
        return RedisQuotaStore(client)
    return QuotaStore(settings.quota_db_path)
