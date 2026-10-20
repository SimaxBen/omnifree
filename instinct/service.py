"""Instinct FastAPI service: task queue, REST API, WebSocket event stream.

Routes (all but ``/healthz`` need ``Authorization: Bearer <INSTINCT_API_TOKEN>``;
the WebSocket also accepts ``?access_token=``):

* ``POST /tasks`` -> 202 with the task summary (``goal``, ``start_url``, ``max_steps``, ``model``)
* ``GET /tasks`` / ``GET /tasks/{id}`` (with steps) / ``POST /tasks/{id}/cancel``
* ``GET /tasks/{id}/screenshot`` latest JPEG
* ``WS /ws`` streams ``instinct.task.*`` events (``Event.model_dump(mode="json")``)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status

from core.events import EventBus, EventType
from core.interfaces import utcnow
from instinct.agent import InstinctAgent
from instinct.browser import BrowserFactory, PlaywrightBrowserFactory
from instinct.config import InstinctSettings, get_instinct_settings, resolve_secret
from instinct.events import EventSink
from instinct.llm import AgentLLM, build_agent_llm
from instinct.models import TaskCreate, TaskState

__all__ = ["TaskManager", "create_app"]

log = logging.getLogger("omnifree.instinct")


class TaskManager:
    """Runs tasks with bounded concurrency; keeps the latest ``max_tasks_kept`` in memory."""

    def __init__(
        self,
        settings: InstinctSettings,
        llm: AgentLLM,
        browsers: BrowserFactory,
        sink: EventSink,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.browsers = browsers
        self.sink = sink
        self.tasks: OrderedDict[str, TaskState] = OrderedDict()
        self.screenshots: dict[str, bytes] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrent_tasks))
        self.agent = InstinctAgent(llm, settings, sink.emit, self._store_screenshot)

    def _store_screenshot(self, task_id: str, _step: int, image: bytes) -> None:
        self.screenshots[task_id] = image

    def _trim(self) -> None:
        while len(self.tasks) > self.settings.max_tasks_kept:
            for task_id, task in self.tasks.items():
                if task.status not in ("queued", "running"):
                    self.tasks.pop(task_id)
                    self.screenshots.pop(task_id, None)
                    break
            else:
                return

    async def submit(self, body: TaskCreate) -> TaskState:
        steps = min(body.max_steps or self.settings.max_steps, self.settings.hard_max_steps)
        task = TaskState(goal=body.goal, start_url=body.start_url, model=body.model, max_steps=steps)
        self.tasks[task.id] = task
        self._trim()
        await self.sink.emit(
            EventType.INSTINCT_TASK_CREATED, task_id=task.id, goal=task.goal, status=task.status,
            max_steps=task.max_steps, start_url=task.start_url,
        )
        self._runners[task.id] = asyncio.create_task(self._run(task), name=f"instinct-{task.id}")
        return task

    async def _run(self, task: TaskState) -> None:
        try:
            async with self._slots:
                session = await self.browsers.new_session()
                try:
                    await asyncio.wait_for(self.agent.run(task, session), self.settings.task_timeout_s)
                finally:
                    with contextlib.suppress(Exception):
                        await session.close()
        except asyncio.CancelledError:
            await self._fail(task, "cancelled", "cancelled")
        except TimeoutError:
            await self._fail(task, "failed", f"task timeout ({self.settings.task_timeout_s:g}s)")
        except Exception as exc:
            log.exception("task %s crashed", task.id)
            await self._fail(task, "failed", f"{type(exc).__name__}: {exc}"[:500])
        finally:
            self._runners.pop(task.id, None)

    async def _fail(self, task: TaskState, status_: str, error: str) -> None:
        if task.status in ("completed", "failed", "cancelled"):
            return
        task.status = status_  # type: ignore[assignment]
        task.error = error
        task.finished_at = utcnow()
        await self.sink.emit(
            EventType.INSTINCT_TASK_FAILED, task_id=task.id, status=status_, error=error, steps=len(task.steps)
        )

    async def cancel(self, task_id: str) -> TaskState:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        runner = self._runners.get(task_id)
        if runner is not None and not runner.done():
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runner
        return task

    async def wait(self, task_id: str, timeout_s: float = 30.0) -> TaskState:
        runner = self._runners.get(task_id)
        if runner is not None:
            await asyncio.wait_for(asyncio.shield(runner), timeout_s)
        return self.tasks[task_id]

    async def close(self) -> None:
        for task_id in list(self._runners):
            with contextlib.suppress(KeyError):
                await self.cancel(task_id)


def _token_ok(expected: str | None, presented: str | None) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def create_app(
    settings: InstinctSettings | None = None,
    *,
    browser_factory: BrowserFactory | None = None,
    llm: AgentLLM | None = None,
    api_token: str | None = None,
    hub_token: str | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    hub_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_instinct_settings()
    expected_token = api_token if api_token is not None else resolve_secret(settings.api_token_name)
    bus = EventBus()
    sink = EventSink(
        bus,
        hub_url=settings.hub_url,
        hub_token=hub_token if hub_token is not None else resolve_secret(settings.hub_token_name),
        timeout_s=settings.callback_timeout_s,
        queue_size=settings.callback_queue,
        transport=hub_transport,
    )
    agent_llm = llm or build_agent_llm(settings, transport=llm_transport)
    browsers = browser_factory or PlaywrightBrowserFactory(settings)
    manager = TaskManager(settings, agent_llm, browsers, sink)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await sink.start()
        if not expected_token:
            log.error("%s is not set: every protected route will answer 503", settings.api_token_name)
        try:
            yield
        finally:
            await manager.close()
            await sink.close()
            await browsers.close()
            await agent_llm.close()

    app = FastAPI(title="OmniFree Instinct Agent", version="0.1.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.bus = bus
    app.state.sink = sink

    async def require_token(request: Request) -> None:
        if not expected_token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{settings.api_token_name} not configured")
        presented = _bearer(request.headers.get("authorization"))
        if not _token_ok(expected_token, presented):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")

    def _task(task_id: str) -> TaskState:
        task = manager.tasks.get(task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown task '{task_id}'")
        return task

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        running = sum(1 for t in manager.tasks.values() if t.status == "running")
        queued = sum(1 for t in manager.tasks.values() if t.status == "queued")
        return {
            "ok": True,
            "running": running,
            "queued": queued,
            "max_concurrent": settings.max_concurrent_tasks,
            "hub_forwarding": sink.forwarding,
            "events_dropped": sink.dropped,
        }

    auth = [Depends(require_token)]

    @app.post("/tasks", status_code=status.HTTP_202_ACCEPTED, dependencies=auth)
    async def submit(body: TaskCreate) -> dict[str, Any]:
        task = await manager.submit(body)
        return task.summary()

    @app.get("/tasks", dependencies=auth)
    async def list_tasks() -> dict[str, Any]:
        return {"items": [t.summary() for t in reversed(manager.tasks.values())]}

    @app.get("/tasks/{task_id}", dependencies=auth)
    async def get_task(task_id: str) -> dict[str, Any]:
        return _task(task_id).model_dump(mode="json")

    @app.post("/tasks/{task_id}/cancel", dependencies=auth)
    async def cancel(task_id: str) -> dict[str, Any]:
        _task(task_id)
        task = await manager.cancel(task_id)
        return task.summary()

    @app.get("/tasks/{task_id}/screenshot", dependencies=auth)
    async def screenshot(task_id: str) -> Response:
        _task(task_id)
        image = manager.screenshots.get(task_id)
        if image is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no screenshot yet")
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def events_ws(websocket: WebSocket) -> None:
        presented = _bearer(websocket.headers.get("authorization")) or websocket.query_params.get("access_token")
        if not _token_ok(expected_token, presented):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
            return
        await websocket.accept()
        subscription = bus.subscribe(["instinct.*"])
        try:
            while True:
                try:
                    event = await subscription.get(timeout=25.0)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                await websocket.send_json(event.model_dump(mode="json"))
        except (WebSocketDisconnect, RuntimeError):
            log.debug("websocket client disconnected")
        finally:
            subscription.close()

    return app
