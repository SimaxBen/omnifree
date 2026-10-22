"""Linked notes: backlinks, title autocomplete and ``[[link]]`` targets (see :mod:`hub_api.note_links`).

* ``GET /notes/{note_id}/backlinks``: the notes that link to this note's title (with a snippet around the link) and
  this note's own outgoing links, each resolved to a note id or ``null`` when no note has that title yet;
* ``GET /notes/links/titles?q=&limit=``: note titles for the editor's ``[[`` autocomplete (prefix matches first);
* ``POST /notes/links/open`` ``{"title", "create": true, "notebook"}``: the note a ``[[link]]`` points at (the most
  recently updated one with that title, case-insensitive), created empty with that title when missing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from hub_api import note_links
from hub_api.appdata import AppDB, get_appdb
from hub_api.routers import notes as notes_router

__all__ = ["router"]

router = APIRouter(tags=["notes"])

MAX_OUTGOING = 50  # outgoing links resolved per backlinks call


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await notes_router.ensure_schema(db)
    await note_links.ensure_index(db)
    return db


def _like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _by_title(db: AppDB, title: str) -> dict[str, Any] | None:
    """The most recently updated note whose title matches ``title`` (case-insensitive, whitespace collapsed)."""
    key = note_links.title_key(title)
    rows = await db.fetch(
        "SELECT * FROM app_notes WHERE lower(title) = lower(?) OR title LIKE ? ESCAPE '\\'"
        " ORDER BY updated_at DESC LIMIT 50",
        [title.strip(), _like(title.strip())],
    )
    for row in rows:
        if note_links.title_key(row["title"]) == key:
            return row
    return None


@router.get("/notes/links/titles")
async def note_titles(
    q: str = Query("", max_length=200), limit: int = Query(8, ge=1, le=50), db: AppDB = Depends(_db)
) -> dict[str, Any]:
    needle = q.strip()
    if needle:
        rows = await db.fetch(
            "SELECT id, title, notebook, updated_at FROM app_notes WHERE title LIKE ? ESCAPE '\\' AND trim(title) <> ''"
            " ORDER BY (title LIKE ? ESCAPE '\\') DESC, updated_at DESC LIMIT ?",
            [f"%{_like(needle)}%", f"{_like(needle)}%", limit],
            limit=limit,
        )
    else:
        rows = await db.fetch(
            "SELECT id, title, notebook, updated_at FROM app_notes WHERE trim(title) <> '' ORDER BY updated_at DESC LIMIT ?",
            [limit], limit=limit,
        )
    return {"titles": [{"id": r["id"], "title": r["title"], "notebook": r["notebook"]} for r in rows]}


class LinkOpen(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    create: bool = True
    notebook: str | None = Field(None, max_length=100)


@router.post("/notes/links/open")
async def open_link(body: LinkOpen, db: AppDB = Depends(_db)) -> dict[str, Any]:
    title = " ".join(body.title.split())
    if not title:
        raise HTTPException(422, detail="title cannot be blank")
    row = await _by_title(db, title)
    if row is not None:
        return {"note": notes_router._note_out(row), "created": False}
    if not body.create:
        raise HTTPException(404, detail=f"no note titled {title!r}")
    note = await notes_router._insert_note(
        db, body="", title=title, notebook=body.notebook, tags=[], pinned=False
    )
    return {"note": note, "created": True}


@router.get("/notes/{note_id}/backlinks")
async def backlinks(note_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    row = await db.fetch_one("SELECT id, title, body FROM app_notes WHERE id = ?", [note_id])
    if row is None:
        raise HTTPException(404, detail="note not found")
    key = note_links.title_key(row["title"])
    sources = await db.fetch(
        "SELECT n.id, n.title, n.notebook, n.body, n.updated_at FROM app_note_links l"
        " JOIN app_notes n ON n.id = l.src_id WHERE l.target_key = ? AND l.src_id != ? ORDER BY n.updated_at DESC"
        " LIMIT 200",
        [key, note_id],
    )
    back = [
        {"id": s["id"], "title": s["title"], "notebook": s["notebook"], "updated_at": s["updated_at"],
         "snippet": note_links.snippet(s.get("body") or "", row["title"])}
        for s in sources
    ]
    outgoing: list[dict[str, Any]] = []
    for title in note_links.extract_links(row.get("body") or "")[:MAX_OUTGOING]:
        target = await _by_title(db, title)
        outgoing.append({"title": title, "note_id": target["id"] if target else None})
    return {"note_id": note_id, "title": row["title"], "backlinks": back, "outgoing": outgoing}
