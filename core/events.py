"""In-process asyncio pub/sub event bus (streamed to the PWA by hub_api over WebSockets)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections import deque
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from core.interfaces import utcnow

__all__ = ["EventType", "Event", "EventBus", "Subscription"]


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


_ids = itertools.count(1)


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
    def __init__(self, history: int = 500) -> None:
        self._subs: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history)

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

    def subscribe(self, types: Iterable[str] | None = None, maxsize: int = 1000) -> Subscription:
        patterns = frozenset(str(t) for t in types) if types else None
        sub = Subscription(self, patterns, maxsize)
        self._subs.append(sub)
        return sub

    def recent(self, limit: int = 100, types: Iterable[str] | None = None) -> list[Event]:
        patterns = frozenset(str(t) for t in types) if types else None
        matched = [e for e in self._history if e.matches(patterns)]
        return matched[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def _remove(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)
