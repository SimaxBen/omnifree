"""/db: database explorer (providers, tables, describe, query) with read-only default.

``POST /db/{provider}/query`` calls the adapter's read-only ``query()`` unless
``write: true`` is sent, in which case ``execute()`` is used. For SQL adapters
(capability ``sql``) the hub additionally refuses, in read mode, statements
that contain data/schema-modifying keywords or more than one statement.
Non-SQL adapters (Redis, Mongo, DynamoDB PartiQL, Cloudant ...) take their
native statement format; their ``query()`` contract is read-only.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.interfaces import Capability, ProviderError, ProviderKind
from hub_api.deps import spec_of_kind, with_adapter
from hub_api.state import HubState, get_state

__all__ = ["router", "QueryBody", "sql_write_reason"]

router = APIRouter(prefix="/db", tags=["db"])

_WRITE_KEYWORDS = frozenset(
    {
        # Kept to words that are rarely bare identifiers (quoted identifiers are ignored),
        # so "SELECT comment FROM posts" passes while data-modifying CTEs are caught.
        "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "DROP", "ALTER", "CREATE", "TRUNCATE",
        "GRANT", "REVOKE", "CALL", "EXEC", "EXECUTE", "VACUUM", "ATTACH", "DETACH", "REINDEX",
        "INTO",  # SELECT ... INTO new_table
    }
)
_READ_STARTERS = frozenset({"SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "PRAGMA", "VALUES", "TABLE"})

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def sql_write_reason(statement: str) -> str | None:
    """Why ``statement`` is not a single read-only SQL statement (None = looks read-only)."""
    scrubbed = _STRING_RE.sub(" '' ", _COMMENT_RE.sub(" ", statement))
    parts = [p.strip() for p in scrubbed.split(";") if p.strip()]
    if not parts:
        return "empty statement"
    if len(parts) > 1:
        return "multiple statements"
    words = [w.upper() for w in _WORD_RE.findall(parts[0])]
    if not words:
        return "empty statement"
    if words[0] not in _READ_STARTERS:
        return f"statement starts with {words[0]}"
    if words[0] == "PRAGMA" and "=" in parts[0]:
        return "PRAGMA assignment"
    hits = sorted({w for w in words if w in _WRITE_KEYWORDS})
    if hits:
        return f"contains {', '.join(hits)}"
    return None


class QueryBody(BaseModel):
    statement: str = Field(min_length=1, max_length=100_000)
    params: list[Any] | dict[str, Any] | None = None
    limit: int = Field(default=500, ge=1, le=100_000)
    write: bool = False  # explicit opt-in for mutating statements (adapter.execute)


@router.get("/providers")
async def providers(usage: bool = False, state: HubState = Depends(get_state)) -> dict[str, Any]:
    specs = state.registry.list(ProviderKind.DB)
    out: list[dict[str, Any]] = []
    usages: dict[str, Any] = {}

    async def one_usage(name: str) -> None:
        try:
            q = await with_adapter(state, name, lambda a: a.usage())
            usages[name] = {**q.model_dump(mode="json"), "fraction_used": q.fraction_used}
        except ProviderError as exc:
            usages[name] = {"error": str(exc)}

    if usage:
        await asyncio.gather(*(one_usage(s.name) for s in specs if s.enabled))
    for spec in specs:
        meta = state.registry.class_meta(spec.name)
        out.append(
            {
                "name": spec.name,
                "enabled": spec.enabled,
                "group": spec.group,
                "display_name": meta.get("display_name") or spec.name,
                "description": spec.description,
                "available": meta.get("available", False),
                "capabilities": meta.get("capabilities", []),
                "sql": Capability.SQL in meta.get("capabilities", []),
                "missing_secrets": state.registry.missing_secrets(spec.name),
                "usage": usages.get(spec.name),
            }
        )
    return {"providers": out}


@router.get("/{provider}/usage")
async def provider_usage(provider: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.DB)
    q = await with_adapter(state, provider, lambda a: a.usage())
    return {**q.model_dump(mode="json"), "remaining": q.remaining, "fraction_used": q.fraction_used}


@router.get("/{provider}/tables")
async def tables(provider: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.DB)
    items = await with_adapter(state, provider, lambda a: a.list_tables(), timeout_s=state.hub.db_timeout_s)
    return {"provider": provider, "items": [t.model_dump(mode="json") for t in items]}


@router.get("/{provider}/tables/{table:path}")
async def describe(provider: str, table: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.DB)
    info = await with_adapter(state, provider, lambda a: a.describe(table), timeout_s=state.hub.db_timeout_s)
    return info.model_dump(mode="json")


@router.post("/{provider}/query")
async def query(provider: str, body: QueryBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.DB)
    adapter = await state.router.adapter(provider)
    timeout = state.hub.db_timeout_s
    if body.write:
        result = await with_adapter(state, provider, lambda a: a.execute(body.statement, body.params), timeout_s=timeout)
    else:
        if adapter.supports(Capability.SQL):
            reason = sql_write_reason(body.statement)
            if reason:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"read-only mode refused the statement ({reason}); resend with write=true to execute it",
                )
        limit = min(body.limit, state.hub.db_max_rows)
        result = await with_adapter(
            state, provider, lambda a: a.query(body.statement, body.params, limit), timeout_s=timeout
        )
        if len(result.rows) > limit:
            result.rows = result.rows[:limit]
            result.truncated = True
    result.backend = result.backend or provider
    return {**result.model_dump(mode="json"), "mode": "write" if body.write else "read"}
