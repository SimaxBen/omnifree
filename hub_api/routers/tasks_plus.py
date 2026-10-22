"""/tasks/{id}/slots, /tasks/{id}/block, /tasks/{id}/checklist: time blocking and checklist items.

* ``GET /tasks/{id}/slots?days=7&minutes=``: the next free slots for the task (its ``estimate``, else 30 minutes)
  inside working hours (09:00-20:00 Africa/Casablanca), around the agenda (the read calendars and the OmniFree
  calendar), the timed open tasks and the other time blocks;
* ``POST /tasks/{id}/block`` ``{start?, minutes?}``: book that time in the OmniFree Google Calendar (``start`` is
  ``YYYY-MM-DDTHH:MM`` local time; without it the first free slot is taken). Blocking again moves the block. The
  event id is kept on the task (``block``); 409 when Google Calendar is not set up or nothing is free;
* ``DELETE /tasks/{id}/block``: delete the event and forget it;
* ``POST /tasks/{id}/checklist`` ``{text}`` adds an item; ``PATCH /tasks/{id}/checklist/{item}`` ``{text?, done?}``
  edits one; ``DELETE /tasks/{id}/checklist/{item}`` removes one (``PATCH /tasks/{id}`` with the whole ``checklist``
  does all of it, reordering included).

Google Calendar is reached through :mod:`hub_api.gcal` with bounded timeouts so a request stays well under 60 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from hub_api import gcal, taskplus
from hub_api.appdata import AppDB, get_appdb, local_now
from hub_api.routers import notes
from hub_api.state import HubState, get_state

__all__ = ["router"]

log = logging.getLogger("omnifree.hub.tasks_plus")

router = APIRouter(tags=["notes"])

MAX_DAYS = 14
GCAL_TIMEOUT_S = 8.0


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await notes.ensure_schema(db)


def _minutes(row: dict[str, Any], override: int | None) -> int:
    return int(override or row.get("estimate") or taskplus.DEFAULT_MINUTES)


async def _slots(state: HubState, db: AppDB, row: dict[str, Any], minutes: int, days: int,
                 limit: int = 8) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    now = local_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days + 1)
    busy, errors = await taskplus.busy_intervals(state, db, start, end, skip_task=row["id"],
                                                 skip_event=row.get("block_event_id"))
    found = taskplus.free_slots(busy, now=now, minutes=minutes, days=days, limit=limit)
    return [{"start": taskplus.fmt_local(s), "end": taskplus.fmt_local(e)} for s, e in found], errors


@router.get("/tasks/{task_id}/slots")
async def task_slots(
    task_id: str,
    days: int = Query(7, ge=1, le=MAX_DAYS),
    minutes: int | None = Query(None, ge=5, le=720),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Free slots for a time block; ``writable`` says whether ``POST .../block`` can book one."""
    row = await notes.get_task(db, task_id)
    length = _minutes(row, minutes)
    slots, errors = await _slots(state, db, row, length, days)
    return {"writable": gcal.writable(state), "minutes": length, "slots": slots, "errors": errors,
            "work_hours": [taskplus.WORK_START.strftime("%H:%M"), taskplus.WORK_END.strftime("%H:%M")]}


class BlockRequest(BaseModel):
    start: str | None = Field(None, description="YYYY-MM-DDTHH:MM, Africa/Casablanca; default: the first free slot")
    minutes: int | None = Field(None, ge=5, le=720, description="Default: the task's estimate, else 30")


def _not_set_up() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT,
                         detail="Google Calendar is not set up: share the OmniFree calendar first (More > Notifications)")


