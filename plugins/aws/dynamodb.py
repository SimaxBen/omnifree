"""AWS DynamoDB (Always Free 25GB) through boto3, imported lazily and run in worker threads.

Statements are **PartiQL** (``SELECT * FROM "users" WHERE pk = ?``) executed with
``ExecuteStatement``; ``params`` is a positional list of plain Python values (converted to
DynamoDB AttributeValues). Tables map to ``TableInfo(kind="table")``.

Settings: ``region`` (required), ``endpoint_url`` (optional, e.g. DynamoDB Local),
``tables`` (optional allow-list for ``list_tables``/``usage``).
"""

from __future__ import annotations

import asyncio
import base64
import time
from decimal import Decimal
from typing import Any, ClassVar

from core.interfaces import (
    ColumnInfo,
    DBAdapter,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailable,
    QueryResult,
    QuotaExhausted,
    QuotaInfo,
    TableInfo,
)

_QUOTA_CODES = {"ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded",
                "LimitExceededException"}
_AUTH_CODES = {"UnrecognizedClientException", "InvalidSignatureException", "AccessDeniedException",
               "MissingAuthenticationTokenException", "ExpiredTokenException", "IncompleteSignature"}
_UNAVAILABLE_CODES = {"InternalServerError", "ServiceUnavailable", "InternalFailure"}

_TYPE_NAMES = {"S": "string", "N": "number", "B": "binary"}


# --------------------------------------------------------------------------- AttributeValue codec


