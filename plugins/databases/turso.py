"""Turso (libSQL) adapter over the Hrana-over-HTTP v2 pipeline API.

``POST https://<db>-<org>.turso.io/v2/pipeline`` with ``Authorization: Bearer <auth_token>``.
``database_url`` may use the ``libsql://`` scheme; it is rewritten to ``https://``.

Statements are SQLite SQL. Parameters: a list binds ``?``/``?NNN`` positionally, a dict
binds ``:name``/``@name``/``$name`` (a bare key gets a ``:`` prefix). ``query()`` first asks
the server to ``describe`` the statement and refuses anything that is not read-only.
"""

from __future__ import annotations

import base64
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
    QuotaExhausted,
    QuotaInfo,
    TableInfo,
)

from ._common import elapsed_ms


def encode_value(value: Any) -> dict[str, Any]:
    """Python value -> Hrana value object."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii").rstrip("=")}
    return {"type": "text", "value": str(value)}


def decode_value(value: dict[str, Any] | None) -> Any:
    """Hrana value object -> JSON-safe Python value (blobs stay base64 text)."""
    if not value:
        return None
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "blob":
        return value.get("base64", "")
    return value.get("value")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class TursoAdapter(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Turso",
        description="Turso libSQL via the Hrana-over-HTTP v2 pipeline API.",
        required_secrets=("database_url", "auth_token"),
        docs_url="https://docs.turso.tech/sdk/http/reference",
    )

    # ------------------------------------------------------------------ transport
    def _pipeline_url(self) -> str:
        url = self.require_secret("database_url").strip().rstrip("/")
        for scheme in ("libsql://", "wss://", "ws://"):
            if url.startswith(scheme):
                url = ("http://" if scheme == "ws://" else "https://") + url[len(scheme):]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url + "/v2/pipeline"

    def _stmt(self, sql: str, params: list[Any] | dict[str, Any] | None, want_rows: bool = True) -> dict[str, Any]:
        stmt: dict[str, Any] = {"sql": sql, "want_rows": want_rows}
        if isinstance(params, dict):
            stmt["named_args"] = [
                {"name": key if key[:1] in (":", "@", "$") else f":{key}", "value": encode_value(val)}
                for key, val in params.items()
            ]
        elif params:
            stmt["args"] = [encode_value(v) for v in params]
        return stmt

    async def _pipeline(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        body = {"baton": None, "requests": [*requests, {"type": "close"}]}
        response = await self.request(
            "POST", self._pipeline_url(), json=body,
            headers={"Authorization": f"Bearer {self.require_secret('auth_token')}"},
        )
        results = response.json().get("results") or []
        out = []
        for result in results[: len(requests)]:
            if result.get("type") == "error":
                error = result.get("error") or {}
                message = str(error.get("message") or "libSQL error")
                code = str(error.get("code") or "")
                if "BLOCKED" in code or "quota" in message.lower():
                    raise QuotaExhausted(message, provider=self.name, status_code=429)
                raise ProviderError(f"{code}: {message}" if code else message, provider=self.name,
                                    status_code=400)
            out.append((result.get("response") or {}))
        return out

    async def _execute(self, sql: str, params: list[Any] | dict[str, Any] | None = None) -> dict[str, Any]:
        (response,) = await self._pipeline([{"type": "execute", "stmt": self._stmt(sql, params)}])
        return response.get("result") or {}

    @staticmethod
    def _to_result(result: dict[str, Any], limit: int | None, backend: str, started: float) -> QueryResult:
        columns = [str(c.get("name") or f"col{i}") for i, c in enumerate(result.get("cols") or [])]
        rows = [[decode_value(v) for v in row] for row in result.get("rows") or []]
        truncated = limit is not None and len(rows) > limit
        if truncated:
            rows = rows[:limit]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows),
                           affected_rows=result.get("affected_row_count"),
                           elapsed_ms=elapsed_ms(started), backend=backend, truncated=truncated)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._execute("select 1"))

    async def list_tables(self) -> list[TableInfo]:
        result = await self._execute(
            "select name, type from sqlite_schema where type in ('table', 'view') "
            "and name not like 'sqlite\\_%' escape '\\' and name not like '\\_litestream%' escape '\\' "
            "and name not like 'libsql\\_%' escape '\\' order by name"
        )
        return [
            TableInfo(name=decode_value(row[0]), kind="view" if decode_value(row[1]) == "view" else "table")
            for row in result.get("rows") or []
        ]

    async def describe(self, table: str) -> TableInfo:
        ident = _quote_ident(table)
        info, cols = await self._pipeline([
            {"type": "execute", "stmt": self._stmt(
                "select type from sqlite_schema where name = ? and type in ('table', 'view')", [table])},
            {"type": "execute", "stmt": self._stmt(f"pragma table_info({ident})", None)},
        ])
        info_rows = (info.get("result") or {}).get("rows") or []
        if not info_rows:
            raise ProviderError(f"table {table} not found", provider=self.name, status_code=404)
        kind = "view" if decode_value(info_rows[0][0]) == "view" else "table"
        columns = []
        for row in (cols.get("result") or {}).get("rows") or []:
            _cid, name, col_type, notnull, default, pk = (decode_value(v) for v in row[:6])
            columns.append(ColumnInfo(name=str(name), type=str(col_type or ""), nullable=not notnull,
                                      primary_key=bool(pk), default=None if default is None else str(default)))
        row_count = None
        if self.setting("count_rows", True):
            counted = await self._execute(f"select count(*) from {ident}")
            rows = counted.get("rows") or []
            row_count = decode_value(rows[0][0]) if rows else None
        return TableInfo(name=table, kind=kind, row_count=row_count, columns=columns)

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        (described,) = await self._pipeline([{"type": "describe", "sql": statement}])
        if not (described.get("result") or {}).get("is_readonly", False):
            raise ProviderError("query() only runs read-only statements; use execute()",
                                provider=self.name, status_code=400)
        result = await self._execute(statement, params)
        return self._to_result(result, limit, self.name, started)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        result = await self._execute(statement, params)
        return self._to_result(result, None, self.name, started)

    async def usage(self) -> QuotaInfo:
        result = await self._execute(
            "select page_count * page_size from pragma_page_count(), pragma_page_size()"
        )
        rows = result.get("rows") or []
        used = decode_value(rows[0][0]) if rows else 0
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(provider=self.name, used=float(used or 0), limit=limit, unit="bytes",
                         window="total", source="provider")
