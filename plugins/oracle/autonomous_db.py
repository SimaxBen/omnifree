"""Oracle Autonomous Database (Always Free, 20GB) via python-oracledb thin mode (asyncio API).

``oracledb`` is imported lazily. Connection modes:

* **wallet-less TLS** (recommended): enable "TLS authentication: not required" (mTLS off) on the
  ADB and store the full TLS connect descriptor as the ``dsn`` secret, e.g.
  ``(description=(retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1521)(host=adb.eu-frankfurt-1.oraclecloud.com))(connect_data=(service_name=xxx_low.adb.oraclecloud.com))(security=(ssl_server_dn_match=yes)))``.
  An access control list must allow the caller's IP.
* **mTLS wallet**: set ``settings.wallet_dir`` (unzipped wallet with ``ewallet.pem`` / ``tnsnames.ora``)
  and the optional ``wallet_password`` secret; ``dsn`` may then be a tnsnames alias such as ``mydb_low``.

Statements are Oracle SQL with ``:name`` or ``:1`` binds; a trailing ``;`` is removed (except after
PL/SQL ``END;``). Settings: ``wallet_dir``, ``pool_min`` (0), ``pool_max`` (2; Always Free allows
about 30 concurrent sessions), ``call_timeout_ms``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, ClassVar

from core.interfaces import (
    Capability,
    ColumnInfo,
    DBAdapter,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
    QueryResult,
    QuotaInfo,
    TableInfo,
)

log = logging.getLogger(__name__)

# ORA-/DPY- codes meaning "the database cannot be reached right now".
_UNAVAILABLE_PREFIXES = ("DPY-6005", "DPY-6000", "DPY-4011", "DPY-6001", "ORA-12", "ORA-03113", "ORA-03114",
                         "ORA-03135", "ORA-01033", "ORA-01034", "ORA-01089")
_AUTH_PREFIXES = ("ORA-01017", "ORA-28000", "ORA-01045", "ORA-28001")


def _clean_sql(statement: str) -> str:
    sql = statement.strip()
    if sql.endswith(";") and not sql.upper().rstrip(";").rstrip().endswith("END"):
        sql = sql.rstrip(";").rstrip()
    return sql


class OracleAutonomousDBAdapter(DBAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.SQL})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Oracle Autonomous Database",
        description="Always Free ADB (20GB) through oracledb thin mode.",
        required_secrets=("user", "password", "dsn"),
        optional_secrets=("wallet_password",),
        docs_url="https://python-oracledb.readthedocs.io/en/latest/user_guide/asyncio.html",
    )

    _pool: Any = None
    _oracledb: Any = None

    def _module(self) -> Any:
        if self._oracledb is None:
            try:
                import oracledb  # type: ignore[import-not-found]
            except ImportError as exc:
                raise NotConfigured("pip install oracledb", provider=self.name) from exc
            # Return CLOB/BLOB as str/bytes instead of LOB locators (async LOB reads need extra awaits).
            oracledb.defaults.fetch_lobs = False
            self._oracledb = oracledb
        return self._oracledb

    def _map_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        text = str(exc)
        if any(code in text for code in _AUTH_PREFIXES):
            return ProviderAuthError(text[:500], provider=self.name)
        if any(code in text for code in _UNAVAILABLE_PREFIXES):
            return ProviderUnavailable(text[:500], provider=self.name)
        return ProviderError(text[:500], provider=self.name, status_code=400)

    def _get_pool(self) -> Any:
        if self._pool is None:
            # Validate secrets first so an unconfigured adapter never imports oracledb.
            kwargs: dict[str, Any] = {
                "user": self.require_secret("user"),
                "password": self.require_secret("password"),
                "dsn": self.require_secret("dsn"),
                "min": int(self.setting("pool_min", 0)),
                "max": int(self.setting("pool_max", 2)),
                "increment": 1,
            }
            oracledb = self._module()
            wallet_dir = self.setting("wallet_dir")
            if wallet_dir:
                kwargs.update(config_dir=wallet_dir, wallet_location=wallet_dir)
                if self.secret("wallet_password"):
                    kwargs["wallet_password"] = self.secret("wallet_password")
            try:
                self._pool = oracledb.create_pool_async(**kwargs)
            except Exception as exc:  # noqa: BLE001 - driver errors are mapped
                raise self._map_error(exc) from exc
        return self._pool

    async def _run(self, sql: str, params: Any, *, fetch: int | None, commit: bool) -> QueryResult:
        pool = self._get_pool()
        start = time.perf_counter()
        try:
            async with pool.acquire() as connection:
                timeout = self.setting("call_timeout_ms")
                if timeout:
                    connection.call_timeout = int(timeout)
                with connection.cursor() as cursor:
                    await cursor.execute(_clean_sql(sql), params or None)
                    columns: list[str] = []
                    rows: list[list[Any]] = []
                    truncated = False
                    if cursor.description:
                        columns = [d[0] for d in cursor.description]
                        limit = fetch if fetch is not None else 10_000
                        fetched = await cursor.fetchmany(limit + 1)
                        truncated = len(fetched) > limit
                        rows = [list(r) for r in fetched[:limit]]
                    affected = cursor.rowcount if not cursor.description else None
                if commit:
                    await connection.commit()
                else:
                    # Never leave read-path DML pending on a pooled connection.
                    await connection.rollback()
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver errors are mapped
            raise self._map_error(exc) from exc
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            affected_rows=affected if affected is None or affected >= 0 else None,
            elapsed_ms=(time.perf_counter() - start) * 1000,
            backend=self.name,
            truncated=truncated,
        )

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._run("SELECT 1 FROM dual", None, fetch=1, commit=False))

    async def list_tables(self) -> list[TableInfo]:
        result = await self._run(
            "SELECT table_name, num_rows, 'table' FROM user_tables "
            "UNION ALL SELECT view_name, NULL, 'view' FROM user_views ORDER BY 1",
            None, fetch=5000, commit=False,
        )
        return [
            TableInfo(name=row[0], kind="view" if row[2] == "view" else "table",
                      row_count=int(row[1]) if row[1] is not None else None)
            for row in result.rows
        ]

    async def describe(self, table: str) -> TableInfo:
        name = table if table.startswith('"') else table.upper()
        name = name.strip('"')
        cols = await self._run(
            "SELECT column_name, data_type, data_length, data_precision, data_scale, nullable, data_default "
            "FROM user_tab_columns WHERE table_name = :t ORDER BY column_id",
            {"t": name}, fetch=2000, commit=False,
        )
        if not cols.rows:
            raise ProviderError(f"table not found: {table}", provider=self.name, status_code=404)
        pks = await self._run(
            "SELECT cc.column_name FROM user_constraints c JOIN user_cons_columns cc "
            "ON c.constraint_name = cc.constraint_name WHERE c.table_name = :t AND c.constraint_type = 'P'",
            {"t": name}, fetch=200, commit=False,
        )
        stats = await self._run(
            "SELECT t.num_rows, (SELECT SUM(bytes) FROM user_segments s WHERE s.segment_name = t.table_name) "
            "FROM user_tables t WHERE t.table_name = :t",
            {"t": name}, fetch=1, commit=False,
        )
        pk_names = {r[0] for r in pks.rows}
        num_rows = stats.rows[0][0] if stats.rows else None
        size = stats.rows[0][1] if stats.rows else None

        def type_of(dtype: str, length: Any, precision: Any, scale: Any) -> str:
            if dtype in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"):
                return f"{dtype}({length})"
            if dtype == "NUMBER" and precision is not None:
                return f"NUMBER({precision},{scale or 0})"
            return dtype

        return TableInfo(
            name=name,
            kind="table" if stats.rows else "view",
            row_count=int(num_rows) if num_rows is not None else None,
            size_bytes=int(size) if size is not None else None,
            columns=[
                ColumnInfo(
                    name=r[0],
                    type=type_of(r[1], r[2], r[3], r[4]),
                    nullable=r[5] == "Y",
                    primary_key=r[0] in pk_names,
                    default=str(r[6]).strip() if r[6] is not None else None,
                )
                for r in cols.rows
            ],
        )

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        return await self._run(statement, params, fetch=limit, commit=False)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        return await self._run(statement, params, fetch=None, commit=True)

    async def usage(self) -> QuotaInfo:
        result = await self._run("SELECT NVL(SUM(bytes), 0) FROM user_segments", None, fetch=1, commit=False)
        used = float(result.rows[0][0]) if result.rows else 0.0
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(provider=self.name, used=used, limit=limit, unit="bytes", window="total",
                         source="provider", extra={"scope": "schema segments (user_segments)"})

    async def quota(self) -> QuotaInfo:
        return await self.usage()

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close(force=True)
            except Exception as exc:  # noqa: BLE001 - closing must not raise
                log.warning("closing oracledb pool for %s failed: %s", self.name, exc)
        await super().close()
