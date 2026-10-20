"""/ws: WebSocket stream of the event bus (router.*, quota.*, breaker.*, registry.*, instinct.task.* ...).

Auth: ``?access_token=<hub token>`` (alias ``token``), an ``Authorization``
header, or the subprotocol pair ``["bearer", "<token>"]``.

Query parameters: ``types`` (comma separated patterns, default from
``OMNIFREE_WS_DEFAULT_TYPES``, ``*`` = everything) and ``replay`` (number of
recent matching events to send first).

Server -> client frames: ``{"type": "hello", ...}``, then each event as
``Event.model_dump(mode="json")`` (``id, type, ts, provider, kind, data``),
and ``{"type": "ping"}`` keepalives while idle.
Client -> server frames: ``{"op": "subscribe", "types": [...]}`` replaces the
filter, ``{"op": "ping"}`` is answered with ``{"type": "pong"}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from core.events import Event
from hub_api.auth import websocket_token_ok
from hub_api.state import HubState, get_ws_state

__all__ = ["router"]

log = logging.getLogger("omnifree.hub.ws")

router = APIRouter(tags=["ws"])


def _patterns(raw: Any) -> frozenset[str]:
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(t).strip() for t in raw]
    else:
        items = []
    return frozenset(t for t in items if t)


@router.websocket("/ws")
async def events_ws(websocket: WebSocket) -> None:
    state: HubState = get_ws_state(websocket)
    expected = state.hub_token()
    ok, subprotocol = websocket_token_ok(websocket, expected)
    if not ok:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unauthorized")
        return
    await websocket.accept(subprotocol=subprotocol)

    filt = {"patterns": _patterns(websocket.query_params.get("types")) or frozenset(state.hub.ws_default_types)}
    try:
        replay = max(0, min(int(websocket.query_params.get("replay", "0")), state.hub.ws_max_replay))
    except ValueError:
        replay = 0

    subscription = state.bus.subscribe(["*"])
    try:
        await websocket.send_json(
            {"type": "hello", "types": sorted(filt["patterns"]), "subscribers": state.bus.subscriber_count}
        )
        if replay:
            for event in state.bus.recent(replay, filt["patterns"]):
                await websocket.send_json(event.model_dump(mode="json"))

        async def pump() -> None:
            while True:
                try:
                    event: Event = await subscription.get(timeout=state.hub.ws_ping_interval_s)
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                if event.matches(filt["patterns"]):
                    await websocket.send_json(event.model_dump(mode="json"))

        async def reader() -> None:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == "subscribe":
                    filt["patterns"] = _patterns(message.get("types")) or frozenset({"*"})
                    await websocket.send_json({"type": "subscribed", "types": sorted(filt["patterns"])})
                elif op == "ping":
                    await websocket.send_json({"type": "pong"})

        tasks = {asyncio.create_task(pump()), asyncio.create_task(reader())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError, ValueError)):
                log.warning("websocket stream ended: %r", exc)
    except WebSocketDisconnect:
        log.debug("websocket client disconnected")
    finally:
        subscription.close()
        with contextlib.suppress(RuntimeError):
            await websocket.close()
