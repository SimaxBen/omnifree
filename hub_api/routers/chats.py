"""/chats: saved AI conversations (the PWA's chat history), synced across devices through D1.

One row per conversation in ``app_chats``; the messages are a JSON array in the row, so reading or
continuing a conversation is one query. ``PUT /chats/{id}`` is an idempotent upsert with a
client-chosen id: the PWA keeps an unsynced conversation locally and simply re-sends it until the
hub accepts it. ``title_source`` records where the title came from (``auto`` = first user message,
``llm`` = ``POST /chats/{id}/title``, ``user`` = renamed); a user title is never overwritten.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from core.interfaces import ChatMessage, ChatRequest, ProviderError
from hub_api.appdata import AppDB, get_appdb, new_id, now_iso
from hub_api.state import HubState, get_state

__all__ = ["router", "SCHEMA", "chat_title", "clean_llm_title"]

log = logging.getLogger("omnifree.hub.chats")

router = APIRouter(tags=["chats"])

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_chats ("
    " id TEXT PRIMARY KEY, title TEXT NOT NULL, title_source TEXT NOT NULL DEFAULT 'auto',"
    " model TEXT, messages TEXT NOT NULL DEFAULT '[]', message_count INTEGER NOT NULL DEFAULT 0,"
    " preview TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_chats_updated ON app_chats (pinned, updated_at)",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_MAX_MESSAGES = 1000
# D1 caps a row at 2 MB; keep the JSON well under it.
_MAX_JSON_BYTES = 1_800_000
_TITLE_LIMIT = 60
_LLM_TITLE_TIMEOUT_S = 20.0
NEW_CHAT_TITLE = "New chat"


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await db.ensure("chats", SCHEMA)
    return db


# --------------------------------------------------------------------------- models


class ChatTurn(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=200_000)
    routed_via: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=200)
    created_at: str | None = Field(None, max_length=40)


class ChatSave(BaseModel):
    """Body of ``POST /chats`` and ``PUT /chats/{id}``: the whole conversation."""

    title: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=200)
    messages: list[ChatTurn] = Field(default_factory=list, max_length=_MAX_MESSAGES)
    pinned: bool | None = None


class ChatPatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    model: str | None = Field(None, max_length=200)
    pinned: bool | None = None
    append: list[ChatTurn] | None = Field(None, max_length=_MAX_MESSAGES)


# --------------------------------------------------------------------------- helpers


def _shorten(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def chat_title(messages: list[dict[str, Any]], limit: int = _TITLE_LIMIT) -> str:
    """The first user message, whitespace collapsed and shortened; ``New chat`` when there is none."""
    for m in messages:
        if m.get("role") == "user":
            text = re.sub(r"[*_`#>]+", "", str(m.get("content") or ""))
            text = _shorten(text, limit)
            if text:
                return text
    return NEW_CHAT_TITLE


def clean_llm_title(raw: str | None, limit: int = _TITLE_LIMIT) -> str | None:
    """A model's title answer reduced to a bare title (no quotes, ``Title:`` prefix, final dot)."""
    if not raw:
        return None
    line = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")
    line = re.sub(r"^\s*(?:title|titre)\s*:\s*", "", line, flags=re.I)
    quotes = "\"'«»“”‘’"
    line = re.sub(r"[*_`#]+", "", line).strip().strip(quotes).rstrip(".!:;,").strip().strip(quotes).strip()
    return _shorten(line, limit) if line else None


def _preview(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip():
            return _shorten(str(m["content"]), 140)
    return ""


def _turns_json(turns: list[ChatTurn]) -> list[dict[str, Any]]:
    return [t.model_dump(exclude_none=True) for t in turns]


def _encode(messages: list[dict[str, Any]]) -> str:
    if len(messages) > _MAX_MESSAGES:
        raise HTTPException(413, detail="conversation too long: start a new chat")
    text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) > _MAX_JSON_BYTES:
        raise HTTPException(413, detail="conversation too long: start a new chat")
    return text