def to_attr(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (bytes, bytearray)):
        return {"B": bytes(value)}
    if isinstance(value, dict):
        return {"M": {str(k): to_attr(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [to_attr(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        items = list(value)
        if items and all(isinstance(i, str) for i in items):
            return {"SS": items}
        if items and all(isinstance(i, (int, float, Decimal)) and not isinstance(i, bool) for i in items):
            return {"NS": [str(i) for i in items]}
    raise ProviderError(f"cannot convert {type(value).__name__} to a DynamoDB value", status_code=400)


def _number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


def from_attr(attr: dict[str, Any]) -> Any:
    (kind, value), = attr.items()
    if kind == "NULL":
        return None
    if kind in ("S", "BOOL"):
        return value
    if kind == "N":
        return _number(value)
    if kind == "B":
        return base64.b64encode(value).decode("ascii") if isinstance(value, (bytes, bytearray)) else value
    if kind == "M":
        return {k: from_attr(v) for k, v in value.items()}
    if kind == "L":
        return [from_attr(v) for v in value]
    if kind == "SS":
        return sorted(value)
    if kind == "NS":
        return sorted(_number(v) for v in value)
    if kind == "BS":
        return [base64.b64encode(v).decode("ascii") for v in value]
    return value


class DynamoDBAdapter(DBAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="AWS DynamoDB",
        description="NoSQL key-value store, Always Free 25GB + 25 RCU/WCU. PartiQL statements.",
        required_secrets=("access_key_id", "secret_access_key"),
        docs_url="https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ql-reference.html",
    )

    _client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            # Validate configuration first so an unconfigured adapter never imports boto3.
            kwargs: dict[str, Any] = {
                "region_name": self.require_setting("region"),
                "aws_access_key_id": self.require_secret("access_key_id"),
                "aws_secret_access_key": self.require_secret("secret_access_key"),
            }
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise NotConfigured("pip install boto3", provider=self.name) from exc
            if self.setting("endpoint_url"):
                kwargs["endpoint_url"] = self.setting("endpoint_url")
            self._client = boto3.client("dynamodb", **kwargs)
        return self._client

    def _map_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        response = getattr(exc, "response", None)
        if isinstance(response, dict) and "Error" in response:
            code = str(response["Error"].get("Code", ""))
            message = f"{code}: {response['Error'].get('Message', '')}"
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if code in _QUOTA_CODES:
                return QuotaExhausted(message, provider=self.name, status_code=status or 429)
            if code in _AUTH_CODES:
                return ProviderAuthError(message, provider=self.name, status_code=status or 403)
            if code == "ResourceNotFoundException":
                return ProviderError(message, provider=self.name, status_code=404)
            if code in _UNAVAILABLE_CODES or (status or 0) >= 500:
                return ProviderUnavailable(message, provider=self.name, status_code=status)
            return ProviderError(message, provider=self.name, status_code=status or 400)
        name = type(exc).__name__
        if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ConnectionClosedError"):
            return ProviderUnavailable(f"{name}: {exc}", provider=self.name)
        if name in ("NoCredentialsError", "PartialCredentialsError"):
            return NotConfigured(f"{name}: {exc}", provider=self.name)
        return ProviderError(f"{name}: {exc}", provider=self.name)

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        client = self._get_client()
        try:
            return await asyncio.to_thread(getattr(client, method), **kwargs)
        except Exception as exc:  # noqa: BLE001 - botocore errors are mapped
            raise self._map_error(exc) from exc

    async def _table_names(self) -> list[str]:
        allow = self.setting("tables")
        if allow:
            return list(allow)
        names: list[str] = []
        kwargs: dict[str, Any] = {"Limit": 100}
        while True:
            page = await self._call("list_tables", **kwargs)
            names.extend(page.get("TableNames") or [])
            last = page.get("LastEvaluatedTableName")
            if not last:
                return names
            kwargs["ExclusiveStartTableName"] = last

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("list_tables", Limit=1))

    async def list_tables(self) -> list[TableInfo]:
        return [TableInfo(name=n, kind="table") for n in await self._table_names()]

    async def describe(self, table: str) -> TableInfo:
        desc = (await self._call("describe_table", TableName=table)).get("Table") or {}
        attr_types = {a["AttributeName"]: a["AttributeType"] for a in desc.get("AttributeDefinitions") or []}
        key_roles = {k["AttributeName"]: k["KeyType"] for k in desc.get("KeySchema") or []}
        columns = [
            ColumnInfo(
                name=name,
                type=_TYPE_NAMES.get(attr_types.get(name, ""), attr_types.get(name, "")),
                nullable=name not in key_roles,
                primary_key=name in key_roles,
                default=None,
            )
            for name in sorted(attr_types, key=lambda n: (n not in key_roles, key_roles.get(n) != "HASH", n))
        ]
        return TableInfo(
            name=table,
            kind="table",
            row_count=desc.get("ItemCount"),
            size_bytes=desc.get("TableSizeBytes"),
            columns=columns,
        )

    @staticmethod
    def _items_to_result(items: list[dict[str, Any]], backend: str, elapsed: float, truncated: bool) -> QueryResult:
        rows_dicts = [{k: from_attr(v) for k, v in item.items()} for item in items]
        columns: list[str] = []
        for row in rows_dicts:
            for key in row:
                if key not in columns:
                    columns.append(key)
        return QueryResult(
            columns=columns,
            rows=[[r.get(c) for c in columns] for r in rows_dicts],
            row_count=len(rows_dicts),
            elapsed_ms=elapsed,
            backend=backend,
            truncated=truncated,
        )

    def _statement_kwargs(self, statement: str, params: list[Any] | dict[str, Any] | None) -> dict[str, Any]:
        if isinstance(params, dict):
            raise ProviderError("PartiQL uses positional '?' parameters (pass a list)", provider=self.name,
                                status_code=400)
        kwargs: dict[str, Any] = {"Statement": statement.strip().rstrip(";")}
        if params:
            kwargs["Parameters"] = [to_attr(p) for p in params]
        return kwargs

    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        start = time.perf_counter()
        # PartiQL ExecuteStatement also runs INSERT/UPDATE/DELETE: query() stays read-only.
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if verb != "SELECT":
            raise ProviderError("query() only runs PartiQL SELECT statements; use execute()",
                                provider=self.name, status_code=400)
        kwargs = self._statement_kwargs(statement, params)
        items: list[dict[str, Any]] = []
        truncated = False
        while True:
            page = await self._call("execute_statement", **kwargs)
            items.extend(page.get("Items") or [])
            token = page.get("NextToken")
            if len(items) > limit:
                truncated = True
                items = items[:limit]
                break
            if not token:
                break
            kwargs["NextToken"] = token
        return self._items_to_result(items, self.name, (time.perf_counter() - start) * 1000, truncated)

    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        start = time.perf_counter()
        page = await self._call("execute_statement", **self._statement_kwargs(statement, params))
        result = self._items_to_result(page.get("Items") or [], self.name, (time.perf_counter() - start) * 1000,
                                       False)
        # PartiQL INSERT/UPDATE/DELETE address exactly one item and fail otherwise.
        upper = statement.lstrip().upper()
        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            result.affected_rows = 1
        return result

    async def usage(self) -> QuotaInfo:
        """Sum of ``TableSizeBytes`` (DynamoDB refreshes it roughly every six hours)."""
        names = await self._table_names()
        descs = await asyncio.gather(*(self._call("describe_table", TableName=n) for n in names))
        per_table = {n: int((d.get("Table") or {}).get("TableSizeBytes") or 0) for n, d in zip(names, descs, strict=True)}
        limit = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(
            provider=self.name,
            used=float(sum(per_table.values())),
            limit=limit,
            unit="bytes",
            window="total",
            source="provider",
            extra={"tables": per_table},
        )

    async def quota(self) -> QuotaInfo:
        return await self.usage()

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            await asyncio.to_thread(client.close)
        await super().close()
