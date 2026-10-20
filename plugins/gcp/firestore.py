"""Firestore (native mode) through the REST API v1. Collections map to tables.

Base: ``https://firestore.googleapis.com/v1/projects/{project}/databases/{database}/documents``.

``query(statement)`` accepts (statement is NOT SQL):

* a collection path, e.g. ``users`` or ``users/alice/orders``: lists the documents;
* a JSON command object:
  - ``{"get": "users/alice"}``: one document;
  - ``{"collection": "users", "where": [["age", ">=", 18]], "order_by": [["age", "desc"]],
    "select": ["name", "age"], "limit": 20, "parent": "users/alice", "all_descendants": false}``:
    a structured query (operators ``== != < <= > >= in not-in array-contains array-contains-any``);
  - ``{"structuredQuery": {...}, "parent": "users/alice"}``: a raw runQuery body.

``execute(statement)`` takes a JSON object (or a JSON array of them, applied in order):

* ``{"op": "create", "collection": "users", "id": "alice", "data": {...}}`` (``id`` optional);
* ``{"op": "set", "path": "users/alice", "data": {...}}`` (replace the whole document);
* ``{"op": "update", "path": "users/alice", "data": {...}}`` (only the given fields; must exist);
* ``{"op": "delete", "path": "users/alice"}``.

When ``params`` is a dict it is merged into the top-level JSON command.
Rows have the columns ``_id`` and ``_path`` followed by the document fields.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote

from core.interfaces import (
    ColumnInfo,
    DBAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    QueryResult,
    QuotaInfo,
    TableInfo,
)
from plugins.gcp._auth import GCPAuthMixin

_API = "https://firestore.googleapis.com/v1"

_OPERATORS = {
    "==": "EQUAL",
    "!=": "NOT_EQUAL",
    "<": "LESS_THAN",
    "<=": "LESS_THAN_OR_EQUAL",
    ">": "GREATER_THAN",
    ">=": "GREATER_THAN_OR_EQUAL",
    "in": "IN",
    "not-in": "NOT_IN",
    "array-contains": "ARRAY_CONTAINS",
    "array-contains-any": "ARRAY_CONTAINS_ANY",
}


# --------------------------------------------------------------------------- value codec


def encode_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, datetime):
        return {"timestampValue": value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return {"bytesValue": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {str(k): encode_value(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(v) for v in value]}}
    raise ProviderError(f"cannot encode {type(value).__name__} for Firestore", status_code=400)


def decode_value(value: dict[str, Any]) -> Any:
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return value["stringValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "bytesValue" in value:
        return value["bytesValue"]
    if "referenceValue" in value:
        return value["referenceValue"]
    if "geoPointValue" in value:
        return dict(value["geoPointValue"])
    if "mapValue" in value:
        return {k: decode_value(v) for k, v in (value["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in value:
        return [decode_value(v) for v in value["arrayValue"].get("values") or []]
    return None


def _type_name(value: dict[str, Any]) -> str:
    return next(iter(value), "nullValue").removesuffix("Value")


def _enc_path(path: str) -> str:
    return "/".join(quote(seg, safe="") for seg in path.strip("/").split("/") if seg)


class FirestoreAdapter(GCPAuthMixin, DBAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Google Firestore",
        description="Document DB, 1GB storage + 50k reads/20k writes per day free.",
        required_secrets=("service_account_json",),
        docs_url="https://cloud.google.com/firestore/docs/reference/rest",
    )

    def _db_root(self) -> str:
        api = str(self.setting("api_base", _API)).rstrip("/")
        database = str(self.setting("database", "(default)"))
        return f"{api}/projects/{self.project_id()}/databases/{quote(database, safe='()')}/documents"

    def _parent_url(self, parent: str | None) -> str:
        root = self._db_root()
        return f"{root}/{_enc_path(parent)}" if parent else root

    # ------------------------------------------------------------------ helpers
    def _doc_row(self, doc: dict[str, Any]) -> dict[str, Any]:
        name = str(doc.get("name", ""))
        rel = name.split("/documents/", 1)[-1]
        row: dict[str, Any] = {"_id": rel.rsplit("/", 1)[-1], "_path": rel}
        for key, value in (doc.get("fields") or {}).items():
            row[key] = decode_value(value)
        return row

    @staticmethod
    def _rows_to_result(rows: list[dict[str, Any]], backend: str, elapsed: float, limit: int | None) -> QueryResult:
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        truncated = limit is not None and len(rows) > limit
        if limit is not None:
            rows = rows[:limit]
        return QueryResult(
            columns=columns,
            rows=[[r.get(c) for c in columns] for r in rows],
            row_count=len(rows),
            elapsed_ms=elapsed,
            backend=backend,
            truncated=truncated,
        )

    def _parse_command(self, statement: str, params: Any) -> Any:
        text = statement.strip()
        if not text.startswith(("{", "[")):
            return text
        try:
            command = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON command: {exc}", provider=self.name, status_code=400) from exc
        if isinstance(params, dict) and isinstance(command, dict):
            command = {**command, **params}
        return command

    def _structured_query(self, cmd: dict[str, Any], limit: int) -> dict[str, Any]:
        query: dict[str, Any] = {
            "from": [{"collectionId": cmd["collection"], "allDescendants": bool(cmd.get("all_descendants"))}]
        }
        filters = []
        for clause in cmd.get("where") or []:
            if not isinstance(clause, (list, tuple)) or len(clause) != 3:
                raise ProviderError("where clauses are [field, op, value]", provider=self.name, status_code=400)
            field, op, value = clause
            if op not in _OPERATORS:
                raise ProviderError(f"unsupported operator {op!r}", provider=self.name, status_code=400)
            filters.append(
                {"fieldFilter": {"field": {"fieldPath": field}, "op": _OPERATORS[op], "value": encode_value(value)}}
            )
        if len(filters) == 1:
            query["where"] = filters[0]
        elif filters:
            query["where"] = {"compositeFilter": {"op": "AND", "filters": filters}}
        order = cmd.get("order_by")
        if order:
            items = [order] if isinstance(order, str) else order
            query["orderBy"] = [
                {
                    "field": {"fieldPath": item if isinstance(item, str) else item[0]},
                    "direction": "DESCENDING"
                    if not isinstance(item, str) and len(item) > 1 and str(item[1]).lower().startswith("desc")
                    else "ASCENDING",
                }
                for item in items
            ]
        if cmd.get("select"):
            query["select"] = {"fields": [{"fieldPath": f} for f in cmd["select"]]}
        query["limit"] = int(min(int(cmd.get("limit", limit + 1)), limit + 1))
        return query

    async def _run_query(self, parent: str | None, structured: dict[str, Any]) -> list[dict[str, Any]]:
        results = await self.gcp_call(
            "POST", f"{self._parent_url(parent)}:runQuery", json={"structuredQuery": structured}
        )
        return [self._doc_row(r["document"]) for r in results or [] if isinstance(r, dict) and r.get("document")]

    async def _list_documents(self, collection_path: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": min(limit + 1, 300)}
        while len(rows) <= limit:
            body = await self.gcp_call("GET", f"{self._db_root()}/{_enc_path(collection_path)}", params=params)
            rows.extend(self._doc_row(d) for d in body.get("documents") or [])
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return rows

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(
            lambda: self.gcp_call("POST", f"{self._db_root()}:listCollectionIds", json={"pageSize": 1})
        )

    async def list_tables(self) -> list[TableInfo]:
        names: list[str] = []
        body: dict[str, Any] = {"pageSize": 300}
        while True:
            result = await self.gcp_call("POST", f"{self._db_root()}:listCollectionIds", json=body)
            names.extend(result.get("collectionIds") or [])
            token = result.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return [TableInfo(name=n, kind="collection") for n in sorted(names)]

    async def describe(self, table: str) -> TableInfo:
        sample_size = int(self.setting("describe_sample_size", 25))
        parent, _, collection = table.strip("/").rpartition("/")
        results = await self.gcp_call(
            "POST",
            f"{self._parent_url(parent or None)}:runQuery",
            json={"structuredQuery": {"from": [{"collectionId": collection}], "limit": sample_size}},
        )
        types: dict[str, set[str]] = {}
        seen: dict[str, int] = {}
        docs = [r["document"] for r in results or [] if isinstance(r, dict) and r.get("document")]
        for doc in docs:
            for key, value in (doc.get("fields") or {}).items():
                types.setdefault(key, set()).add(_type_name(value))
                seen[key] = seen.get(key, 0) + 1
        count = await self._count(parent or None, collection)
        columns = [ColumnInfo(name="_id", type="string", nullable=False, primary_key=True)]
        columns += [
            ColumnInfo(name=k, type="|".join(sorted(types[k])), nullable=seen[k] < len(docs) or "null" in types[k])
            for k in types
        ]
        return TableInfo(name=table, kind="collection", row_count=count, columns=columns)

    async def _count(self, parent: str | None, collection: str) -> int | None:
        body = {
            "structuredAggregationQuery": {
                "structuredQuery": {"from": [{"collectionId": collection}]},
                "aggregations": [{"alias": "n", "count": {}}],
            }
        }
        try:
            results = await self.gcp_call("POST", f"{self._parent_url(parent)}:runAggregationQuery", json=body)
        except ProviderError as exc:
            if exc.status_code in (400, 404):
                return None
            raise
        for entry in results or []:
            fields = ((entry or {}).get("result") or {}).get("aggregateFields") or {}
            if "n" in fields:
                return int(decode_value(fields["n"]) or 0)
        return None

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        start = time.perf_counter()
        command = self._parse_command(statement, params)
        if isinstance(command, str):
            rows = await self._list_documents(command, limit)
        elif isinstance(command, dict) and "get" in command:
            try:
                doc = await self.gcp_call("GET", f"{self._db_root()}/{_enc_path(str(command['get']))}")
            except ProviderError as exc:
                if exc.status_code != 404:
                    raise
                doc = None
            rows = [self._doc_row(doc)] if doc else []
        elif isinstance(command, dict) and "structuredQuery" in command:
            rows = await self._run_query(command.get("parent"), dict(command["structuredQuery"]))
        elif isinstance(command, dict) and "collection" in command:
            rows = await self._run_query(command.get("parent"), self._structured_query(command, limit))
        else:
            raise ProviderError(
                "query expects a collection path or a JSON object with get/collection/structuredQuery",
                provider=self.name,
                status_code=400,
            )
        return self._rows_to_result(rows, self.name, (time.perf_counter() - start) * 1000, limit)

    async def _write(self, cmd: dict[str, Any]) -> dict[str, Any] | None:
        op = str(cmd.get("op", "")).lower()
        fields = {str(k): encode_value(v) for k, v in (cmd.get("data") or {}).items()}
        root = self._db_root()
        if op == "create":
            collection = cmd.get("collection")
            if not collection:
                raise ProviderError("create needs 'collection'", provider=self.name, status_code=400)
            params = {"documentId": cmd["id"]} if cmd.get("id") else None
            return await self.gcp_call("POST", f"{root}/{_enc_path(collection)}", params=params,
                                       json={"fields": fields})
        path = cmd.get("path")
        if not path:
            raise ProviderError(f"{op or 'write'} needs 'path'", provider=self.name, status_code=400)
        url = f"{root}/{_enc_path(path)}"
        if op == "set":
            return await self.gcp_call("PATCH", url, json={"fields": fields})
        if op == "update":
            params_list = [("updateMask.fieldPaths", k) for k in fields] + [("currentDocument.exists", "true")]
            return await self.gcp_call("PATCH", url, params=params_list, json={"fields": fields})
        if op == "delete":
            await self.gcp_call("DELETE", url)
            return None
        raise ProviderError(f"unknown op {op!r} (create|set|update|delete)", provider=self.name, status_code=400)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        start = time.perf_counter()
        command = self._parse_command(statement, params)
        commands = command if isinstance(command, list) else [command]
        rows: list[dict[str, Any]] = []
        for cmd in commands:
            if not isinstance(cmd, dict):
                raise ProviderError("execute expects a JSON object or array of objects", provider=self.name,
                                    status_code=400)
            doc = await self._write(cmd)
            if doc:
                rows.append(self._doc_row(doc))
        result = self._rows_to_result(rows, self.name, (time.perf_counter() - start) * 1000, None)
        result.affected_rows = len(commands)
        return result

    async def usage(self) -> QuotaInfo:
        """Firestore's REST API does not report stored bytes (only Cloud Monitoring does),
        so this returns the configured capacity with ``source='config'``."""
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(
            provider=self.name,
            used=0.0,
            limit=limit,
            unit="bytes",
            window="total",
            source="config",
            extra={"note": "storage size is only exposed through Cloud Monitoring"},
        )
