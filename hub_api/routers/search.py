"""/search: one query across the personal apps and the Drive (the PWA's global search).

``GET /search?q=...&types=notes,tasks&limit=8`` answers typed results grouped per type, best group first:
notes (title, body), tasks (title, details), expenses (merchant, note, category), incomes (source, note,
category), Drive files (key/file name), saved AI chats (title, messages) and short links / file drops (title,
URL, code, file name).

Matching is accent- and case-insensitive (``Café`` matches ``cafe``): every query word must appear somewhere in
the item. Scoring is simple and deterministic: a hit in the title is worth more than one in the body, a whole
title / title prefix / word start more than a substring, and recent items win ties. Each group is capped at
``limit``; ``count`` is the number of matches before the cap.

D1 has no accent folding (``LIKE`` is ASCII-only case-insensitive), so the newest :data:`SCAN_ROWS` rows of each
table are scored in Python: plenty for one person's data, and every scan is bounded. A table that does not exist
yet just gives no results; a slow or failing source (the Drive listing across backends) is reported in ``errors``
without failing the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from hub_api.appdata import AppDB, get_appdb
from hub_api.state import HubState, get_state

__all__ = ["router", "TYPES", "fold", "Field", "score_item", "snippet", "query_terms"]

log = logging.getLogger("omnifree.hub.search")

router = APIRouter(tags=["search"])

TYPES: tuple[str, ...] = ("notes", "tasks", "expenses", "incomes", "files", "chats", "links")
LABELS = {"notes": "Notes", "tasks": "Tasks", "expenses": "Expenses", "incomes": "Income", "files": "Drive",
          "chats": "AI chats", "links": "Links & drops"}
SCAN_ROWS = 2000  # newest rows scored per table
CHAT_ROWS = 400  # chats carry their whole conversation: fewer rows
DRIVE_LIST_LIMIT = 5000
DRIVE_TIMEOUT_S = 8.0
SOURCE_TIMEOUT_S = 15.0
SNIPPET_CHARS = 140
MAX_TERMS = 8


# --------------------------------------------------------------------------- matching (pure)


def _fold_char(c: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", c) if not unicodedata.combining(ch)).lower()


def fold(text: str) -> str:
    """Lower case without accents: ``"Été à Fès"`` -> ``"ete a fes"``."""
    return "".join(_fold_char(c) for c in text)


def _fold_map(text: str) -> tuple[str, list[int]]:
    """The folded text and, for each folded character, the index of the original character it came from."""
    out: list[str] = []
    index: list[int] = []
    for i, c in enumerate(text):
        f = _fold_char(c)
        out.append(f)
        index.extend([i] * len(f))
    return "".join(out), index


def query_terms(q: str) -> list[str]:
    """Folded, de-duplicated query words (punctuation splits words), at most :data:`MAX_TERMS`."""
    terms: list[str] = []
    for t in re.split(r"[^\w@.+#-]+", fold(q)):
        t = t.strip(".-")
        if t and t not in terms:
            terms.append(t)
    return terms[:MAX_TERMS]


@dataclass(frozen=True)
class Field:
    text: str
    weight: float  # 3 = title, 1 = body
    title: bool = False


def _word_start(hay: str, pos: int) -> bool:
    return pos == 0 or not hay[pos - 1].isalnum()


def score_item(fields: Iterable[Field], terms: list[str]) -> float:
    """0 when a term is missing from every field; otherwise the sum of each term's best field score."""
    folded = [(fold(f.text or ""), f) for f in fields]
    total = 0.0
    for term in terms:
        best = 0.0
        for hay, f in folded:
            pos = hay.find(term)
            if pos < 0:
                continue
            s = 1.0
            if f.title:
                stripped = hay.strip()
                if stripped == term:
                    s = 4.0
                elif stripped.startswith(term):
                    s = 3.0
                elif _word_start(hay, pos):
                    s = 2.0
            elif _word_start(hay, pos):
                s = 1.5
            best = max(best, s * f.weight)
        if best == 0:
            return 0.0
        total += best
    if len(terms) > 1:
        whole = " ".join(terms)
        if any(f.title and whole in hay for hay, f in folded):
            total += 3.0  # the words in order in the title
    return total


