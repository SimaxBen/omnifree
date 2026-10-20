"""In-process asyncio pub/sub event bus (streamed to the PWA by hub_api over WebSockets).

With a shared-state client (``Settings.state_backend == "upstash"``) every event is also pushed,
in the background and in batches, to a capped Redis list (``omnifree:events``) so that
``fetch_recent()`` (the PWA's ``/events/recent`` polling fallback) sees the events of every
serverless instance. Redis failures are logged and never reach the publisher.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from core.interfaces import utcnow
from core.shared_state import KEY_PREFIX, SharedStateError, UpstashRest

__all__ = ["EventType", "Event", "EventBus", "Subscription"]

log = logging.getLogger("omnifree.events")


class EventType(StrEnum):
    # router
    REQUEST_STARTED = "router.request"
    REQUEST_SUCCEEDED = "router.success"
    REQUEST_FAILED = "router.attempt_failed"
    FAILOVER = "router.failover"
    ALL_FAILED = "router.exhausted"
    # quota
    QUOTA_UPDATED = "quota.updated"
    QUOTA_EXHAUSTED = "quota.exhausted"
    # circuit breaker
    BREAKER_OPENED = "breaker.opened"
    BREAKER_HALF_OPEN = "breaker.half_open"
    BREAKER_CLOSED = "breaker.closed"
    # health
    HEALTH_CHECKED = "health.checked"
    # registry
    PLUGIN_LOADED = "registry.plugin_loaded"
    PLUGIN_LOAD_FAILED = "registry.plugin_load_failed"
    PLUGIN_ENABLED = "registry.enabled"
    PLUGIN_DISABLED = "registry.disabled"
    CHAIN_REORDERED = "registry.reordered"
    PLUGIN_UPDATED = "registry.updated"
    REGISTRY_RELOADED = "registry.reloaded"
    # storage
    OBJECT_STORED = "storage.stored"
    OBJECT_DELETED = "storage.deleted"
    # instinct agent (published by the instinct package; reserved names)
    INSTINCT_TASK_CREATED = "instinct.task.created"
    INSTINCT_TASK_STEP = "instinct.task.step"
    INSTINCT_TASK_SCREENSHOT = "instinct.task.screenshot"
    INSTINCT_TASK_COMPLETED = "instinct.task.completed"
    INSTINCT_TASK_FAILED = "instinct.task.failed"
    # dns / compute (published by hub_api after actions)
    DNS_BLOCKING_CHANGED = "dns.blocking_changed"
    COMPUTE_STATE_CHANGED = "compute.state_changed"


# Ids are seeded from the wall clock (ms * 1000 + jitter) so that events of different processes
# (serverless instances sharing one Redis list) do not collide; still < 2**53 for JS clients.
_ids = itertools.count(int(time.time() * 1000) * 1000 + random.randrange(1000))


class Event(BaseModel):
    id: int = Field(default_factory=lambda: next(_ids))
    type: str
    ts: datetime = Field(default_factory=utcnow)
    provider: str | None = None
    kind: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def matches(self, patterns: frozenset[str] | None) -> bool:
        """Pattern = exact type, a prefix ending in '.*' (e.g. 'router.*'), or '*'."""
        if not patterns or "*" in patterns:
            return True
        if self.type in patterns:
            return True
        return any(p.endswith(".*") and self.type.startswith(p[:-1]) for p in patterns)


class Subscription:
    """Async iterator over events. Use ``async with bus.subscribe(...) as sub``."""

    def __init__(self, bus: EventBus, patterns: frozenset[str] | None, maxsize: int) -> None:
        self._bus = bus
        self.patterns = patterns
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def _offer(self, event: Event) -> None:
        if self.closed or not event.matches(self.patterns):
            return
        if self.queue.full():  # slow consumer: drop the oldest event
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(event)

    async def get(self, timeout: float | None = None) -> Event:
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self) -> None:
        self.closed = True
        self._bus._remove(self)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while not self.closed:
            yield await self.queue.get()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(
        self,
        history: int = 500,
        *,
        shared: UpstashRest | None = None,
        shared_key: str = f"{KEY_PREFIX}:events",
        shared_timeout_s: float = 2.0,
    ) -> None:
        self._subs: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history)
        self.history_size = history
        self.shared = shared
        self.shared_key = shared_key
        self.shared_timeout_s = shared_timeout_s
        self._pending: list[Event] = []
        self._pusher: asyncio.Task[None] | None = None

    def publish(
        self,
        type: str | EventType,
        *,
        provider: str | None = None,
        kind: str | None = None,
        **data: Any,
    ) -> Event:
        """Non-blocking publish; safe to call from any coroutine on the loop."""
        event = Event(type=str(type), provider=provider, kind=kind, data=data)
        self.emit(event)
        return event

    def emit(self, event: Event) -> None:
        self._history.append(event)
        for sub in list(self._subs):
            sub._offer(event)
        if self.shared is not None:
            self._pending.append(event)
            self._schedule_push()

    def subscribe(self, types: Iterable[str] | None = None, maxsize: int = 1000) -> Subscription:
        patterns = frozenset(str(t) for t in types) if types else None
        sub = Subscription(self, patterns, maxsize)
        self._subs.append(sub)
        return sub

    def recent(self, limit: int = 100, types: Iterable[str] | None = None) -> list[Event]:
        patterns = frozenset(str(t) for t in types) if types else None
        matched = [e for e in self._history if e.matches(patterns)]
        return matched[-limit:]

    async def fetch_recent(self, limit: int = 100, types: Iterable[str] | None = None) -> list[Event]:
        """Like recent(), merged with the shared Redis list when configured (oldest first, deduped by id)."""
        if self.shared is None:
            return self.recent(limit, types)
        await self.flush()
        patterns = frozenset(str(t) for t in types) if types else None
        merged: dict[int, Event] = {e.id: e for e in self._history}
        try:
            raw = await asyncio.wait_for(
                self.shared.command("LRANGE", self.shared_key, 0, self.history_size - 1), self.shared_timeout_s
            )
        except (SharedStateError, TimeoutError) as exc:
            log.warning("shared event history unavailable: %s", exc)
            raw = []
        for item in raw or []:
            event = _decode(item)
            if event is not None:
                merged.setdefault(event.id, event)
        ordered = sorted(merged.values(), key=lambda e: (e.ts, e.id))
        return [e for e in ordered if e.matches(patterns)][-limit:]

    async def flush(self, timeout: float | None = None) -> None:
        """Wait (bounded) until pending events reached the shared list; never raises."""
        if self.shared is None:
            return
        if self._pending and (self._pusher is None or self._pusher.done()):
            self._schedule_push()
        task = self._pusher
        if task is None or task.done():
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout or self.shared_timeout_s)

    def _schedule_push(self) -> None:
        if self._pusher is not None and not self._pusher.done():
            return  # the running pusher drains new events too
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): pushed with the next event or flush()
        self._pusher = loop.create_task(self._push_pending())

    async def _push_pending(self) -> None:
        assert self.shared is not None
        while self._pending:
            batch, self._pending = self._pending, []
            commands: list[list[object]] = [
                ["LPUSH", self.shared_key, *(e.model_dump_json() for e in batch)],
                ["LTRIM", self.shared_key, 0, self.history_size - 1],
            ]
            try:
                await asyncio.wait_for(self.shared.pipeline(commands), self.shared_timeout_s)
            except Exception as exc:  # publishing must never fail because of Redis
                log.warning("dropped %d event(s) for the shared history: %s", len(batch), exc)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def _remove(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)


def _decode(item: object) -> Event | None:
    try:
        return Event.model_validate_json(item) if isinstance(item, (str, bytes)) else None
    except ValueError:
        return None
