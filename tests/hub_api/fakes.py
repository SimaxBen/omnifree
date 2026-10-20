"""In-test fake adapters registered through providers.yaml entries (test code only).

Behaviour is driven by each entry's ``settings`` so one class serves many entries.
State that must survive re-instantiation (registry drops instances on disable /
settings updates) lives in module-level dicts cleared by ``reset()``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    ColumnInfo,
    ComputeAdapter,
    DBAdapter,
    DNSAdapter,
    DNSQueryLogEntry,
    DNSStatus,
    HealthStatus,
    LLMAdapter,
    ModelInfo,
    PluginMeta,
    ProviderError,
    ProviderUnavailable,
    QueryResult,
    QuotaExhausted,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
    TableInfo,
    Usage,
    VMInstance,
)

STORES: dict[str, dict[str, tuple[bytes, str | None]]] = defaultdict(dict)
DB_LOG: list[tuple[str, str, str]] = []  # (provider, mode, statement)
DNS_STATE: dict[str, dict[str, Any]] = defaultdict(lambda: {"enabled": True, "duration": None})
POWER_LOG: list[tuple[str, str, str]] = []


def reset() -> None:
    STORES.clear()
    DB_LOG.clear()
    DNS_STATE.clear()
    POWER_LOG.clear()


def _fail(name: str, behaviour: str | None) -> None:
    if behaviour == "quota":
        raise QuotaExhausted("rate limited", provider=name, status_code=429, retry_after=60)
    if behaviour == "down":
        raise ProviderUnavailable("503 upstream", provider=name, status_code=503)


class FakeLLM(LLMAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(display_name="Fake LLM")

    def _routed(self) -> str:
        upstream = self.setting("upstream")
        return f"{self.name}:{upstream}" if upstream else ""

    async def chat(self, request: ChatRequest) -> ChatResponse:
        _fail(self.name, self.setting("behaviour"))
        last = request.messages[-1].content if request.messages else ""
        return ChatResponse(
            model=request.model or self.setting("default_model", "fake-model"),
            content=f"{self.name} says: {last}",
            usage=Usage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            routed_via=self._routed(),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        _fail(self.name, self.setting("behaviour"))
        for word in ("Hello", " from", f" {self.name}"):
            yield ChatChunk(delta=word, model="fake-model", routed_via=self._routed())
        yield ChatChunk(finish_reason="stop", usage=Usage(total_tokens=4), routed_via=self._routed())

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m, provider=self.name) for m in self.setting("models", [])]

    async def health(self) -> HealthStatus:
        return await self.probe(self._check)

    async def _check(self) -> None:
        _fail(self.name, self.setting("behaviour"))


class FakeStorage(StorageAdapter):
    @property
    def objects(self) -> dict[str, tuple[bytes, str | None]]:
        return STORES[self.name]

    async def put(self, key: str, data: bytes, content_type: str | None = None, metadata: dict[str, str] | None = None) -> StoredObject:
        _fail(self.name, self.setting("behaviour"))
        self.objects[key] = (data, content_type)
        return StoredObject(key=key, size=len(data), content_type=content_type, backend=self.name, metadata=metadata or {})

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ProviderError("not found", provider=self.name, status_code=404)
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        return [
            StoredObject(key=k, size=len(v[0]), content_type=v[1], backend=self.name)
            for k, v in sorted(self.objects.items()) if k.startswith(prefix)
        ][:limit]

    async def stat(self, key: str) -> StoredObject | None:
        item = self.objects.get(key)
        return None if item is None else StoredObject(key=key, size=len(item[0]), backend=self.name)

    async def quota(self) -> QuotaInfo:
        used = sum(len(v[0]) for v in self.objects.values()) + int(self.setting("used_bytes", 0))
        return QuotaInfo(
            provider=self.name, used=float(used), limit=self.setting("capacity_bytes"),
            unit="bytes", window="total", source="provider",
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True, latency_ms=2.5)


class FakeSQLDB(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})

    async def list_tables(self) -> list[TableInfo]:
        return [TableInfo(name="users", row_count=2), TableInfo(name="notes", row_count=0)]

    async def describe(self, table: str) -> TableInfo:
        if table not in ("users", "notes"):
            raise ProviderError(f"no such table {table}", provider=self.name, status_code=404)
        return TableInfo(
            name=table,
            columns=[ColumnInfo(name="id", type="INTEGER", primary_key=True, nullable=False), ColumnInfo(name="name", type="TEXT")],
        )

    async def query(self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500) -> QueryResult:
        DB_LOG.append((self.name, "read", statement))
        if re.search(r"\bbroken\b", statement):
            raise ProviderError("syntax error near 'broken'", provider=self.name, status_code=400)
        rows = [[i, f"user{i}"] for i in range(1, int(self.setting("rows", 2)) + 1)]
        return QueryResult(columns=["id", "name"], rows=rows[: limit + 1], row_count=len(rows), backend=self.name)

    async def execute(self, statement: str, params: list[Any] | dict[str, Any] | None = None) -> QueryResult:
        DB_LOG.append((self.name, "write", statement))
        return QueryResult(affected_rows=1, backend=self.name)

    async def usage(self) -> QuotaInfo:
        return QuotaInfo(provider=self.name, used=1024.0, limit=float(self.setting("capacity_bytes", 4096)), unit="bytes", window="total")

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True, latency_ms=1.0)


class FakeDNS(DNSAdapter):
    async def toggle_blocking(self, enabled: bool, duration_s: int | None = None) -> DNSStatus:
        DNS_STATE[self.name] = {"enabled": enabled, "duration": duration_s}
        return await self.status()

    async def status(self) -> DNSStatus:
        st = DNS_STATE[self.name]
        return DNSStatus(provider=self.name, blocking_enabled=st["enabled"], queries_today=100, blocked_today=25, percent_blocked=25.0)

    async def query_log(self, limit: int = 100, blocked_only: bool = False) -> list[DNSQueryLogEntry]:
        now = datetime.now(timezone.utc)
        entries = [
            DNSQueryLogEntry(timestamp=now, domain="ads.example.com", client="10.0.0.2", query_type="A", status="GRAVITY", blocked=True),
            DNSQueryLogEntry(timestamp=now, domain="github.com", client="10.0.0.2", query_type="A", status="FORWARDED", blocked=False),
        ]
        if blocked_only:
            entries = [e for e in entries if e.blocked]
        return entries[:limit]

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


class FakeTailnet(ComputeAdapter):
    async def list_instances(self) -> list[VMInstance]:
        return [
            VMInstance(id="n1", name="oracle-arm", provider=self.name, state="online", tailscale_ip="100.64.0.1"),
            VMInstance(id="n2", name="amd-vm1", provider=self.name, state="offline", tailscale_ip="100.64.0.2"),
        ]

    async def status(self, instance_id: str) -> VMInstance:
        return next(i for i in await self.list_instances() if i.id == instance_id)

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=True)


class FakePowerVM(FakeTailnet):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.POWER})

    async def start(self, instance_id: str) -> VMInstance:
        POWER_LOG.append((self.name, "start", instance_id))
        return VMInstance(id=instance_id, name="vm", provider=self.name, state="starting")

    async def stop(self, instance_id: str) -> VMInstance:
        POWER_LOG.append((self.name, "stop", instance_id))
        return VMInstance(id=instance_id, name="vm", provider=self.name, state="stopping")