@router.post("/tasks/{task_id}/block")
async def block_task(
    task_id: str, body: BlockRequest, state: HubState = Depends(get_state), db: AppDB = Depends(_db)
) -> dict[str, Any]:
    row = await notes.get_task(db, task_id)
    if not gcal.writable(state):
        raise _not_set_up()
    length = _minutes(row, body.minutes)
    if body.start:
        try:
            start = taskplus.parse_local(body.start)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    else:
        slots, _ = await _slots(state, db, row, length, 7, limit=1)
        if not slots:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="no free slot in the next 7 days (09:00-20:00)")
        start = taskplus.parse_local(slots[0]["start"])
    end = start + timedelta(minutes=length)
    task = notes.task_out(row)
    event_body = taskplus.block_event(task, start, end, gcal.tasks_link(state))
    try:
        event = await asyncio.wait_for(gcal.insert_event(state, event_body), GCAL_TIMEOUT_S)
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail="Google Calendar did not answer in time") from exc
    except gcal.GCalError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    old = row.get("block_event_id")
    changes: dict[str, Any] = {"block_event_id": event["id"], "block_start": taskplus.fmt_local(start),
                               "block_end": taskplus.fmt_local(end)}
    if body.minutes and not row.get("estimate"):
        changes["estimate"] = body.minutes
    sets = ", ".join(f"{k} = ?" for k in changes)
    await db.execute(f"UPDATE app_tasks SET {sets} WHERE id = ?", [*changes.values(), task_id])
    if old and old != event["id"]:  # moved: the previous block goes
        await _delete_quietly(state, old)
    return {"task": notes.task_out(await notes.get_task(db, task_id)), "event": event}


async def _delete_quietly(state: HubState, event: str) -> None:
    try:
        await asyncio.wait_for(gcal.delete_event(state, event), GCAL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - an orphaned block is harmless, the task no longer points to it
        log.warning("could not delete the time block %s: %r", event, exc)


@router.delete("/tasks/{task_id}/block")
async def unblock_task(task_id: str, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    row = await notes.get_task(db, task_id)
    event = row.get("block_event_id")
    if event:
        if not gcal.writable(state):
            raise _not_set_up()
        try:
            await asyncio.wait_for(gcal.delete_event(state, event), GCAL_TIMEOUT_S)
        except TimeoutError as exc:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail="Google Calendar did not answer in time") from exc
        except gcal.GCalError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        await db.execute("UPDATE app_tasks SET block_event_id = NULL, block_start = NULL, block_end = NULL WHERE id = ?",
                         [task_id])
    return notes.task_out(await notes.get_task(db, task_id))


# --------------------------------------------------------------------------- checklist items


class ItemCreate(BaseModel):
    text: str = Field(min_length=1, max_length=taskplus.CHECKLIST_TEXT_MAX)


class ItemPatch(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=taskplus.CHECKLIST_TEXT_MAX)
    done: bool | None = None


async def _save_checklist(state: HubState, db: AppDB, task_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    clean = taskplus.normalize_checklist(items)
    await db.execute("UPDATE app_tasks SET checklist = ? WHERE id = ?",
                     [json.dumps(clean, ensure_ascii=False) if clean else None, task_id])
    task = notes.task_out(await notes.get_task(db, task_id))
    await gcal.push_task(state, db, task_id, task)  # the checklist is in the event description
    return task


@router.post("/tasks/{task_id}/checklist", status_code=status.HTTP_201_CREATED)
async def add_item(task_id: str, body: ItemCreate, state: HubState = Depends(get_state),
                   db: AppDB = Depends(_db)) -> dict[str, Any]:
    items = taskplus.checklist_of((await notes.get_task(db, task_id)).get("checklist"))
    if not body.text.strip():
        raise HTTPException(422, detail="text cannot be blank")
    if len(items) >= taskplus.CHECKLIST_MAX:
        raise HTTPException(422, detail=f"a checklist holds at most {taskplus.CHECKLIST_MAX} items")
    return await _save_checklist(state, db, task_id, [*items, {"text": body.text}])


@router.patch("/tasks/{task_id}/checklist/{item_id}")
async def patch_item(task_id: str, item_id: str, body: ItemPatch, state: HubState = Depends(get_state),
                     db: AppDB = Depends(_db)) -> dict[str, Any]:
    items = taskplus.checklist_of((await notes.get_task(db, task_id)).get("checklist"))
    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="checklist item not found")
    if body.text is not None:
        if not body.text.strip():
            raise HTTPException(422, detail="text cannot be blank")
        item["text"] = body.text
    if body.done is not None:
        item["done"] = body.done
    return await _save_checklist(state, db, task_id, items)


@router.delete("/tasks/{task_id}/checklist/{item_id}")
async def delete_item(task_id: str, item_id: str, state: HubState = Depends(get_state),
                      db: AppDB = Depends(_db)) -> dict[str, Any]:
    items = taskplus.checklist_of((await notes.get_task(db, task_id)).get("checklist"))
    kept = [i for i in items if i["id"] != item_id]
    if len(kept) == len(items):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="checklist item not found")
    return await _save_checklist(state, db, task_id, kept)
