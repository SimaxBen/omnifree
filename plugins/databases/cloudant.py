"""IBM Cloudant (CouchDB HTTP API) adapter with IAM API-key authentication.

The ``apikey`` secret is exchanged at ``settings.iam_url`` (default IBM Cloud IAM) for a
bearer token, cached until shortly before it expires. Databases map to tables.

Statements are JSON:

* ``query()``: Mango ``{"db": "orders", "selector": {"status": "open"}, "fields": [...],
  "sort": [...], "limit": 50}`` (``POST /<db>/_find``), or ``{"db": "orders", "id": "<doc id>"}``
  to read a single document.
* ``execute()``: ``{"db": "orders", "doc": {...}}`` (create/update, include ``_id``/``_rev``
  to update), ``{"db": "orders", "docs": [...]}`` (``_bulk_docs``),
  ``{"db": "orders", "delete": "<doc id>", "rev": "<rev>"}``, ``{"create_db": "orders"}``.

``usage()`` sums ``sizes.<settings.usage_size_field>`` (default ``file``) over all databases.
"""

from __future__ import annotations

import asyncio
import time
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

from ._common import documents_to_result, elapsed_ms, parse_json_statement

DEFAULT_IAM_URL = "https://iam.cloud.ibm.com/identity/token"


def _db_path(db: str) -> str:
    return "/" + quote(db, safe="")


class CloudantAdapter(DBAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="IBM Cloudant",
        description="IBM Cloudant via the CouchDB HTTP API (IAM bearer token).",
        required_secrets=("url", "apikey"),
        docs_url="https://cloud.ibm.com/apidocs/cloudant",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth / transport
    async def _bearer(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expires - 60:
                return self._token
            response = await self.request(
                "POST",
                str(self.setting("iam_url", DEFAULT_IAM_URL)),
                data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                      "apikey": self.require_secret("apikey")},
                headers={"Accept": "application/json"},
            )
            payload = response.json()
            self._token = str(payload["access_token"])
            expiration = payload.get("expiration")
            self._token_expires = (
                float(expiration) if expiration else time.time() + float(payload.get("expires_in", 3600))
            )
            return self._token

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        url = self.require_secret("url").rstrip("/") + path
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {await self._bearer(force=attempt == 1)}",
                       "Accept": "application/json"}
            try:
                response = await self.request(method, url, headers=headers, **kwargs)
            except ProviderError as exc:
                if exc.status_code == 401 and attempt == 0:
                    continue
                raise
            return response.json() if response.content else None
        raise ProviderError("unreachable", provider=self.name)

    async def _all_dbs(self) -> list[str]:
        names = await self._call("GET", "/_all_dbs")
        return [n for n in names or [] if not str(n).startswith("_")]

    async def _dbs_info(self, names: list[str]) -> dict[str, dict[str, Any]]:
        if not names:
            return {}
        try:
            infos = await self._call("POST", "/_dbs_info", json={"keys": names})
        except ProviderError as exc:
            if exc.status_code not in (400, 404, 405):
                raise
            infos = [{"key": n, "info": await self._call("GET", _db_path(n))} for n in names]
        return {str(item.get("key")): item.get("info") or {} for item in infos or []}

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/"))

    async def list_tables(self) -> list[TableInfo]:
        names = await self._all_dbs()
        infos = await self._dbs_info(names)
        out = []
        for name in sorted(names):
            info = infos.get(name, {})
            sizes = info.get("sizes") or {}
            out.append(TableInfo(name=name, kind="table", row_count=info.get("doc_count"),
                                 size_bytes=sizes.get("external")))
        return out

    async def describe(self, table: str) -> TableInfo:
        info = await self._call("GET", _db_path(table))
        sample = await self._call("GET", _db_path(table) + "/_all_docs",
                                  params={"limit": int(self.setting("describe_sample", 20)),
                                          "include_docs": "true"})
        keys: dict[str, set[str]] = {}
        docs = [row.get("doc") or {} for row in (sample or {}).get("rows") or []
                if not str(row.get("id", "")).startswith("_design/")]
        for doc in docs:
            for key, value in doc.items():
                keys.setdefault(key, set()).add(type(value).__name__)
        columns = [ColumnInfo(name=k, type="|".join(sorted(v)), nullable=k not in ("_id", "_rev"),
                              primary_key=k == "_id") for k, v in keys.items()]
        sizes = info.get("sizes") or {}
        return TableInfo(name=table, kind="table", row_count=info.get("doc_count"),
                         size_bytes=sizes.get("external"), columns=columns)

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        started = time.perf_counter()
        spec = parse_json_statement(statement, self.name)
        if not isinstance(spec, dict) or not spec.get("db"):
            raise ProviderError("query statements need a 'db'", provider=self.name, status_code=400)
        if isinstance(params, dict):
            spec.update(params)
        db = str(spec.pop("db"))
        if "id" in spec:
            doc = await self._call("GET", f"{_db_path(db)}/{quote(str(spec['id']), safe='')}")
            return documents_to_result([doc], limit=limit, backend=self.name, started=started)
        spec.setdefault("selector", {})
        spec["limit"] = min(int(spec.get("limit") or limit + 1), limit + 1)
        result = await self._call("POST", f"{_db_path(db)}/_find", json=spec)
        return documents_to_result(result.get("docs") or [], limit=limit, backend=self.name, started=started)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        started = time.perf_counter()
        spec = parse_json_statement(statement, self.name)
        if not isinstance(spec, dict):
            raise ProviderError("execute statements must be JSON objects", provider=self.name, status_code=400)
        if "create_db" in spec:
            result = await self._call("PUT", _db_path(str(spec["create_db"])))
            docs = [result or {"ok": True}]
        elif "delete_db" in spec:
            result = await self._call("DELETE", _db_path(str(spec["delete_db"])))
            docs = [result or {"ok": True}]
        elif spec.get("db") and "docs" in spec:
            docs = await self._call("POST", f"{_db_path(str(spec['db']))}/_bulk_docs", json={"docs": spec["docs"]})
        elif spec.get("db") and "doc" in spec:
            docs = [await self._call("POST", _db_path(str(spec["db"])), json=spec["doc"])]
        elif spec.get("db") and "delete" in spec:
            if not spec.get("rev"):
                raise ProviderError("delete needs the document 'rev'", provider=self.name, status_code=400)
            docs = [await self._call("DELETE", f"{_db_path(str(spec['db']))}/{quote(str(spec['delete']), safe='')}",
                                     params={"rev": spec["rev"]})]
        else:
            raise ProviderError("unknown execute statement (create_db, delete_db, doc, docs, delete)",
                                provider=self.name, status_code=400)
        docs = list(docs or [])
        out = documents_to_result(docs, limit=max(len(docs), 1), backend=self.name, started=started)
        out.affected_rows = sum(1 for d in docs if not d.get("error"))
        out.elapsed_ms = elapsed_ms(started)
        return out

    async def usage(self) -> QuotaInfo:
        field = str(self.setting("usage_size_field", "file"))
        infos = await self._dbs_info(await self._all_dbs())
        used = sum(float((info.get("sizes") or {}).get(field) or 0) for info in infos.values())
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(provider=self.name, used=used, limit=limit, unit="bytes", window="total",
                         source="provider", extra={"databases": len(infos), "size_field": field})
