"""Supabase (Postgres free tier) adapter.

Two transports, picked per operation:

* **SQL** (``db_url`` optional secret set): asyncpg pool against the project's Postgres
  (use the Supavisor pooler URL). ``query()``/``execute()`` take SQL with positional
  ``$1`` parameters; ``query()`` runs inside a READ ONLY transaction.
* **PostgREST** (always available with ``service_role_key``): ``list_tables``/``describe``
  read the OpenAPI document served at ``/rest/v1/``. Without ``db_url``, statements use
  the PostgREST form ``"<METHOD> /<table>?<query> [JSON body]"``, e.g.
  ``GET /todos?select=id,title&done=eq.false`` or ``PATCH /todos?id=eq.3 {"done": true}``.

Settings: ``project_url`` (``https://<ref>.supabase.co``, required for PostgREST),
``schemas`` (default ``["public"]``), ``pool_max_size`` (default 4).
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ColumnInfo,
    DBAdapter,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderError,
    QueryResult,
    QuotaInfo,
    TableInfo,
)

from ._common import documents_to_result, elapsed_ms
from ._pg import AsyncpgRunner, PgRows, pg_database_size, pg_describe, pg_list_tables, positional_params

_REST_METHODS = {"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"}


class SupabaseAdapter(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Supabase",
        description="Supabase Postgres via asyncpg (db_url) or PostgREST (service role key).",
        required_secrets=("service_role_key",),
        optional_secrets=("db_url",),
        docs_url="https://supabase.com/docs/guides/api",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._runner: AsyncpgRunner | None = None

    # ------------------------------------------------------------------ plumbing
    @property
    def schemas(self) -> list[str]:
        value = self.setting("schemas", ["public"])
        return [value] if isinstance(value, str) else [str(s) for s in value]

    def _rest_base(self) -> str:
        return str(self.require_setting("project_url")).rstrip("/") + "/rest/v1"

    def _rest_headers(self, schema: str | None = None, write: bool = False) -> dict[str, str]:
        key = self.require_secret("service_role_key")
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        if schema:
            headers["Content-Profile" if write else "Accept-Profile"] = schema
        return headers

    def _sql_runner(self) -> AsyncpgRunner | None:
        dsn = self.secret("db_url")
        if not dsn:
            return None
        if self._runner is None or self._runner.dsn != dsn:
            self._runner = AsyncpgRunner(
                dsn,
                self.name,
                max_size=int(self.setting("pool_max_size", 4)),
                timeout_s=self.context.timeout_s,
            )
        return self._runner

    async def _run(self, sql: str, params: Any = ()) -> PgRows:
        runner = self._sql_runner()
        if runner is None:
            raise NotConfigured("SQL needs the db_url secret (SUPABASE_DB_URL)", provider=self.name)
        return await runner.run(sql, params, readonly=True)

    async def _openapi(self) -> dict[str, Any]:
        response = await self.request(
            "GET", self._rest_base() + "/", headers=self._rest_headers(self.schemas[0])
        )
        return response.json()

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        runner = self._sql_runner()
        if runner is not None:
            return await self.probe(lambda: runner.run("select 1", (), readonly=True), detail="sql ok")
        return await self.probe(self._openapi, detail="postgrest ok")

    async def list_tables(self) -> list[TableInfo]:
        if self._sql_runner() is not None:
            return await pg_list_tables(self._run, self.schemas)
        spec = await self._openapi()
        out = []
        for name, definition in sorted((spec.get("definitions") or {}).items()):
            out.append(self._table_from_definition(name, definition))
        return out

    async def describe(self, table: str) -> TableInfo:
        if self._sql_runner() is not None:
            return await pg_describe(self._run, table, self.schemas[0], self.name)
        spec = await self._openapi()
        definitions = spec.get("definitions") or {}
        bare = table.split(".", 1)[-1]
        if bare not in definitions:
            raise ProviderError(f"table {table} not found", provider=self.name, status_code=404)
        info = self._table_from_definition(bare, definitions[bare])
        info.row_count = await self._rest_count(bare)
        return info

    def _table_from_definition(self, name: str, definition: dict[str, Any]) -> TableInfo:
        required = set(definition.get("required") or [])
        columns = []
        for col, prop in (definition.get("properties") or {}).items():
            description = str(prop.get("description") or "")
            columns.append(
                ColumnInfo(
                    name=col,
                    type=str(prop.get("format") or prop.get("type") or ""),
                    nullable=col not in required,
                    primary_key="<pk/>" in description,
                    default=None if prop.get("default") is None else str(prop.get("default")),
                )
            )
        return TableInfo(name=name, schema_name=self.schemas[0], kind="table", columns=columns)

    async def _rest_count(self, table: str) -> int | None:
        headers = self._rest_headers(self.schemas[0])
        headers.update({"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"})
        response = await self.request("HEAD", f"{self._rest_base()}/{table}", headers=headers)
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        return int(total) if total.isdigit() else None

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        runner = self._sql_runner()
        if runner is not None and not self._is_rest_statement(statement):
            result = await runner.run(
                statement, positional_params(params, self.name), readonly=True, limit=limit
            )
            return QueryResult(
                columns=result.columns,
                rows=result.rows,
                row_count=len(result.rows),
                elapsed_ms=elapsed_ms(started),
                backend=self.name,
                truncated=result.truncated,
            )
        method, path, body = self._parse_rest(statement)
        if method not in ("GET", "HEAD"):
            raise ProviderError("query() only accepts GET PostgREST statements", provider=self.name,
                                status_code=400)
        headers = self._rest_headers(self.schemas[0])
        headers.update({"Range-Unit": "items", "Range": f"0-{limit}"})
        response = await self.request("GET", self._rest_base() + path, headers=headers)
        data = response.json()
        docs = data if isinstance(data, list) else [data]
        return documents_to_result(docs, limit=limit, backend=self.name, started=started)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        runner = self._sql_runner()
        if runner is not None and not self._is_rest_statement(statement):
            result = await runner.run(statement, positional_params(params, self.name))
            return QueryResult(
                columns=result.columns,
                rows=result.rows,
                row_count=len(result.rows),
                affected_rows=result.affected_rows,
                elapsed_ms=elapsed_ms(started),
                backend=self.name,
            )
        method, path, body = self._parse_rest(statement)
        if body is None and isinstance(params, (dict, list)):
            body = params
        headers = self._rest_headers(self.schemas[0], write=method != "GET")
        headers["Prefer"] = "return=representation"
        response = await self.request(method, self._rest_base() + path, headers=headers,
                                      json=body if body is not None else None)
        data: Any = response.json() if response.content else []
        docs = data if isinstance(data, list) else [data]
        result = documents_to_result(docs, limit=max(len(docs), 1), backend=self.name, started=started)
        result.affected_rows = len(docs)
        return result

    async def usage(self) -> QuotaInfo:
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        if self._sql_runner() is None:
            raise NotConfigured("database size needs the db_url secret (SUPABASE_DB_URL)",
                                provider=self.name)
        used = await pg_database_size(self._run)
        return QuotaInfo(provider=self.name, used=float(used), limit=limit, unit="bytes",
                         window="total", source="provider")

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.close()
        await super().close()

    # ------------------------------------------------------------------ PostgREST statements
    @staticmethod
    def _is_rest_statement(statement: str) -> bool:
        head = statement.lstrip().split(" ", 1)[0].upper()
        return statement.lstrip().startswith("/") or (
            head in _REST_METHODS and statement.lstrip()[len(head):].lstrip().startswith("/")
        )

    def _parse_rest(self, statement: str) -> tuple[str, str, Any]:
        text = statement.strip()
        if text.startswith("/"):
            method, rest = "GET", text
        else:
            parts = text.split(None, 1)
            method, rest = parts[0].upper(), (parts[1] if len(parts) > 1 else "")
        if method not in _REST_METHODS or not rest.startswith("/"):
            raise ProviderError(
                "without db_url, statements use the PostgREST form '<METHOD> /<table>?<query> [JSON]'",
                provider=self.name,
                status_code=400,
            )
        path, _, raw_body = rest.partition(" ")
        body = None
        if raw_body.strip():
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"invalid JSON body: {exc}", provider=self.name,
                                    status_code=400) from exc
        return method, path, body
