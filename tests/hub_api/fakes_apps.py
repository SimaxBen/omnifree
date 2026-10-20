"""Fakes for the personal-app routers (notes, expenses, links): test code only, no network.

* ``FakeD1``: a DB plugin backed by one shared in-memory SQLite connection, with the same
  ``query``/``execute`` interface (positional ``?`` params, QueryResult) as the D1 adapter.
* ``FakeChatLLM``: scripted chat answers (``CHAT_SCRIPT[name]``), records requests, transcribes
  to ``TRANSCRIPTS[name]``.
* ``FakeVisionLLM``: same, advertising Capability.VISION.
* ``FakePresignStorage``: FakeStorage with deterministic presigned URLs.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ChatRequest,
    ChatResponse,
    DBAdapter,
    HealthStatus,
    ProviderError,
    QueryResult,
    QuotaInfo,
    TableInfo,
    TranscriptionRequest,
    TranscriptionResponse,
    Usage,
)
from tests.hub_api.fakes import FakeLLM, FakeStorage, _fail

CHAT_SCRIPT: dict[str, list[Any]] = defaultdict(list)  # provider -> queue of str | Exception
CHAT_LOG: list[tuple[str, ChatRequest]] = []
TRANSCRIPTS: dict[str, str] = {}
TRANSCRIBE_LOG: list[tuple[str, TranscriptionRequest]] = []
PRESIGN_LOG: list[tuple[str, str, int, str]] = []
PRESIGN_FAIL: set[str] = set()
SQL_LOG: list[str] = []
_CONN: list[sqlite3.Connection] = []


def reset() -> None:
    CHAT_SCRIPT.clear()
    CHAT_LOG.clear()
    TRANSCRIPTS.clear()
    TRANSCRIBE_LOG.clear()
    PRESIGN_LOG.clear()
    PRESIGN_FAIL.clear()
    SQL_LOG.clear()
    for c in _CONN:
        c.close()
    _CONN.clear()


def conn() -> sqlite3.Connection:
    if not _CONN:
        _CONN.append(sqlite3.connect(":memory:", check_same_thread=False))
    return _CONN[0]


def sql(statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    """Direct access for tests (e.g. to expire a link)."""
    c = conn()
    cur = c.execute(statement, params or [])
    c.commit()
    return cur.fetchall()


class FakeD1(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})

    def _run(self, statement: str, params: list[Any] | dict[str, Any] | None) -> sqlite3.Cursor:
        if isinstance(params, dict):
            raise ProviderError("D1 only supports positional '?' parameters", provider=self.name, status_code=400)
        SQL_LOG.append(statement)
        try:
            cur = conn().execute(statement, list(params or []))
            conn().commit()
        except sqlite3.Error as exc:
            raise ProviderError(str(exc), provider=self.name, status_code=400) from exc
        return cur

    async def query(self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500) -> QueryResult:
        cur = self._run(statement, params)
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchall()]
        return QueryResult(columns=columns, rows=rows[:limit], row_count=min(len(rows), limit),
                           truncated=len(rows) > limit, backend=self.name)

    async def execute(self, statement: str, params: list[Any] | dict[str, Any] | None = None) -> QueryResult:
        cur = self._run(statement, params)
        return QueryResult(affected_rows=max(cur.rowcount, 0), backend=self.name)

    async def list_tables(self) -> list[TableInfo]:
        return []

    async def describe(self, table: str) -> TableInfo:
        return TableInfo(name=table)

    async def usage(self) -> QuotaInfo:
        return QuotaInfo(provider=self.name, used=0.0, unit="bytes", window="total")

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


class FakeChatLLM(FakeLLM):
    capabilities: ClassVar[frozenset[str]] = FakeLLM.capabilities | {Capability.TRANSCRIBE}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        CHAT_LOG.append((self.name, request))
        _fail(self.name, self.setting("behaviour"))
        queue = CHAT_SCRIPT[self.name]
        answer = queue.pop(0) if queue else "{}"
        if isinstance(answer, Exception):
            raise answer
        return ChatResponse(model="fake", content=answer, usage=Usage(total_tokens=7))

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        TRANSCRIBE_LOG.append((self.name, request))
        _fail(self.name, self.setting("behaviour"))
        return TranscriptionResponse(model="whisper-fake", text=TRANSCRIPTS.get(self.name, ""))


class FakeVisionLLM(FakeChatLLM):
    capabilities: ClassVar[frozenset[str]] = FakeLLM.capabilities | {Capability.VISION}


class FakePresignStorage(FakeStorage):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.PRESIGN})

    async def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        if self.name in PRESIGN_FAIL:
            raise ProviderError("presign backend down", provider=self.name, status_code=503)
        PRESIGN_LOG.append((self.name, key, expires_s, method))
        return f"https://signed.example/{self.name}/{key}?exp={expires_s}"


A = "tests.hub_api.fakes_apps"
H = "tests.hub_api.fakes"


def apps_plugins() -> dict[str, Any]:
    """providers.yaml plugin map for the app routers: a text LLM first in the chain (no vision),
    two vision LLMs, presign-capable + plain storage and a SQLite-backed ``cloudflare_d1``."""
    return {
        "groq": {"module": f"{A}:FakeChatLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
                 "settings": {"default_model": "g", "models": ["g"]}},
        "gemini": {"module": f"{A}:FakeVisionLLM", "kind": "llm", "group": "llm_pool", "priority": 30,
                   "settings": {"default_model": "gem", "models": ["gem"]}},
        "vision_b": {"module": f"{A}:FakeVisionLLM", "kind": "llm", "group": "llm_pool", "priority": 20,
                     "settings": {"default_model": "vb", "models": ["vb"]}},
        "r2": {"module": f"{A}:FakePresignStorage", "kind": "storage", "group": "storage", "priority": 10,
               "settings": {"capacity_bytes": 10_000_000}},
        "plain": {"module": f"{H}:FakeStorage", "kind": "storage", "group": "storage", "priority": 20,
                  "settings": {"capacity_bytes": 1000}},
        "cloudflare_d1": {"module": f"{A}:FakeD1", "kind": "db", "group": "cloudflare", "priority": 10},
    }
