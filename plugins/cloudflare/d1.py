"""Cloudflare D1 (serverless SQLite) through the REST query API.

Statements are SQLite SQL. ``params`` must be a positional list (``?`` placeholders);
D1's HTTP API does not support named parameters.

Endpoints: ``POST /accounts/{account_id}/d1/database/{database_id}/query`` and
``GET /accounts/{account_id}/d1/database/{database_id}`` (``file_size`` for usage).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ColumnInfo,
    DBAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    QueryResult,
    QuotaInfo,
    TableInfo,
)
from plugins.cloudflare._api import CloudflareAPIMixin


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class D1Adapter(CloudflareAPIMixin, DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cloudflare D1",
        description="Serverless SQLite. Free 5GB, 5M rows read/day, 100k rows written/day.",
        required_secrets=("account_id", "api_token"),
        docs_url="https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/",
    )

    def _db_url(self, suffix: str = "") -> str:
        database_id = self.require_setting("database_id")
        return self.cf_account_url(f"d1/database/{database_id}{suffix}")

    async def _run(self, sql: str, params: list[Any] | dict[str, Any] | None) -> tuple[dict[str, Any], float]:
        if isinstance(params, dict):
            raise ProviderError("D1 only supports positional '?' parameters (pass a list)", provider=self.name, status_code=400)
        body: dict[str, Any] = {"sql": sql}
        if params:
            body["params"] = list(params)
        start = time.perf_counter()
        result = await self.cf_call("POST", self._db_url("/query"), json=body)
        elapsed = (time.perf_counter() - start) * 1000
        if not isinstance(result, list) or not result:
            raise ProviderError("unexpected D1 response shape", provider=self.name)
        # Several statements separated by ';' produce several result sets: report the last one.
        last = result[-1]
        if last.get("success") is False:
            raise ProviderError(str(last.get("error") or "D1 statement failed"), provider=self.name, status_code=400)
        return last, elapsed

    @staticmethod
    def _to_result(entry: dict[str, Any], elapsed: float, backend: str, limit: int | None) -> QueryResult:
        rows_raw = entry.get("results") or []
        columns: list[str] = []
        for row in rows_raw:
            for key in row:
                if key not in columns:
                    columns.append(key)
        truncated = limit is not None and len(rows_raw) > limit
        if limit is not None:
            rows_raw = rows_raw[:limit]
        rows = [[row.get(c) for c in columns] for row in rows_raw]
        meta = entry.get("meta") or {}
        changes = meta.get("changes")
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            affected_rows=int(changes) if changes is not None else None,
            elapsed_ms=float(meta.get("duration", elapsed)),
            backend=backend,
            truncated=truncated,
        )

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._run("SELECT 1", None))

    async def list_tables(self) -> list[TableInfo]:
        entry, _ = await self._run(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY name",
            None,
        )
        return [
            TableInfo(name=row["name"], kind="view" if row.get("type") == "view" else "table")
            for row in entry.get("results") or []
        ]

    async def describe(self, table: str) -> TableInfo:
        quoted = _quote_ident(table)
        info, _ = await self._run(f"PRAGMA table_info({quoted})", None)
        cols = info.get("results") or []
        if not cols:
            raise ProviderError(f"table not found: {table}", provider=self.name, status_code=404)
        count_entry, _ = await self._run(f"SELECT COUNT(*) AS n FROM {quoted}", None)
        count_rows = count_entry.get("results") or [{}]
        return TableInfo(
            name=table,
            kind="table",
            row_count=int(count_rows[0].get("n") or 0),
            columns=[
                ColumnInfo(
                    name=c["name"],
                    type=c.get("type") or "",
                    nullable=not bool(c.get("notnull")),
                    primary_key=bool(c.get("pk")),
                    default=None if c.get("dflt_value") is None else str(c.get("dflt_value")),
                )
                for c in cols
            ],
        )

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        entry, elapsed = await self._run(statement, params)
        return self._to_result(entry, elapsed, self.name, limit)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        entry, elapsed = await self._run(statement, params)
        return self._to_result(entry, elapsed, self.name, None)

    async def usage(self) -> QuotaInfo:
        info = await self.cf_call("GET", self._db_url())
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(
            provider=self.name,
            used=float((info or {}).get("file_size") or 0),
            limit=limit,
            unit="bytes",
            window="total",
            source="provider",
            extra={k: info.get(k) for k in ("num_tables", "name", "version") if isinstance(info, dict) and k in info},
        )

    async def quota(self) -> QuotaInfo:
        return await self.usage()
