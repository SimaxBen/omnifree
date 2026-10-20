"""Helpers shared by the database adapters in this group (private module)."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.interfaces import (
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    QueryResult,
)


def jsonable(value: Any) -> Any:
    """Convert driver values (Decimal, UUID, bytes, ObjectId, records...) to JSON-safe values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return str(value)


def documents_to_result(
    docs: Iterable[Mapping[str, Any]], *, limit: int, backend: str, started: float
) -> QueryResult:
    """Tabulate documents: columns are the union of keys in first-seen order."""
    docs = list(docs)
    truncated = len(docs) > limit
    docs = docs[:limit]
    columns: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        for key in doc:
            if key not in seen:
                seen.add(key)
                columns.append(str(key))
    rows = [[jsonable(doc.get(col)) for col in columns] for doc in docs]
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        elapsed_ms=elapsed_ms(started),
        backend=backend,
        truncated=truncated,
    )


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def parse_json_statement(statement: str, provider: str) -> Any:
    try:
        return json.loads(statement)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"statement must be JSON: {exc}", provider=provider, status_code=400) from exc


def _class_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


_AUTH_NAMES = {
    "InvalidPasswordError",
    "InvalidAuthorizationSpecificationError",
    "InsufficientPrivilegeError",
}
_UNAVAILABLE_NAMES = {
    "CannotConnectNowError",
    "TooManyConnectionsError",
    "ConnectionDoesNotExistError",
    "InterfaceError",
    "ConnectionFailure",
    "AutoReconnect",
    "NetworkTimeout",
    "ServerSelectionTimeoutError",
    "ConnectionError",
    "OSError",
}


def translate_driver_error(exc: BaseException, provider: str) -> ProviderError:
    """Map asyncpg / pymongo exceptions (matched by class name, no import) to OmniFree errors."""
    if isinstance(exc, ProviderError):
        return exc
    names = _class_names(exc)
    message = f"{type(exc).__name__}: {exc}"[:500]
    if isinstance(exc, TimeoutError) or "ExecutionTimeout" in names or "NetworkTimeout" in names:
        return ProviderTimeout(message, provider=provider)
    if names & _AUTH_NAMES:
        return ProviderAuthError(message, provider=provider, status_code=401)
    code = getattr(exc, "code", None)
    if "OperationFailure" in names and code in (13, 18, 8000):
        return ProviderAuthError(message, provider=provider, status_code=401)
    if "ConfigurationError" in names or "InvalidURI" in names:
        return NotConfigured(message, provider=provider)
    if names & _UNAVAILABLE_NAMES:
        return ProviderUnavailable(message, provider=provider)
    return ProviderError(message, provider=provider, status_code=400)
