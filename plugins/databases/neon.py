"""Neon serverless Postgres adapter.

SQL transport (``settings.driver``):

* ``http`` (default): Neon's SQL-over-HTTP endpoint (the one used by
  ``@neondatabase/serverless``). ``POST https://api.<region host>/sql`` with the
  ``Neon-Connection-String`` header; no driver dependency. ``query()`` is sent as a
  one-statement batch with ``Neon-Batch-Read-Only: true``. Override the endpoint with
  ``settings.http_sql_url`` if Neon changes the host scheme.
* ``asyncpg``: an asyncpg pool on ``database_url`` (``pip install asyncpg``).

Statements are SQL with positional ``$1`` parameters. ``usage()`` reads the project's
storage from the Neon API (``api_key`` secret + ``settings.project_id``) and falls back
to ``pg_database_size()``.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

from core.interfaces import (
    Capability,
    DBAdapter,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderError,
    QueryResult,
    QuotaInfo,
    TableInfo,
)

from ._common import elapsed_ms, jsonable
from ._pg import (
    AsyncpgRunner,
    PgRows,
    affected_from_status,
    pg_database_size,
    pg_describe,
    pg_list_tables,
    positional_params,
)

_BOOL_OIDS = {16}
_INT_OIDS = {20, 21, 23, 26, 28}
_FLOAT_OIDS = {700, 701}
_NUMERIC_OIDS = {1700}
_JSON_OIDS = {114, 3802}


def _parse_text(value: Any, oid: int | None) -> Any:
    """Neon's HTTP endpoint returns Postgres text output; convert the common scalar types."""
    if value is None or not isinstance(value, str):
        return value
    try:
        if oid in _BOOL_OIDS:
            return value in ("t", "true")
        if oid in _INT_OIDS:
            return int(value)
        if oid in _FLOAT_OIDS:
            return float(value)
        if oid in _NUMERIC_OIDS:
            return jsonable(Decimal(value))
        if oid in _JSON_OIDS:
            return json.loads(value)
    except (ValueError, ArithmeticError):
        return value
    return value


def _encode_param(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return "\\x" + bytes(value).hex()
    return json.dumps(jsonable(value))


class NeonAdapter(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Neon",
        description="Neon serverless Postgres over SQL-over-HTTP or asyncpg; usage via the Neon API.",
        required_secrets=("database_url",),
        optional_secrets=("api_key",),
        docs_url="https://neon.tech/docs/serverless/serverless-driver",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._runner: AsyncpgRunner | None = None

    # ------------------------------------------------------------------ transport
    @property
    def driver(self) -> str:
        return str(self.setting("driver", "http")).lower()

    @property
    def schemas(self) -> list[str]:
        value = self.setting("schemas", ["public"])
        return [value] if isinstance(value, str) else [str(s) for s in value]

    def _sql_url(self) -> str:
        override = self.setting("http_sql_url")
        if override:
            return str(override)
        host = urlsplit(self.require_secret("database_url")).hostname
        if not host or "." not in host:
            raise NotConfigured("database_url has no Neon host", provider=self.name)
        return "https://api." + host.split(".", 1)[1] + "/sql"

    def _http_headers(self, readonly: bool) -> dict[str, str]:
        headers = {
            "Neon-Connection-String": self.require_secret("database_url"),
            "Neon-Raw-Text-Output": "true",
            "Neon-Array-Mode": "true",
        }
        if readonly:
            headers["Neon-Batch-Read-Only"] = "true"
            headers["Neon-Batch-Isolation-Level"] = "ReadCommitted"
        return headers

    async def _http_run(self, sql: str, params: list[Any], *, readonly: bool) -> PgRows:
        body = {"queries": [{"query": sql, "params": [_encode_param(p) for p in params]}]}
        response = await self.request("POST", self._sql_url(), json=body,
                                      headers=self._http_headers(readonly))
        payload = response.json()
        result = (payload.get("results") or [payload])[0]
        fields = result.get("fields") or []
        oids = [f.get("dataTypeID") for f in fields]
        rows = []
        for raw in result.get("rows") or []:
            values = raw if isinstance(raw, list) else [raw.get(f.get("name")) for f in fields]
            rows.append([_parse_text(v, oids[i] if i < len(oids) else None) for i, v in enumerate(values)])
        affected = result.get("rowCount")
        if affected is None:
            affected = affected_from_status(result.get("command"))
        return PgRows(columns=[str(f.get("name")) for f in fields], rows=rows,
                      affected_rows=None if affected is None else int(affected))

    def _asyncpg(self) -> AsyncpgRunner:
        dsn = self.require_secret("database_url")
        if self._runner is None or self._runner.dsn != dsn:
            self._runner = AsyncpgRunner(dsn, self.name, max_size=int(self.setting("pool_max_size", 4)),
                                         timeout_s=self.context.timeout_s)
        return self._runner

    async def _run(self, sql: str, params: Any = (), *, readonly: bool = True,
                   limit: int | None = None) -> PgRows:
        if self.driver == "asyncpg":
            return await self._asyncpg().run(sql, list(params), readonly=readonly, limit=limit)
        if self.driver != "http":
            raise NotConfigured(f"unknown driver '{self.driver}' (http|asyncpg)", provider=self.name)
        result = await self._http_run(sql, list(params), readonly=readonly)
        if limit is not None and len(result.rows) > limit:
            result.rows = result.rows[:limit]
            result.truncated = True
        return result

    async def _introspect(self, sql: str, params: Any = ()) -> PgRows:
        return await self._run(sql, params)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._run("select 1"), detail=f"{self.driver} ok")

    async def list_tables(self) -> list[TableInfo]:
        return await pg_list_tables(self._introspect, self.schemas)

    async def describe(self, table: str) -> TableInfo:
        return await pg_describe(self._introspect, table, self.schemas[0], self.name)

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        result = await self._run(statement, positional_params(params, self.name), readonly=True,
                                 limit=limit)
        return QueryResult(columns=result.columns, rows=result.rows, row_count=len(result.rows),
                           elapsed_ms=elapsed_ms(started), backend=self.name,
                           truncated=result.truncated)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        result = await self._run(statement, positional_params(params, self.name), readonly=False)
        return QueryResult(columns=result.columns, rows=result.rows, row_count=len(result.rows),
                           affected_rows=result.affected_rows, elapsed_ms=elapsed_ms(started),
                           backend=self.name)

    async def usage(self) -> QuotaInfo:
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        api_key = self.secret("api_key")
        project_id = self.setting("project_id")
        if api_key and project_id:
            base = str(self.setting("api_base", "https://console.neon.tech/api/v2")).rstrip("/")
            response = await self.request(
                "GET", f"{base}/projects/{project_id}",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
            project = response.json().get("project") or {}
            used = project.get("synthetic_storage_size")
            if used is None:
                raise ProviderError("Neon API returned no synthetic_storage_size", provider=self.name)
            extra = {
                key: project[key]
                for key in ("compute_time_seconds", "active_time_seconds", "data_transfer_bytes",
                            "written_data_bytes", "cpu_used_sec", "consumption_period_end")
                if key in project
            }
            return QuotaInfo(provider=self.name, used=float(used), limit=limit, unit="bytes",
                             window="total", source="provider", extra=extra)
        used_bytes = await pg_database_size(self._introspect)
        return QuotaInfo(provider=self.name, used=float(used_bytes), limit=limit, unit="bytes",
                         window="total", source="provider")

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.close()
        await super().close()
