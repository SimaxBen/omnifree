"""MongoDB Atlas (M0) adapter; ``motor`` is imported lazily.

Statements are **JSON database command documents** (MongoDB Extended JSON is accepted
when ``bson`` is installed), run with ``db.command(...)`` on ``settings.database``:

* ``query()`` (read-only): ``{"find": "users", "filter": {"age": {"$gt": 30}}}``,
  ``{"aggregate": "orders", "pipeline": [...]}`` (no ``$out``/``$merge``), ``{"count": ...}``,
  ``{"distinct": ...}``, ``{"listCollections": 1}``, ``{"dbStats": 1}``, ``{"collStats": ...}``.
* ``execute()``: any command, e.g. ``{"insert": "users", "documents": [{...}]}``,
  ``{"update": ..., "updates": [...]}``, ``{"delete": ..., "deletes": [...]}``, ``{"create": "logs"}``.

Collections and views map to ``TableInfo(kind="collection"|"view")``; ``describe()`` infers
columns from a ``$sample`` of documents.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, ClassVar

from core.interfaces import (
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

from ._common import documents_to_result, elapsed_ms, jsonable, translate_driver_error

READ_COMMANDS = {
    "find", "aggregate", "count", "distinct", "listcollections", "listindexes",
    "dbstats", "collstats", "ping", "buildinfo", "serverstatus", "hello", "ismaster",
}
_WRITE_STAGES = {"$out", "$merge"}


def _bson_type(value: Any) -> str:
    name = type(value).__name__
    return {
        "str": "string", "int": "int", "float": "double", "bool": "bool", "dict": "object",
        "list": "array", "NoneType": "null", "datetime": "date", "ObjectId": "objectId",
        "Decimal128": "decimal", "Int64": "long", "bytes": "binData",
    }.get(name, name)


class MongoAtlasAdapter(DBAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="MongoDB Atlas",
        description="MongoDB Atlas via motor; statements are JSON command documents.",
        required_secrets=("uri",),
        docs_url="https://www.mongodb.com/docs/manual/reference/command/",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._client: Any = None

    # ------------------------------------------------------------------ plumbing
    def _db(self) -> Any:
        if self._client is None:
            uri = self.require_secret("uri")
            try:
                from motor.motor_asyncio import AsyncIOMotorClient  # lazy heavy import
            except ImportError as exc:
                raise NotConfigured("pip install motor", provider=self.name) from exc
            try:
                self._client = AsyncIOMotorClient(
                    uri,
                    serverSelectionTimeoutMS=int(self.context.timeout_s * 1000),
                    appname="omnifree",
                )
            except Exception as exc:
                raise translate_driver_error(exc, self.name) from exc
        return self._client[str(self.setting("database", "omnifree"))]

    async def _command(self, command: dict[str, Any]) -> dict[str, Any]:
        db = self._db()
        try:
            result = await db.command(command)
        except Exception as exc:
            raise translate_driver_error(exc, self.name) from exc
        return dict(result)

    def _parse(self, statement: str) -> dict[str, Any]:
        try:
            from bson import json_util  # ships with pymongo; enables Extended JSON
        except ImportError:
            loader = lambda text: json.loads(text, object_pairs_hook=OrderedDict)  # noqa: E731
        else:
            loader = json_util.loads
        try:
            doc = loader(statement)
        except (ValueError, TypeError) as exc:
            raise ProviderError(f"statement must be a JSON command document: {exc}",
                                provider=self.name, status_code=400) from exc
        if not isinstance(doc, dict) or not doc:
            raise ProviderError("statement must be a non-empty JSON object", provider=self.name,
                                status_code=400)
        return dict(doc)

    @staticmethod
    def _cursor_docs(result: dict[str, Any]) -> list[dict[str, Any]] | None:
        cursor = result.get("cursor")
        if isinstance(cursor, dict) and "firstBatch" in cursor:
            return [dict(d) for d in cursor["firstBatch"]]
        return None

    def _tabulate(self, result: dict[str, Any], limit: int, started: float) -> QueryResult:
        docs = self._cursor_docs(result)
        if docs is None:
            if "values" in result and isinstance(result["values"], list):  # distinct
                docs = [{"value": v} for v in result["values"]]
            else:
                docs = [{k: v for k, v in result.items() if k not in ("ok", "$clusterTime", "operationTime")}]
        return documents_to_result(docs, limit=limit, backend=self.name, started=started)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._command({"ping": 1}))

    async def list_tables(self) -> list[TableInfo]:
        result = await self._command({"listCollections": 1, "nameOnly": False})
        docs = self._cursor_docs(result) or []
        return sorted(
            (
                TableInfo(name=str(d.get("name")), kind="view" if d.get("type") == "view" else "collection")
                for d in docs
                if not str(d.get("name", "")).startswith("system.")
            ),
            key=lambda t: t.name,
        )

    async def describe(self, table: str) -> TableInfo:
        listing = await self._command({"listCollections": 1, "filter": {"name": table}})
        found = self._cursor_docs(listing) or []
        if not found:
            raise ProviderError(f"collection {table} not found", provider=self.name, status_code=404)
        kind = "view" if found[0].get("type") == "view" else "collection"
        sample_size = int(self.setting("describe_sample", 20))
        count_res, sample_res = await asyncio.gather(
            self._command({"count": table}),
            self._command({"aggregate": table, "pipeline": [{"$sample": {"size": sample_size}}],
                           "cursor": {"batchSize": sample_size}}),
        )
        size_bytes = None
        if kind == "collection":
            try:
                stats = await self._command({"collStats": table})
                size_bytes = int(stats.get("storageSize", 0)) + int(stats.get("totalIndexSize", 0))
            except ProviderError:
                size_bytes = None
        types: OrderedDict[str, set[str]] = OrderedDict()
        docs = self._cursor_docs(sample_res) or []
        for doc in docs:
            for key, value in doc.items():
                types.setdefault(key, set()).add(_bson_type(value))
        columns = [
            ColumnInfo(name=key, type="|".join(sorted(kinds)),
                       nullable=key != "_id" and (len([d for d in docs if key in d]) < len(docs) or "null" in kinds),
                       primary_key=key == "_id")
            for key, kinds in types.items()
        ]
        return TableInfo(name=table, kind=kind, row_count=int(count_res.get("n", 0)),
                         size_bytes=size_bytes, columns=columns)

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        command = self._parse(statement)
        if isinstance(params, dict):
            command.update(params)
        name = next(iter(command)).lower()
        if name not in READ_COMMANDS:
            raise ProviderError(f"'{next(iter(command))}' is not a read command; use execute()",
                                provider=self.name, status_code=400)
        if name == "aggregate":
            stages = {key for stage in command.get("pipeline") or [] for key in stage}
            if stages & _WRITE_STAGES:
                raise ProviderError("$out/$merge are writes; use execute()", provider=self.name,
                                    status_code=400)
            command.setdefault("cursor", {})
            command["cursor"]["batchSize"] = limit + 1
        if name == "find":
            command["limit"] = min(int(command.get("limit") or limit + 1), limit + 1)
            command["batchSize"] = limit + 1
            command["singleBatch"] = True
        return self._tabulate(await self._command(command), limit, started)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        command = self._parse(statement)
        if isinstance(params, dict):
            command.update(params)
        result = await self._command(command)
        affected = result.get("nModified", result.get("n"))
        out = self._tabulate(result, 10_000, started)
        out.affected_rows = None if affected is None else int(affected)
        out.elapsed_ms = elapsed_ms(started)
        return out

    async def usage(self) -> QuotaInfo:
        stats = await self._command({"dbStats": 1})
        used = float(stats.get("storageSize", 0)) + float(stats.get("indexSize", 0))
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        extra = {k: jsonable(stats[k]) for k in ("dataSize", "storageSize", "indexSize", "objects", "collections")
                 if k in stats}
        return QuotaInfo(provider=self.name, used=used, limit=limit, unit="bytes", window="total",
                         source="provider", extra=extra)

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            client.close()
        await super().close()
