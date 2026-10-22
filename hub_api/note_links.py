"""Wiki links between notes: ``[[Note title]]`` (also ``[[Title|label]]`` and ``[[Title#section]]``) in note bodies.

The index table ``app_note_links`` holds one row per (source note, linked title); titles are matched
case-insensitively with collapsed whitespace (:func:`title_key`). It is rewritten for a note each time the note is
saved (:func:`index_note`, called by the notes router) and rebuilt once for the notes written before it existed
(:func:`ensure_index`). Backlinks of a note are the notes whose links point at its title. Links inside code
(fenced blocks, inline ``code``) are ignored, like the PWA renderer does.

``#tags`` written in a body (``#idea``, ``#projet-x``) are extracted by :func:`body_tags` with the same rules as the
PWA (a letter first, letters/digits/``_``/``-`` after, not inside a word or a URL).
"""

from __future__ import annotations

import logging
import re
import time as _time

from hub_api.appdata import AppDB, now_iso

__all__ = [
    "SCHEMA", "extract_links", "title_key", "body_tags", "ensure_index", "index_note", "unindex_note", "safe_index",
    "snippet",
]

log = logging.getLogger("omnifree.hub.note_links")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_note_links ("
    " src_id TEXT NOT NULL, target_key TEXT NOT NULL, target_title TEXT NOT NULL, created_at TEXT NOT NULL,"
    " PRIMARY KEY (src_id, target_key))",
    "CREATE INDEX IF NOT EXISTS app_note_links_target ON app_note_links (target_key)",
    "CREATE TABLE IF NOT EXISTS app_note_links_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
MAX_LINKS = 50  # per note
_ROWS_PER_INSERT = 20  # 4 parameters a row: D1 binds at most 100 per statement
INDEX_PAGE = 50  # notes per page of the one-off rebuild...
INDEX_BUDGET_S = 15.0  # ...which stops after this long and carries on at the next call (60 s serverless limit)
_INDEX_VERSION = "1"

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)", re.M | re.S)
_CODE_RE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_LINK_RE = re.compile(r"\[\[([^\[\]\n|#]{1,200})(?:#[^\[\]\n|]{0,200})?(?:\|[^\[\]\n]{0,200})?\]\]")
_TAG_RE = re.compile(r"(?<![\w&/#])#([^\W\d_][\w-]{0,39})", re.U)


def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().casefold()


def _strip_code(body: str) -> str:
    return _CODE_RE.sub(" ", _FENCE_RE.sub("\n", body.replace("\r\n", "\n")))


def extract_links(body: str) -> list[str]:
    """The linked titles of a body, in order, once each (first spelling kept), code ignored."""
    out: dict[str, str] = {}
    for m in _LINK_RE.finditer(_strip_code(body or "")):
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title and title_key(title) not in out:
            out[title_key(title)] = title
    return list(out.values())[:MAX_LINKS]


def body_tags(body: str) -> list[str]:
    """``#tags`` written in a body (lowercase, once each), ignoring code, headings' ``#`` and URL fragments."""
    text = re.sub(r"https?://\S+", " ", _strip_code(body or ""))
    out: list[str] = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(1).rstrip("-").lower()
        if tag and tag not in out:
            out.append(tag)
    return out


async def _ensure_schema(db: AppDB) -> None:
    await db.ensure("note_links", SCHEMA)


async def index_note(db: AppDB, note_id: str, body: str) -> None:
    """Rewrite the link rows of one note from its body."""
    await _ensure_schema(db)
    await db.execute("DELETE FROM app_note_links WHERE src_id = ?", [note_id])
    stamp = now_iso()
    titles = extract_links(body)
    for i in range(0, len(titles), _ROWS_PER_INSERT):  # a few multi-row statements, not one round trip per link
        chunk = titles[i:i + _ROWS_PER_INSERT]
        params: list[str] = []
        for title in chunk:
            params += [note_id, title_key(title), title[:200], stamp]
        await db.execute(
            "INSERT INTO app_note_links (src_id, target_key, target_title, created_at) VALUES "
            + ", ".join("(?, ?, ?, ?)" for _ in chunk) + " ON CONFLICT(src_id, target_key) DO NOTHING",
            params,
        )


async def unindex_note(db: AppDB, note_id: str) -> None:
    await _ensure_schema(db)
    await db.execute("DELETE FROM app_note_links WHERE src_id = ?", [note_id])


async def safe_index(db: AppDB, note_id: str, body: str | None) -> None:
    """:func:`index_note` (or :func:`unindex_note` when ``body`` is None) that never fails the note save."""
    try:
        if body is None:
            await unindex_note(db, note_id)
        else:
            await index_note(db, note_id, body)
    except Exception as exc:  # noqa: BLE001 - the index is rebuilt by ensure_index if it ever drifts
        log.warning("note link index update failed for %s: %r", note_id, exc)


async def _meta_set(db: AppDB, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO app_note_links_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [key, value],
    )


async def ensure_index(db: AppDB, budget_s: float | None = None) -> bool:
    """Index the notes written before the index existed, once. Notes must exist (notes schema).

    Runs in pages of :data:`INDEX_PAGE` notes in id order and saves the last id after each page, so a call stops
    after ``budget_s`` (:data:`INDEX_BUDGET_S`) and the next one carries on where it stopped instead of starting
    over. Returns whether the index is complete."""
    await _ensure_schema(db)
    meta = {r["key"]: r["value"] for r in await db.fetch(
        "SELECT key, value FROM app_note_links_meta WHERE key IN ('indexed', 'cursor')")}
    if meta.get("indexed") == _INDEX_VERSION:
        return True
    deadline = _time.monotonic() + (INDEX_BUDGET_S if budget_s is None else budget_s)
    cursor = meta.get("cursor") or ""
    while True:
        rows = await db.fetch(
            "SELECT id, body FROM app_notes WHERE id > ? AND body LIKE '%[[%' ORDER BY id LIMIT ?", [cursor, INDEX_PAGE]
        )
        for row in rows:
            await index_note(db, row["id"], row.get("body") or "")
        if len(rows) < INDEX_PAGE:
            await _meta_set(db, "indexed", _INDEX_VERSION)
            await db.execute("DELETE FROM app_note_links_meta WHERE key = 'cursor'")
            return True
        cursor = rows[-1]["id"]
        await _meta_set(db, "cursor", cursor)
        if _time.monotonic() >= deadline:
            log.info("note link index: rebuild paused after %s, continued at the next call", cursor)
            return False


def snippet(body: str, title: str, width: int = 140) -> str:
    """The text around the first ``[[title]]`` link in ``body`` (markup kept light), for the backlinks list."""
    key = title_key(title)
    for m in _LINK_RE.finditer(body or ""):
        if title_key(m.group(1)) == key:
            start = max(0, m.start() - width // 2)
            end = min(len(body), m.end() + width // 2)
            text = re.sub(r"\s+", " ", body[start:end]).strip()
            return ("…" if start else "") + text + ("…" if end < len(body) else "")
    return re.sub(r"\s+", " ", (body or "")[:width]).strip()