def snippet(text: str, terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """About ``width`` characters of ``text`` around its first matching term (whitespace collapsed)."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    folded, index = _fold_map(flat)
    hits = [p for p in (folded.find(t) for t in terms) if p >= 0]
    if not hits:
        return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"
    at = index[min(hits)] if index else 0
    start = max(0, at - width // 3)
    if start > 0:
        space = flat.rfind(" ", 0, start + 1)
        start = space + 1 if space >= 0 and start - space < 20 else start
    end = min(len(flat), start + width)
    out = flat[start:end].strip()
    return ("…" if start > 0 else "") + out + ("…" if end < len(flat) else "")


def _recency(stamp: Any) -> float:
    """A tiny tie-breaker from an ISO date: newer items sort first among equal scores."""
    digits = re.sub(r"\D", "", str(stamp or ""))[:12]
    return int(digits) / 1e13 if digits else 0.0


# --------------------------------------------------------------------------- sources


async def _rows(db: AppDB, sql: str, limit: int = SCAN_ROWS) -> list[dict[str, Any]]:
    """Rows of one table; [] when the table does not exist yet (that app was never used)."""
    try:
        return await db.fetch(sql, [limit], limit=limit)
    except Exception as exc:  # noqa: BLE001 - a missing table means no results, not an error
        if "no such table" in str(exc).lower():
            return []
        raise


def _hit(kind: str, item_id: str, title: str, score: float, *, snippet_text: str = "", date: Any = None,
         data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": kind, "id": item_id, "title": title, "snippet": snippet_text, "date": date,
            "score": round(score + _recency(date), 6), "data": data or {}}


async def _notes(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    rows = await _rows(db, "SELECT id, title, body, notebook, pinned, updated_at FROM app_notes"
                           " ORDER BY updated_at DESC LIMIT ?")
    out = []
    for r in rows:
        s = score_item([Field(r["title"] or "", 3, True), Field(r.get("body") or "", 1),
                        Field(r.get("notebook") or "", 0.5)], terms)
        if s:
            out.append(_hit("notes", r["id"], r["title"] or "Untitled", s, snippet_text=snippet(r.get("body") or "", terms),
                            date=r.get("updated_at"), data={"notebook": r.get("notebook"), "pinned": bool(r.get("pinned"))}))
    return out


async def _tasks(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    rows = await _rows(db, "SELECT * FROM app_tasks ORDER BY created_at DESC LIMIT ?")
    out = []
    for r in rows:
        s = score_item([Field(r["title"] or "", 3, True), Field(r.get("details") or "", 1)], terms)
        if s:
            s *= 0.8 if r.get("done") else 1.0  # open tasks first
            out.append(_hit("tasks", r["id"], r["title"], s, snippet_text=snippet(r.get("details") or "", terms),
                            date=r.get("due") or r.get("created_at"),
                            data={"done": bool(r.get("done")), "due": r.get("due"), "due_time": r.get("due_time"),
                                  "priority": r.get("priority") or "normal"}))
    return out


async def _expenses(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    from hub_api.routers.expenses import expense_out

    rows = await _rows(db, "SELECT * FROM app_expenses ORDER BY date DESC, created_at DESC LIMIT ?")
    out = []
    for r in rows:
        items = r.get("items") or ""
        s = score_item([Field(r.get("merchant") or "", 3, True), Field(r.get("note") or "", 1),
                        Field(r.get("category") or "", 1), Field(items, 0.5)], terms)
        if s:
            e = expense_out(r)
            out.append(_hit("expenses", r["id"], e["merchant"] or e["category"], s,
                            snippet_text=snippet(r.get("note") or "", terms), date=r.get("date"), data=e))
    return out


async def _incomes(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    from hub_api.routers.money import _income_out

    rows = await _rows(db, "SELECT * FROM app_incomes ORDER BY date DESC, created_at DESC LIMIT ?")
    out = []
    for r in rows:
        s = score_item([Field(r.get("source") or "", 3, True), Field(r.get("note") or "", 1),
                        Field(r.get("category") or "", 1)], terms)
        if s:
            i = _income_out(r)
            out.append(_hit("incomes", r["id"], i.get("source") or i.get("category") or "Income", s,
                            snippet_text=snippet(r.get("note") or "", terms), date=r.get("date"), data=i))
    return out


def _chat_text(raw: Any) -> str:
    try:
        messages = json.loads(raw or "[]")
    except ValueError:
        return ""
    if not isinstance(messages, list):
        return ""
    return "\n".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))


async def _chats(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    rows = await _rows(db, "SELECT id, title, messages, message_count, updated_at FROM app_chats"
                           " ORDER BY updated_at DESC LIMIT ?", CHAT_ROWS)
    out = []
    for r in rows:
        text = _chat_text(r.get("messages"))
        s = score_item([Field(r.get("title") or "", 3, True), Field(text, 1)], terms)
        if s:
            out.append(_hit("chats", r["id"], r.get("title") or "Chat", s, snippet_text=snippet(text, terms),
                            date=r.get("updated_at"), data={"message_count": int(r.get("message_count") or 0)}))
    return out


async def _links(db: AppDB, terms: list[str]) -> list[dict[str, Any]]:
    out = []
    for r in await _rows(db, "SELECT code, url, title, clicks, created_at FROM app_links ORDER BY created_at DESC LIMIT ?"):
        s = score_item([Field(r.get("title") or "", 3, True), Field(r.get("url") or "", 1.5), Field(r["code"], 1)], terms)
        if s:
            out.append(_hit("links", f"link:{r['code']}", r.get("title") or r["url"], s, snippet_text=r["url"],
                            date=r.get("created_at"), data={"kind": "link", "code": r["code"], "url": r["url"]}))
    for r in await _rows(db, "SELECT code, provider, key, filename, created_at, expires_at FROM app_drops"
                             " ORDER BY created_at DESC LIMIT ?"):
        s = score_item([Field(r.get("filename") or "", 3, True), Field(r["code"], 1)], terms)
        if s:
            out.append(_hit("links", f"drop:{r['code']}", r["filename"], s, snippet_text=f"File drop · {r['key']}",
                            date=r.get("created_at"),
                            data={"kind": "drop", "code": r["code"], "provider": r["provider"], "key": r["key"]}))
    return out


async def _files(state: HubState, terms: list[str]) -> list[dict[str, Any]]:
    raw = await asyncio.wait_for(state.router.storage_list("", limit=DRIVE_LIST_LIMIT), DRIVE_TIMEOUT_S)
    out = []
    seen: set[tuple[str, str]] = set()
    for name, result in raw.items():
        if isinstance(result, str):
            log.info("search: storage %s unavailable: %s", name, result)
            continue
        for o in result:
            obj = o.model_dump(mode="json")
            key = str(obj.get("key") or "")
            if not key or (key, obj.get("backend") or name) in seen:
                continue
            seen.add((key, obj.get("backend") or name))
            base = key.rsplit("/", 1)[-1]
            s = score_item([Field(base, 3, True), Field(key, 1)], terms)
            if s:
                folder = key[: -len(base)].rstrip("/")
                out.append(_hit("files", f"{obj.get('backend') or name}:{key}", base, s, snippet_text=folder,
                                date=obj.get("last_modified"), data=obj))
    return out


# --------------------------------------------------------------------------- route


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    types: str | None = Query(None, description="Comma-separated subset of: " + ", ".join(TYPES)),
    limit: int = Query(8, ge=1, le=50, description="Results per type"),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(get_appdb),
) -> dict[str, Any]:
    terms = query_terms(q)
    wanted = [t.strip() for t in (types or "").split(",") if t.strip()] or list(TYPES)
    unknown = sorted(set(wanted) - set(TYPES))
    if unknown:
        raise HTTPException(422, detail=f"unknown types: {', '.join(unknown)}")
    if not terms:
        return {"query": q, "terms": [], "total": 0, "groups": [], "errors": {}}

    sources: dict[str, Callable[[], Awaitable[list[dict[str, Any]]]]] = {
        "notes": lambda: _notes(db, terms),
        "tasks": lambda: _tasks(db, terms),
        "expenses": lambda: _expenses(db, terms),
        "incomes": lambda: _incomes(db, terms),
        "files": lambda: _files(state, terms),
        "chats": lambda: _chats(db, terms),
        "links": lambda: _links(db, terms),
    }
    found: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    async def one(kind: str) -> None:
        try:
            found[kind] = await asyncio.wait_for(sources[kind](), SOURCE_TIMEOUT_S)
        except TimeoutError:
            errors[kind] = "timed out"
        except Exception as exc:  # noqa: BLE001 - one failing source must not hide the others
            log.warning("search source %s failed: %r", kind, exc)
            errors[kind] = f"{type(exc).__name__}: {str(exc)[:200]}"

    await asyncio.gather(*(one(k) for k in TYPES if k in wanted))
    groups = []
    for kind in TYPES:
        hits = sorted(found.get(kind) or [], key=lambda h: -h["score"])
        if hits:
            groups.append({"type": kind, "label": LABELS[kind], "count": len(hits), "items": hits[:limit]})
    groups.sort(key=lambda g: -g["items"][0]["score"])
    return {"query": q, "terms": terms, "total": sum(g["count"] for g in groups), "groups": groups, "errors": errors}
