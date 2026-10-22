"""Saved library storage: the D1 tables (items, collections) and the Turso vector table for "find it by describing it".

D1 (apps database, :mod:`hub_api.appdata`)
    ``app_saved``              one row per saved thing (link, text/quote, Drive file), deduped on ``canonical``
    ``app_saved_collections``  user collections (name, color dot, emoji); "Uncategorised" is implicit (NULL)

Turso (libSQL vectors, the same database as Ask my Drive but its own tables, so each keeps its embedding model)
    ``saved_vec_meta``    key/value: ``embedding_model``, ``embedding_provider``, ``dim``
    ``saved_vec_chunks``  up to :data:`MAX_CHUNKS` chunks per item with ``embedding F32_BLOB(dim)``; created on the
                          first index and dropped when the last chunk is gone so another model can be adopted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from core.interfaces import QueryResult
from hub_api.rag_store import EmbeddingMeta, SqlRunner, vector_literal

__all__ = ["SCHEMA", "SCHEMA_NAME", "ITEM_COLUMNS", "SavedVectors", "VecHit", "MAX_CHUNKS", "tags_of"]

SCHEMA_NAME = "saved_v1"
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_saved ("
    " id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT, canonical TEXT, title TEXT, description TEXT,"
    " site TEXT, site_name TEXT, favicon TEXT, image_url TEXT, thumb_key TEXT, thumb_provider TEXT,"
    " file_key TEXT, file_provider TEXT, mime TEXT, author TEXT, published_at TEXT, content_type TEXT,"
    " summary TEXT, tags TEXT, note TEXT, collection_id TEXT, suggested_collection TEXT, suggested_collection_id TEXT,"
    " body TEXT, lang TEXT, enrich_status TEXT, enrich_error TEXT, ai_source TEXT, indexed INTEGER NOT NULL DEFAULT 0,"
    " search_text TEXT, saved_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_saved_canonical ON app_saved (canonical)",
    "CREATE INDEX IF NOT EXISTS app_saved_saved_at ON app_saved (saved_at)",
    "CREATE INDEX IF NOT EXISTS app_saved_collection ON app_saved (collection_id)",
    "CREATE TABLE IF NOT EXISTS app_saved_collections ("
    " id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT, emoji TEXT, position INTEGER NOT NULL DEFAULT 0,"
    " created_at TEXT NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_saved_collections_name ON app_saved_collections (name COLLATE NOCASE)",
)
ITEM_COLUMNS: tuple[str, ...] = (
    "id", "kind", "url", "canonical", "title", "description", "site", "site_name", "favicon", "image_url", "thumb_key",
    "thumb_provider", "file_key", "file_provider", "mime", "author", "published_at", "content_type", "summary", "tags",
    "note", "collection_id", "suggested_collection", "suggested_collection_id", "body", "lang", "enrich_status",
    "enrich_error", "ai_source", "indexed", "search_text", "saved_at", "created_at", "updated_at",
)
MAX_CHUNKS = 8
INSERT_BATCH = 25


def tags_of(row: dict[str, Any]) -> list[str]:
    raw = row.get("tags")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(t) for t in value] if isinstance(value, list) else []


class VecHit:
    __slots__ = ("item_id", "ord", "text", "distance")

    def __init__(self, item_id: str, ord_: int, text: str, distance: float) -> None:
        self.item_id = item_id
        self.ord = ord_
        self.text = text
        self.distance = distance


def _rows(result: QueryResult) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, row, strict=False)) for row in result.rows]


class SavedVectors:
    """Chunk vectors of saved items on libSQL (``vector32`` / ``vector_distance_cos``)."""

    def __init__(self, db: SqlRunner, *, timeout_s: float = 30.0) -> None:
        self.db = db
        self.timeout_s = timeout_s

    async def _exec(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        return await asyncio.wait_for(self.db.execute(sql, params), self.timeout_s)

    async def _query(self, sql: str, params: list[Any] | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        return _rows(await asyncio.wait_for(self.db.query(sql, params, limit), self.timeout_s))

    async def ensure_schema(self) -> None:
        await self._exec("CREATE TABLE IF NOT EXISTS saved_vec_meta (k TEXT PRIMARY KEY, v TEXT)")

    async def meta(self) -> EmbeddingMeta | None:
        values = {r["k"]: r["v"] for r in await self._query("SELECT k, v FROM saved_vec_meta")}
        if not values.get("embedding_model") or not values.get("dim"):
            return None
        return EmbeddingMeta(model=str(values["embedding_model"]), provider=str(values.get("embedding_provider") or ""),
                             dim=int(values["dim"]))

    async def adopt(self, meta: EmbeddingMeta) -> None:
        await self._exec(
            "CREATE TABLE IF NOT EXISTS saved_vec_chunks (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
            f"ord INTEGER NOT NULL, text TEXT NOT NULL, embedding F32_BLOB({int(meta.dim)}))"
        )
        await self._exec("CREATE INDEX IF NOT EXISTS saved_vec_chunks_item ON saved_vec_chunks (item_id)")
        for k, v in (("embedding_model", meta.model), ("embedding_provider", meta.provider), ("dim", str(meta.dim))):
            await self._exec("INSERT OR REPLACE INTO saved_vec_meta (k, v) VALUES (?, ?)", [k, v])

    async def chunk_count(self) -> int:
        rows = await self._query("SELECT count(*) AS n FROM saved_vec_chunks")
        return int(rows[0]["n"] or 0) if rows else 0

    async def current_meta(self) -> EmbeddingMeta | None:
        """The model in use; forgotten (table dropped) once no chunk remains, so a new model can be adopted."""
        meta = await self.meta()
        if meta is not None and await self.chunk_count() == 0:
            await self._exec("DROP TABLE IF EXISTS saved_vec_chunks")
            await self._exec("DELETE FROM saved_vec_meta")
            return None
        return meta

    async def delete_item(self, item_id: str) -> None:
        if await self.meta() is not None:
            await self._exec("DELETE FROM saved_vec_chunks WHERE item_id = ?", [item_id])

    async def replace_item(self, item_id: str, texts: Sequence[str], vectors: Sequence[Sequence[float]]) -> None:
        await self._exec("DELETE FROM saved_vec_chunks WHERE item_id = ?", [item_id])
        rows = list(zip(texts, vectors, strict=True))
        for start in range(0, len(rows), INSERT_BATCH):
            batch = rows[start : start + INSERT_BATCH]
            values = ", ".join("(?, ?, ?, vector32(?))" for _ in batch)
            params: list[Any] = []
            for i, (text, vector) in enumerate(batch, start=start):
                params.extend([item_id, i, text, vector_literal(vector)])
            await self._exec(f"INSERT INTO saved_vec_chunks (item_id, ord, text, embedding) VALUES {values}", params)

    async def search(self, vector: Sequence[float], *, limit: int = 40) -> list[VecHit]:
        rows = await self._query(
            "SELECT item_id, ord, text, vector_distance_cos(embedding, vector32(?)) AS distance "
            "FROM saved_vec_chunks ORDER BY distance ASC LIMIT ?",
            [vector_literal(vector), int(limit)],
            limit=limit,
        )
        return [VecHit(str(r["item_id"]), int(r["ord"] or 0), str(r["text"] or ""),
                       float(r["distance"]) if r["distance"] is not None else 2.0) for r in rows]
