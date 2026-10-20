"""Postgres plumbing shared by Supabase and Neon (private module).

* :class:`AsyncpgRunner` runs SQL through an asyncpg pool (asyncpg is imported lazily).
* :class:`PgRows` is the driver-neutral result of one statement.
* :func:`pg_list_tables` / :func:`pg_describe` / :func:`pg_database_size` implement the
  DBAdapter introspection on top of any ``run(sql, params) -> PgRows`` callable.

Introspection SQL only uses scalar text parameters so it works identically over asyncpg
and over Neon's SQL-over-HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.interfaces import ColumnInfo, NotConfigured, ProviderError, TableInfo

from ._common import jsonable, translate_driver_error


@dataclass
class PgRows:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    affected_rows: int | None = None
    truncated: bool = False


Runner = Callable[[str, Sequence[Any]], Awaitable[PgRows]]

_STATUS_COUNT = re.compile(r"(\d+)\s*$")


def affected_from_status(status: str | None) -> int | None:
    """``'INSERT 0 3'`` -> 3, ``'UPDATE 2'`` -> 2, ``'CREATE TABLE'`` -> None."""
    if not status:
        return None
    match = _STATUS_COUNT.search(status)
    return int(match.group(1)) if match else None


def positional_params(params: list[Any] | dict[str, Any] | None, provider: str) -> list[Any]:
    if params is None:
        return []
    if isinstance(params, dict):
        raise ProviderError(
            "Postgres statements take positional parameters ($1, $2, ...): pass a list",
            provider=provider,
            status_code=400,
        )
    return list(params)


def split_table(table: str, default_schema: str) -> tuple[str, str]:
    if "." in table:
        schema, name = table.split(".", 1)
        return schema.strip('"'), name.strip('"')
    return default_schema, table.strip('"')


class AsyncpgRunner:
    """Lazily created asyncpg pool. ``statement_cache_size=0`` keeps it PgBouncer/Supavisor-safe."""

    def __init__(self, dsn: str, provider: str, *, max_size: int = 4, timeout_s: float = 30.0) -> None:
        self.dsn = dsn
        self.provider = provider
        self.max_size = max_size
        self.timeout_s = timeout_s
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                try:
                    import asyncpg  # heavy optional dependency, imported lazily
                except ImportError as exc:
                    raise NotConfigured("pip install asyncpg", provider=self.provider) from exc
                try:
                    self._pool = await asyncpg.create_pool(
                        dsn=self.dsn,
                        min_size=0,
                        max_size=self.max_size,
                        statement_cache_size=0,
                        command_timeout=self.timeout_s,
                    )
                except Exception as exc:
                    raise translate_driver_error(exc, self.provider) from exc
        return self._pool

    async def run(self, sql: str, params: Sequence[Any] = (), *, readonly: bool = False,
                  limit: int | None = None) -> PgRows:
        pool = await self.pool()
        try:
            async with pool.acquire() as conn:
                if readonly:
                    async with conn.transaction(readonly=True):
                        stmt = await conn.prepare(sql)
                        columns = [attr.name for attr in stmt.get_attributes()]
                        if limit is None:
                            records = await stmt.fetch(*params)
                            truncated = False
                        else:
                            cursor = await stmt.cursor(*params)
                            records = await cursor.fetch(limit + 1)
                            truncated = len(records) > limit
                            records = records[:limit]
                    return PgRows(
                        columns=columns,
                        rows=[[jsonable(v) for v in tuple(r)] for r in records],
                        truncated=truncated,
                    )
                async with conn.transaction():
                    stmt = await conn.prepare(sql)
                    columns = [attr.name for attr in stmt.get_attributes()]
                    records = await stmt.fetch(*params)
                    status = stmt.get_statusmsg()
                return PgRows(
                    columns=columns,
                    rows=[[jsonable(v) for v in tuple(r)] for r in records],
                    affected_rows=affected_from_status(status),
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise translate_driver_error(exc, self.provider) from exc

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()


# --------------------------------------------------------------------------- introspection

_LIST_TABLES_SQL = """
select n.nspname, c.relname, c.relkind, c.reltuples::bigint, pg_total_relation_size(c.oid)
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where c.relkind in ('r', 'p', 'v', 'm')
  and n.nspname = any(string_to_array($1, ','))
order by n.nspname, c.relname
"""

_ONE_TABLE_SQL = """
select n.nspname, c.relname, c.relkind, c.reltuples::bigint, pg_total_relation_size(c.oid)
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname = $1 and c.relname = $2 and c.relkind in ('r', 'p', 'v', 'm')
"""

_COLUMNS_SQL = """
select a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       not a.attnotnull,
       coalesce((select true from pg_catalog.pg_index i
                 where i.indrelid = c.oid and i.indisprimary and a.attnum = any(i.indkey)), false),
       pg_catalog.pg_get_expr(d.adbin, d.adrelid)
from pg_catalog.pg_attribute a
join pg_catalog.pg_class c on c.oid = a.attrelid
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
left join pg_catalog.pg_attrdef d on d.adrelid = c.oid and d.adnum = a.attnum
where n.nspname = $1 and c.relname = $2 and a.attnum > 0 and not a.attisdropped
order by a.attnum
"""

_DB_SIZE_SQL = "select pg_database_size(current_database())"

_KINDS = {"r": "table", "p": "table", "v": "view", "m": "view"}


def _table_from_row(row: Sequence[Any]) -> TableInfo:
    schema, name, relkind, reltuples, size = row[:5]
    rows = int(reltuples) if reltuples is not None and int(reltuples) >= 0 else None
    return TableInfo(
        name=name,
        schema_name=schema,
        kind=_KINDS.get(str(relkind), "table"),
        row_count=rows,
        size_bytes=int(size) if size is not None else None,
    )


async def pg_list_tables(run: Runner, schemas: Sequence[str]) -> list[TableInfo]:
    result = await run(_LIST_TABLES_SQL, [",".join(schemas)])
    return [_table_from_row(row) for row in result.rows]


async def pg_describe(run: Runner, table: str, default_schema: str, provider: str) -> TableInfo:
    schema, name = split_table(table, default_schema)
    head = await run(_ONE_TABLE_SQL, [schema, name])
    if not head.rows:
        raise ProviderError(f"table {schema}.{name} not found", provider=provider, status_code=404)
    info = _table_from_row(head.rows[0])
    cols = await run(_COLUMNS_SQL, [schema, name])
    info.columns = [
        ColumnInfo(
            name=str(r[0]),
            type=str(r[1] or ""),
            nullable=bool(r[2]),
            primary_key=bool(r[3]),
            default=None if r[4] is None else str(r[4]),
        )
        for r in cols.rows
    ]
    return info


async def pg_database_size(run: Runner) -> int:
    result = await run(_DB_SIZE_SQL, [])
    return int(result.rows[0][0]) if result.rows and result.rows[0][0] is not None else 0
