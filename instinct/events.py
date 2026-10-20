"""Event fan-out for Instinct: local EventBus (service /ws) + hub_api callback (POST /agent/events)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from core.events import EventBus

__all__ = ["EventSink"]

log = logging.getLogger("omnifree.instinct.events")


class EventSink:
    """Publishes ``instinct.task.*`` events locally and forwards them to the hub in the background.

    Forwarding never blocks the agent: events go through a bounded queue and
    the oldest one is dropped when the hub is slow or unreachable.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        hub_url: str = "",
        hub_token: str | None = None,
        timeout_s: float = 10.0,
        queue_size: int = 500,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.bus = bus
        self.hub_url = hub_url.rstrip("/")
        self._hub_token = hub_token
        self._timeout_s = timeout_s
        self._transport = transport
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self.dropped = 0
        self.forwarded = 0

    @property
    def forwarding(self) -> bool:
        return bool(self.hub_url)

    async def start(self) -> None:
        if self.forwarding and self._worker is None:
            headers = {"Authorization": f"Bearer {self._hub_token}"} if self._hub_token else {}
            self._client = httpx.AsyncClient(
                base_url=self.hub_url, headers=headers, timeout=self._timeout_s, transport=self._transport
            )
            self._worker = asyncio.create_task(self._run())

    async def emit(self, type_: str, **data: Any) -> None:
        self.bus.publish(type_, provider="instinct", kind="agent", **data)
        if not self.forwarding:
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            self.dropped += 1
        self._queue.put_nowait({"type": type_, "provider": "instinct", "data": data})

    async def _run(self) -> None:
        assert self._client is not None
        while True:
            event = await self._queue.get()
            try:
                resp = await self._client.post("/agent/events", json=event)
                if resp.is_success:
                    self.forwarded += 1
                else:
                    log.warning("hub rejected %s: HTTP %s %s", event["type"], resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.warning("hub callback failed for %s: %r", event["type"], exc)
            finally:
                self._queue.task_done()

    async def flush(self, timeout_s: float = 5.0) -> None:
        if self._worker is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout_s)

    async def close(self) -> None:
        await self.flush()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
