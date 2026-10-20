"""/agent: submit and inspect Instinct tasks (proxied to the Instinct service) and ingest its events.

The Instinct service pushes its ``instinct.task.*`` events (including base64
JPEG screenshots in ``data.image_b64``) to ``POST /agent/events``; the hub
republishes them on the event bus, so every ``/ws`` client sees them live, and
keeps the latest frame per task for ``GET /agent/tasks/{id}/screenshot``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hub_api.state import HubState, get_state

__all__ = ["router", "TaskSubmit", "AgentEvent"]

router = APIRouter(prefix="/agent", tags=["agent"])

INSTINCT_PREFIX = "instinct.task."
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _task_path(task_id: str, suffix: str = "") -> str:
    """Instinct path for ``task_id``; refuses ids that could escape ``/tasks/`` (``..``, ``%2F`` ...)."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid task id")
    return f"/tasks/{task_id}{suffix}"


class TaskSubmit(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    start_url: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    model: str | None = None


class AgentEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None


async def _proxy(state: HubState, method: str, path: str, **kwargs: Any) -> httpx.Response:
    if not state.instinct.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="instinct_url not configured")
    try:
        return await state.instinct.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail=f"instinct timeout: {exc!r}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"instinct unreachable: {exc!r}") from exc


def _relay(resp: httpx.Response) -> Response:
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return JSONResponse(resp.json(), status_code=resp.status_code)
    return Response(resp.content, status_code=resp.status_code, media_type=content_type or None)


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED)
async def submit_task(body: TaskSubmit, state: HubState = Depends(get_state)) -> Response:
    resp = await _proxy(state, "POST", "/tasks", json=body.model_dump(exclude_none=True))
    if resp.is_success:
        data = resp.json()
        if isinstance(data, dict) and data.get("id"):
            state.agent_cache.update(f"{INSTINCT_PREFIX}created", {"task_id": data["id"], "goal": body.goal})
    return _relay(resp)


@router.get("/tasks")
async def list_tasks(state: HubState = Depends(get_state)) -> Response:
    return _relay(await _proxy(state, "GET", "/tasks"))


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, state: HubState = Depends(get_state)) -> Response:
    try:
        resp = await _proxy(state, "GET", _task_path(task_id))
    except HTTPException as exc:
        view = state.agent_cache.get(task_id)
        if view is None or exc.status_code not in (502, 504):
            raise
        # Instinct unreachable: serve the last state seen through the event callback.
        return JSONResponse(
            {
                "id": view.task_id,
                "status": view.status,
                "last_event": view.last_event,
                "has_screenshot": view.screenshot_b64 is not None,
                "stale": True,
            }
        )
    return _relay(resp)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, state: HubState = Depends(get_state)) -> Response:
    return _relay(await _proxy(state, "POST", _task_path(task_id, "/cancel")))


@router.get("/tasks/{task_id}/screenshot")
async def latest_screenshot(task_id: str, state: HubState = Depends(get_state)) -> Response:
    path = _task_path(task_id, "/screenshot")
    data = state.agent_cache.screenshot_bytes(task_id)
    if data is not None:
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    return _relay(await _proxy(state, "GET", path))


@router.get("/health")
async def instinct_health(state: HubState = Depends(get_state)) -> Response:
    return _relay(await _proxy(state, "GET", "/healthz"))


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    body: AgentEvent | list[AgentEvent], state: HubState = Depends(get_state)
) -> dict[str, int]:
    """Callback used by the Instinct service. Only ``instinct.task.*`` types are accepted."""
    events = body if isinstance(body, list) else [body]
    bad = [e.type for e in events if not e.type.startswith(INSTINCT_PREFIX)]
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"only {INSTINCT_PREFIX}* events accepted: {bad}")
    for event in events:
        state.agent_cache.update(event.type, event.data)
        payload = {k: v for k, v in event.data.items() if k not in ("type", "provider", "kind")}
        state.bus.publish(event.type, provider=event.provider or "instinct", kind="agent", **payload)
    return {"accepted": len(events)}
