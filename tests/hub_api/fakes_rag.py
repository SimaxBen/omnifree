"""Test-only fakes for Ask my Drive: a libSQL-like DB on in-memory SQLite and a deterministic LLM.

``FakeTurso`` registers ``vector32(json)`` and ``vector_distance_cos(a, b)`` on sqlite3 so the
real ``RagStore`` SQL (``F32_BLOB``, ``vector32(?)``, ``vector_distance_cos``) runs unchanged.
``FakeRagLLM`` embeds with a hashed bag of words, reranks by word overlap and records chat prompts.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import zlib
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    DBAdapter,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    LLMAdapter,
    ModelInfo,
    ProviderError,
    ProviderUnavailable,
    QueryResult,
    QuotaInfo,
    RerankRequest,
    RerankResponse,
    RerankResult,
    TableInfo,
    Usage,
)

DBS: dict[str, sqlite3.Connection] = {}
SQL_LOG: list[str] = []
EMBED = {"model": "fake-embed", "dim": 64, "calls": 0, "inputs": 0}
CHATS: list[ChatRequest] = []
RERANKS: list[RerankRequest] = []
_WORD_RE = re.compile(r"[a-z0-9]+")


def reset() -> None:
    for conn in DBS.values():
        conn.close()
    DBS.clear()
    SQL_LOG.clear()
    EMBED.update(model="fake-embed", dim=64, calls=0, inputs=0)
    CHATS.clear()
    RERANKS.clear()


def _vector32(text: str) -> bytes:
    values = json.loads(text)
    return struct.pack(f"<{len(values)}f", *values)


def _distance_cos(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        raise ValueError("vector dimensions differ")
    n = len(a) // 4
    va, vb = struct.unpack(f"<{n}f", a), struct.unpack(f"<{n}f", b)
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    na, nb = math.sqrt(sum(x * x for x in va)), math.sqrt(sum(y * y for y in vb))
    return 1.0 - dot / (na * nb) if na and nb else 1.0


def _connect(name: str) -> sqlite3.Connection:
    conn = DBS.get(name)
    if conn is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.create_function("vector32", 1, _vector32, deterministic=True)
        conn.create_function("vector_distance_cos", 2, _distance_cos, deterministic=True)
        DBS[name] = conn
    return conn


class FakeTurso(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})

    @property
    def conn(self) -> sqlite3.Connection:
        return _connect(self.name)

    def _run(self, statement: str, params: Any) -> QueryResult:
        SQL_LOG.append(statement)
        try:
            cur = self.conn.execute(statement, params or [])
        except sqlite3.Error as exc:
            raise ProviderError(str(exc), provider=self.name, status_code=400) from exc
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        self.conn.commit()
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), affected_rows=cur.rowcount, backend=self.name)

    async def query(self, statement: str, params: Any = None, limit: int = 500) -> QueryResult:
        if not statement.lstrip().upper().startswith("SELECT"):
            raise ProviderError("query() only runs read-only statements", provider=self.name, status_code=400)
        result = self._run(statement, params)
        if len(result.rows) > limit:
            result.rows = result.rows[:limit]
            result.truncated = True
        return result

    async def execute(self, statement: str, params: Any = None) -> QueryResult:
        return self._run(statement, params)

    async def list_tables(self) -> list[TableInfo]:
        rows = self.conn.execute("select name from sqlite_schema where type='table'").fetchall()
        return [TableInfo(name=r[0]) for r in rows]

    async def describe(self, table: str) -> TableInfo:
        return TableInfo(name=table)

    async def usage(self) -> QuotaInfo:
        return QuotaInfo(provider=self.name, used=float(self.setting("used_bytes", 100)),
                         limit=float(self.setting("capacity_bytes", 1000)), unit="bytes", window="total")

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


def embed_text(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    vec[0] = 0.01  # never a zero vector
    for word in _WORD_RE.findall(text.lower()):
        vec[zlib.crc32(word.encode()) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


class FakeRagLLM(LLMAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {Capability.CHAT, Capability.STREAM, Capability.EMBED, Capability.RERANK}
    )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        CHATS.append(request)
        return ChatResponse(model="fake-chat", content=self.setting("answer", "Apples are red [1]."),
                            usage=Usage(total_tokens=10))

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(delta="x")

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        EMBED["calls"] += 1
        EMBED["inputs"] += len(request.input)
        dim = int(EMBED["dim"])
        return EmbeddingResponse(model=str(EMBED["model"]), vectors=[embed_text(t, dim) for t in request.input])

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        RERANKS.append(request)
        if self.setting("rerank_down"):
            raise ProviderUnavailable("rerank down", provider=self.name, status_code=503)
        q = set(_WORD_RE.findall(request.query.lower()))
        scored = []
        for i, doc in enumerate(request.documents):
            words = set(_WORD_RE.findall(doc.lower()))
            scored.append(RerankResult(index=i, relevance_score=len(q & words) / (len(q) or 1)))
        scored.sort(key=lambda r: r.relevance_score, reverse=True)
        return RerankResponse(model="fake-rerank", results=scored[: request.top_n or len(scored)])

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


class FakeNoRerankLLM(FakeRagLLM):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.CHAT, Capability.STREAM, Capability.EMBED})
