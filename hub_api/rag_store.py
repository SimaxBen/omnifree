"""Ask my Drive: vector store on libSQL (Turso) native vectors.

Tables (created idempotently):

* ``rag_docs``   one row per indexed Drive file (Doc of the API contract)
* ``rag_meta``   key/value: ``embedding_model``, ``embedding_provider``, ``dim``
* ``rag_chunks`` text chunks with ``embedding F32_BLOB(dim)``; created on the first index, when
  the embedding dimension is known, and dropped when the last document is removed so a new
  embedding model can be adopted.

Vectors are sent as JSON array text and converted server-side with ``vector32(?)``;
search is a brute-force ``vector_distance_cos`` scan (fine for a personal drive).

The store only needs an object with the DBAdapter ``execute``/``query`` coroutines, so tests
can back it with an in-memory SQLite that registers ``vector32``/``vector_distance_cos``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from core.interfaces import QueryResult

__all__ = ["RagStore", "SqlRunner", "EmbeddingMeta", "SearchHit", "vector_literal"]

DOCS_DDL = (
    "CREATE TABLE IF NOT EXISTS rag_docs ("
    "provider TEXT NOT NULL, key TEXT NOT NULL, title TEXT, mime TEXT, size INTEGER, "
    "chunks INTEGER NOT NULL DEFAULT 0, indexed_at TEXT, error TEXT, PRIMARY KEY (provider, key))"
)
META_DDL = "CREATE TABLE IF NOT EXISTS rag_meta (k TEXT PRIMARY KEY, v TEXT)"
INSERT_BATCH = 25  # rows per multi-row INSERT (one HTTP round trip each)

_DOC_COLUMNS = ("provider", "key", "title", "mime", "size", "chunks", "indexed_at", "error")


class SqlRunner(Protocol):
    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult: ...

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult: ...


@dataclass(frozen=True)
class EmbeddingMeta:
    model: str
    provider: str
    dim: int


@dataclass(frozen=True)
class SearchHit:
    provider: str
    key: str
    ord: int
    page: int | None
    text: str
    title: str | None
    distance: float


def vector_literal(vector: Sequence[float]) -> str:
    """JSON array text for ``vector32(?)`` (7 significant digits ~ float32 precision)."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


def _rows(result: QueryResult) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, row, strict=False)) for row in result.rows]


