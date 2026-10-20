"""Upstash Redis adapter over the REST API.

Statements are one Redis command, either a command line (``SET user:1 "Ada Lovelace"``,
shell-style quoting) or a JSON array (``["HSET", "user:1", "name", "Ada"]``). A ``params``
list is appended to the command arguments. ``query()`` only accepts read commands.

Key namespaces map to tables: keys are grouped by the prefix before ``settings.key_delimiter``
(default ``:``), giving ``TableInfo(name="user:*", kind="keyspace")``; keys without a
delimiter are grouped under ``settings.root_keyspace`` (default ``(root)``).
"""

from __future__ import annotations

import json
import re
import shlex
import time
from typing import Any, ClassVar

from core.interfaces import (
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

from ._common import elapsed_ms, jsonable

READ_COMMANDS = {
    "GET", "MGET", "GETRANGE", "STRLEN", "EXISTS", "TYPE", "TTL", "PTTL", "EXPIRETIME", "KEYS", "SCAN",
    "RANDOMKEY", "DBSIZE", "PING", "ECHO", "INFO", "TIME", "MEMORY", "OBJECT", "DUMP",
    "HGET", "HGETALL", "HMGET", "HKEYS", "HVALS", "HLEN", "HEXISTS", "HSCAN", "HSTRLEN", "HRANDFIELD",
    "LRANGE", "LLEN", "LINDEX", "LPOS",
    "SMEMBERS", "SCARD", "SISMEMBER", "SMISMEMBER", "SSCAN", "SRANDMEMBER", "SINTER", "SUNION", "SDIFF",
    "ZRANGE", "ZRANGEBYSCORE", "ZRANGEBYLEX", "ZREVRANGE", "ZREVRANGEBYSCORE", "ZCARD", "ZSCORE",
    "ZMSCORE", "ZRANK", "ZREVRANK", "ZCOUNT", "ZLEXCOUNT", "ZSCAN", "ZRANDMEMBER",
    "XRANGE", "XREVRANGE", "XLEN", "XINFO", "XREAD",
    "GETBIT", "BITCOUNT", "BITPOS", "PFCOUNT", "GEOPOS", "GEODIST", "GEOHASH", "GEOSEARCH",
    "JSON.GET", "JSON.MGET", "JSON.TYPE", "JSON.STRLEN", "JSON.OBJKEYS", "JSON.OBJLEN", "JSON.ARRLEN",
}
_PAIR_COMMANDS = {"HGETALL", "CONFIG"}
_SCAN_COMMANDS = {"SCAN", "HSCAN", "SSCAN", "ZSCAN"}
_USED_MEMORY = re.compile(r"^used_memory:(\d+)", re.MULTILINE)


class UpstashRedisAdapter(DBAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Upstash Redis",
        description="Upstash Redis REST API; statements are Redis commands.",
        required_secrets=("rest_url", "rest_token"),
        docs_url="https://upstash.com/docs/redis/features/restapi",
    )

    # ------------------------------------------------------------------ transport
    def _parse(self, statement: str, params: list[Any] | dict[str, Any] | None) -> list[str]:
        text = statement.strip()
        if not text:
            raise ProviderError("empty Redis command", provider=self.name, status_code=400)
        if text.startswith("["):
            try:
                parts = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"invalid JSON command: {exc}", provider=self.name, status_code=400) from exc
            if not isinstance(parts, list) or not parts:
                raise ProviderError("JSON command must be a non-empty array", provider=self.name, status_code=400)
        else:
            try:
                parts = shlex.split(text)
            except ValueError as exc:
                raise ProviderError(f"invalid command line: {exc}", provider=self.name, status_code=400) from exc
        if isinstance(params, list):
            parts.extend(params)
        elif isinstance(params, dict):
            raise ProviderError("Redis params must be a list of extra arguments", provider=self.name,
                                status_code=400)
        return [p if isinstance(p, str) else json.dumps(p) if isinstance(p, (dict, list)) else str(p)
                for p in parts]

    async def command(self, *args: Any) -> Any:
        """Run one Redis command and return its ``result``."""
        url = self.require_secret("rest_url").rstrip("/")
        return await self._post(url, [str(a) for a in args])

    async def _post(self, url: str, body: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.require_secret('rest_token')}"}
        try:
            response = await self.request("POST", url, json=body, headers=headers)
        except ProviderError as exc:
            if exc.status_code == 400 and "max" in exc.message.lower() and "limit" in exc.message.lower():
                raise QuotaExhausted(exc.message, provider=self.name, status_code=429) from exc
            raise
        payload = response.json()
        if isinstance(payload, dict):
            if "error" in payload:
                raise ProviderError(str(payload["error"]), provider=self.name, status_code=400)
            return payload.get("result")
        return payload

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        """Run several commands in one HTTP round trip (``/pipeline``); errors become exceptions."""
        url = self.require_secret("rest_url").rstrip("/") + "/pipeline"
        results = await self._post(url, [[str(a) for a in cmd] for cmd in commands])
        out = []
        for item in results or []:
            if isinstance(item, dict) and "error" in item:
                raise ProviderError(str(item["error"]), provider=self.name, status_code=400)
            out.append(item.get("result") if isinstance(item, dict) else item)
        return out

    @staticmethod
    def _tabulate(name: str, result: Any, limit: int, backend: str, started: float) -> QueryResult:
        truncated = False
        if name in _SCAN_COMMANDS and isinstance(result, list) and len(result) == 2:
            cursor, items = result
            truncated = str(cursor) != "0"
            if name in ("HSCAN", "ZSCAN"):
                columns = ["field", "value"] if name == "HSCAN" else ["member", "score"]
                rows = [[items[i], items[i + 1] if i + 1 < len(items) else None] for i in range(0, len(items), 2)]
            else:
                columns, rows = ["key" if name == "SCAN" else "member"], [[k] for k in items]
        elif name in _PAIR_COMMANDS and isinstance(result, list):
            columns = ["field", "value"]
            rows = [[result[i], result[i + 1] if i + 1 < len(result) else None] for i in range(0, len(result), 2)]
        elif isinstance(result, list):
            columns, rows = ["value"], [[jsonable(v)] for v in result]
        else:
            columns, rows = ["result"], [[jsonable(result)]]
        if len(rows) > limit:
            rows, truncated = rows[:limit], True
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), elapsed_ms=elapsed_ms(started),
                           backend=backend, truncated=truncated)

    async def _scan(self, match: str, max_keys: int) -> tuple[list[str], bool]:
        cursor, keys = "0", []
        while True:
            cursor, batch = await self.command("SCAN", cursor, "MATCH", match, "COUNT", 500)
            keys.extend(batch)
            if str(cursor) == "0":
                return keys, False
            if len(keys) >= max_keys:
                return keys[:max_keys], True

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self.command("PING"))

    async def list_tables(self) -> list[TableInfo]:
        delimiter = str(self.setting("key_delimiter", ":"))
        root = str(self.setting("root_keyspace", "(root)"))
        keys, _ = await self._scan("*", int(self.setting("max_scan_keys", 10_000)))
        groups: dict[str, int] = {}
        for key in keys:
            name = key.split(delimiter, 1)[0] + delimiter + "*" if delimiter in key else root
            groups[name] = groups.get(name, 0) + 1
        return [TableInfo(name=n, kind="keyspace", row_count=c) for n, c in sorted(groups.items())]

    async def describe(self, table: str) -> TableInfo:
        delimiter = str(self.setting("key_delimiter", ":"))
        root = str(self.setting("root_keyspace", "(root)"))
        keys, _ = await self._scan("*" if table == root else table, int(self.setting("max_scan_keys", 10_000)))
        if table == root:
            keys = [k for k in keys if delimiter not in k]
        columns = [
            ColumnInfo(name="key", type="string", nullable=False, primary_key=True),
            ColumnInfo(name="type", type="string", nullable=False),
            ColumnInfo(name="ttl", type="int"),
        ]
        return TableInfo(name=table, kind="keyspace", row_count=len(keys), columns=columns)

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        args = self._parse(statement, params)
        name = args[0].upper()
        if name not in READ_COMMANDS:
            raise ProviderError(f"{name} is not a read command; use execute()", provider=self.name,
                                status_code=400)
        result = await self.command(*args)
        return self._tabulate(name, result, limit, self.name, started)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        args = self._parse(statement, params)
        name = args[0].upper()
        result = await self.command(*args)
        out = self._tabulate(name, result, 10_000, self.name, started)
        if isinstance(result, int) and not isinstance(result, bool):
            out.affected_rows = result
        elif result == "OK":
            out.affected_rows = 1
        return out

    async def usage(self) -> QuotaInfo:
        info, dbsize = await self.pipeline([["INFO"], ["DBSIZE"]])
        match = _USED_MEMORY.search(str(info or ""))
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(provider=self.name, used=float(match.group(1)) if match else 0.0, limit=limit,
                         unit="bytes", window="total", source="provider",
                         extra={"keys": dbsize, "used_memory_reported": bool(match)})