def _decode(raw: Any) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [m for m in value if isinstance(m, dict)] if isinstance(value, list) else []


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "title_source": row.get("title_source") or "auto",
        "model": row.get("model"),
        "message_count": int(row.get("message_count") or 0),
        "preview": row.get("preview") or "",
        "pinned": bool(row.get("pinned")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _full(row: dict[str, Any]) -> dict[str, Any]:
    return {**_summary(row), "messages": _decode(row.get("messages"))}


def _check_id(chat_id: str) -> str:
    if not _ID_RE.fullmatch(chat_id):
        raise HTTPException(422, detail="chat id must be 6-64 characters of A-Z, a-z, 0-9, '_' or '-'")
    return chat_id


async def _find(db: AppDB, chat_id: str) -> dict[str, Any] | None:
    return await db.fetch_one("SELECT * FROM app_chats WHERE id = ?", [chat_id])


async def _get(db: AppDB, chat_id: str) -> dict[str, Any]:
    row = await _find(db, chat_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="chat not found")
    return row


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _content_fields(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"messages": _encode(messages), "message_count": len(messages), "preview": _preview(messages)}


async def _insert(db: AppDB, chat_id: str, body: ChatSave) -> dict[str, Any]:
    messages = _turns_json(body.messages)
    title = (body.title or "").strip()
    now = now_iso()
    row = {
        "id": chat_id,
        "title": _shorten(title, 300) if title else chat_title(messages),
        "title_source": "user" if title else "auto",
        "model": body.model or None,
        **_content_fields(messages),
        "pinned": 1 if body.pinned else 0,
        "created_at": now,
        "updated_at": now,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_chats ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return row


async def _update(db: AppDB, chat_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    if changes:
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_chats SET {sets} WHERE id = ?", [*changes.values(), chat_id])
    return await _get(db, chat_id)


# --------------------------------------------------------------------------- routes


@router.get("/chats")
async def list_chats(
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Conversations without their messages, pinned first then most recently updated."""
    sql = (
        "SELECT id, title, title_source, model, message_count, preview, pinned, created_at, updated_at"
        " FROM app_chats"
    )
    params: list[Any] = []
    if q and q.strip():
        like = _like(q.strip())
        sql += " WHERE (title LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\')"
        params += [like, like]
    sql += " ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ?"
    rows = await db.fetch(sql, [*params, limit], limit=limit)
    return {"chats": [_summary(r) for r in rows]}


@router.post("/chats", status_code=status.HTTP_201_CREATED)
async def create_chat(body: ChatSave, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _full(await _insert(db, new_id(), body))


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _full(await _get(db, chat_id))


@router.put("/chats/{chat_id}")
async def save_chat(chat_id: str, body: ChatSave, response: Response, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Create or replace a conversation under a client-chosen id (idempotent, safe to retry)."""
    _check_id(chat_id)
    current = await _find(db, chat_id)
    if current is None:
        response.status_code = status.HTTP_201_CREATED
        return _full(await _insert(db, chat_id, body))
    messages = _turns_json(body.messages)
    changes: dict[str, Any] = _content_fields(messages)
    title = (body.title or "").strip()
    if title:
        changes["title"] = _shorten(title, 300)
        changes["title_source"] = "user"
    elif (current.get("title_source") or "auto") == "auto":
        changes["title"] = chat_title(messages)
    if body.model is not None:
        changes["model"] = body.model or None
    if body.pinned is not None:
        changes["pinned"] = 1 if body.pinned else 0
    return _full(await _update(db, chat_id, changes))


@router.patch("/chats/{chat_id}")
async def patch_chat(chat_id: str, body: ChatPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Rename, pin, set the model, and/or append messages."""
    current = await _get(db, chat_id)
    changes: dict[str, Any] = {}
    fields = body.model_fields_set
    if "title" in fields:
        title = (body.title or "").strip()
        if title:
            changes["title"] = _shorten(title, 300)
            changes["title_source"] = "user"
        else:  # a blank title goes back to the automatic one
            changes["title"] = chat_title(_decode(current.get("messages")))
            changes["title_source"] = "auto"
    if "pinned" in fields:
        if body.pinned is None:
            raise HTTPException(422, detail="pinned cannot be null")
        changes["pinned"] = 1 if body.pinned else 0
    if "model" in fields:
        changes["model"] = body.model or None
    if body.append:
        messages = _decode(current.get("messages")) + _turns_json(body.append)
        changes.update(_content_fields(messages))
        if (changes.get("title_source") or current.get("title_source") or "auto") == "auto":
            changes["title"] = chat_title(messages)
    return _full(await _update(db, chat_id, changes))


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(chat_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, chat_id)
    await db.execute("DELETE FROM app_chats WHERE id = ?", [chat_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_TITLE_PROMPT = (
    "Write a short title (3 to 6 words) for the conversation below, in the language of the user's message. "
    "Answer with the title only: no quotes, no 'Title:', no final punctuation."
)


@router.post("/chats/{chat_id}/title")
async def auto_title(chat_id: str, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Ask the LLM pool for a short title (one small request). A renamed chat is left alone, and any
    failure (no provider, timeout, empty answer) keeps the current title: the reply is the chat either way."""
    current = await _get(db, chat_id)
    if (current.get("title_source") or "auto") == "user":
        return _full(current)
    messages = _decode(current.get("messages"))
    first_user = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "").strip()
    if not first_user:
        return _full(current)
    first_reply = next((str(m.get("content") or "") for m in messages if m.get("role") == "assistant"), "").strip()
    excerpt = f"User: {first_user[:1200]}"
    if first_reply:
        excerpt += f"\n\nAssistant: {first_reply[:800]}"
    request = ChatRequest(
        messages=[ChatMessage(role="system", content=_TITLE_PROMPT), ChatMessage(role="user", content=excerpt)],
        temperature=0.2,
        max_tokens=24,
    )
    try:
        resp = await asyncio.wait_for(state.router.chat(request), _LLM_TITLE_TIMEOUT_S)
    except (ProviderError, TimeoutError) as exc:
        log.info("chat title generation skipped: %r", exc)
        return _full(current)
    title = clean_llm_title(resp.content)
    if not title:
        return _full(current)
    return _full(await _update(db, chat_id, {"title": title, "title_source": "llm"}))