class RagStore:
    def __init__(self, db: SqlRunner, *, timeout_s: float = 30.0) -> None:
        self.db = db
        self.timeout_s = timeout_s

    async def _exec(self, sql: str, params: list[Any] | None = None) -> QueryResult:
        return await asyncio.wait_for(self.db.execute(sql, params), self.timeout_s)

    async def _query(self, sql: str, params: list[Any] | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        return _rows(await asyncio.wait_for(self.db.query(sql, params, limit), self.timeout_s))

    # ------------------------------------------------------------------ schema / meta
    async def ensure_schema(self) -> None:
        await self._exec(DOCS_DDL)
        await self._exec(META_DDL)

    async def meta(self) -> EmbeddingMeta | None:
        values = {r["k"]: r["v"] for r in await self._query("SELECT k, v FROM rag_meta")}
        if not values.get("embedding_model") or not values.get("dim"):
            return None
        return EmbeddingMeta(
            model=str(values["embedding_model"]),
            provider=str(values.get("embedding_provider") or ""),
            dim=int(values["dim"]),
        )

    async def adopt(self, meta: EmbeddingMeta) -> None:
        """Record the embedding model and create the chunk table for its dimension."""
        await self._exec(
            "CREATE TABLE IF NOT EXISTS rag_chunks ("
            "id INTEGER PRIMARY KEY, provider TEXT NOT NULL, key TEXT NOT NULL, ord INTEGER NOT NULL, "
            f"page INTEGER, text TEXT NOT NULL, embedding F32_BLOB({int(meta.dim)}))"
        )
        await self._exec("CREATE INDEX IF NOT EXISTS rag_chunks_doc ON rag_chunks (provider, key)")
        for k, v in (("embedding_model", meta.model), ("embedding_provider", meta.provider), ("dim", str(meta.dim))):
            await self._exec("INSERT OR REPLACE INTO rag_meta (k, v) VALUES (?, ?)", [k, v])

    async def reset_vectors(self) -> None:
        """Forget the embedding model (only done when no indexed document remains)."""
        await self._exec("DROP TABLE IF EXISTS rag_chunks")
        await self._exec("DELETE FROM rag_meta")

    # ------------------------------------------------------------------ docs
    async def list_docs(self) -> list[dict[str, Any]]:
        cols = ", ".join(_DOC_COLUMNS)
        return await self._query(f"SELECT {cols} FROM rag_docs ORDER BY indexed_at DESC, key")

    async def get_doc(self, provider: str, key: str) -> dict[str, Any] | None:
        cols = ", ".join(_DOC_COLUMNS)
        rows = await self._query(f"SELECT {cols} FROM rag_docs WHERE provider = ? AND key = ?", [provider, key])
        return rows[0] if rows else None

    async def upsert_doc(self, doc: dict[str, Any]) -> None:
        cols = ", ".join(_DOC_COLUMNS)
        marks = ", ".join("?" for _ in _DOC_COLUMNS)
        await self._exec(f"INSERT OR REPLACE INTO rag_docs ({cols}) VALUES ({marks})", [doc.get(c) for c in _DOC_COLUMNS])

    async def indexed_count(self) -> int:
        rows = await self._query("SELECT count(*) AS n FROM rag_docs WHERE chunks > 0")
        return int(rows[0]["n"] or 0) if rows else 0

    async def delete_doc(self, provider: str, key: str, *, has_chunks_table: bool) -> bool:
        existed = await self.get_doc(provider, key) is not None
        if has_chunks_table:
            await self._exec("DELETE FROM rag_chunks WHERE provider = ? AND key = ?", [provider, key])
        await self._exec("DELETE FROM rag_docs WHERE provider = ? AND key = ?", [provider, key])
        return existed

    # ------------------------------------------------------------------ chunks
    async def delete_chunks(self, provider: str, key: str) -> None:
        await self._exec("DELETE FROM rag_chunks WHERE provider = ? AND key = ?", [provider, key])

    async def insert_chunks(
        self,
        provider: str,
        key: str,
        chunks: Sequence[tuple[int, int | None, str]],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        rows = list(zip(chunks, vectors, strict=True))
        for start in range(0, len(rows), INSERT_BATCH):
            batch = rows[start : start + INSERT_BATCH]
            values = ", ".join("(?, ?, ?, ?, ?, vector32(?))" for _ in batch)
            params: list[Any] = []
            for (ord_, page, text), vector in batch:
                params.extend([provider, key, ord_, page, text, vector_literal(vector)])
            await self._exec(
                f"INSERT INTO rag_chunks (provider, key, ord, page, text, embedding) VALUES {values}", params
            )

    async def search(self, vector: Sequence[float], *, limit: int = 30, keys: Sequence[str] | None = None) -> list[SearchHit]:
        params: list[Any] = [vector_literal(vector)]
        where = ""
        if keys:
            where = "WHERE c.key IN (" + ", ".join("?" for _ in keys) + ")"
            params.extend(keys)
        params.append(int(limit))
        rows = await self._query(
            "SELECT c.provider AS provider, c.key AS key, c.ord AS ord, c.page AS page, c.text AS text, "
            "d.title AS title, vector_distance_cos(c.embedding, vector32(?)) AS distance "
            "FROM rag_chunks c LEFT JOIN rag_docs d ON d.provider = c.provider AND d.key = c.key "
            f"{where} ORDER BY distance ASC LIMIT ?",
            params,
            limit=limit,
        )
        return [
            SearchHit(
                provider=str(r["provider"]), key=str(r["key"]), ord=int(r["ord"] or 0),
                page=None if r["page"] is None else int(r["page"]), text=str(r["text"] or ""),
                title=r.get("title"), distance=float(r["distance"]) if r["distance"] is not None else 2.0,
            )
            for r in rows
        ]
