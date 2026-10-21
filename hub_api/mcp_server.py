"""MCP server: OmniFree tools, resources and prompts for agents (Claude Code, Cursor, Claude Desktop ...).

Served on the hub at ``/mcp`` with the official Python SDK (``mcp`` 2.x, ``MCPServer``) over the
Streamable HTTP transport, in **stateless JSON mode**: every POST is a self-contained JSON-RPC
exchange answered with ``application/json`` (no ``Mcp-Session-Id``, no long-lived SSE stream), so it
runs on serverless functions (Vercel, 60 s per request) as well as on Cloud Run.

Serverless hosting
    The SDK's ``StreamableHTTPSessionManager.run()`` normally lives in the ASGI lifespan, which
    Vercel may never run, and its task group is bound to one event loop. :class:`MCPEndpoint`
    therefore creates a fresh stateless session manager per request and enters ``run()`` around
    that single request: nothing outlives the request, nothing depends on the lifespan. The hub
    router (quota DB) is opened lazily too (``SmartRouter.start`` is idempotent).

Authentication
    ``Authorization: Bearer <token>`` where the token is the hub token (``OMNIFREE_HUB_API_TOKEN`` /
    vault ``HUB_API_TOKEN``) or the optional, independently revocable ``MCP_API_TOKEN`` (env or
    vault). Failures answer 401 with ``WWW-Authenticate: Bearer``; no token configured at all
    answers 503 (fail closed). Browser ``Origin`` headers must be one of the hub CORS origins.

Tools are thin wrappers over the same Python functions the HTTP routes use (no HTTP to itself).
Destructive operations (deletes) are deliberately not exposed; ``tasks_complete`` is the only
state-changing call that is not a create.
"""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from fastapi import FastAPI, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from core.interfaces import ChatMessage, ChatRequest, ProviderError, ProviderKind
from hub_api.appdata import AppDB, appdb_for, local_today
from hub_api.auth import token_matches
from hub_api.budget_report import build_budget
from hub_api.deps import default_provider, spec_of_kind
from hub_api.errors import error_body
from hub_api.routers import chats, cron, drive, expenses, links, llm, notes, rag
from hub_api.state import HubState

__all__ = ["build_mcp_server", "mount_mcp", "MCPEndpoint", "MCP_TOKEN_KEY", "MCP_PATH"]

log = logging.getLogger("omnifree.hub.mcp")

MCP_PATH = "/mcp"
# Vault/env key of the optional agent-only token (revocable without rotating the hub token).
MCP_TOKEN_KEY = "MCP_API_TOKEN"

TEXT_READ_DEFAULT = 100_000
TEXT_READ_MAX = 1_000_000
TEXT_FETCH_MAX = 5 * 1024 * 1024  # never download more than this to answer drive_read_text
NOTE_PREVIEW_CHARS = 400

_TEXT_EXTENSIONS = frozenset(
    "txt md markdown rst csv tsv json jsonl ndjson yaml yml toml ini cfg conf env log xml html htm css js mjs cjs ts tsx "
    "jsx py pyi rb go rs java kt kts swift c h cpp hpp cc cs php sh bash zsh ps1 bat sql graphql proto tf hcl "
    "dockerfile makefile gitignore editorconfig properties srt vtt tex bib svg".split()
)
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
READ_ONLY_OPEN = ToolAnnotations(read_only_hint=True, open_world_hint=True)
CREATES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
UPDATES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = (
    "OmniFree is the user's personal cloud: a pooled free-tier LLM gateway with failover, a unified Drive "
    "across several storage backends, 'Ask my Drive' (RAG with citations), notes, tasks, expenses, short "
    "links, file drops, scheduled HTTP jobs and a free-tier budget. Dates use the Africa/Casablanca time "
    "zone; amounts default to MAD. Nothing here deletes data: deletions are done by the user in the app."
)

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


# --------------------------------------------------------------------------- error mapping


def _detail(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _validation_text(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body") or "input"
        parts.append(f"{loc}: {err.get('msg')}")
    return "invalid arguments: " + "; ".join(parts)


def _tool_error(exc: Exception) -> ToolError | None:
    """The message an agent can act on, for the exceptions the hub routes raise on purpose."""
    if isinstance(exc, HTTPException):
        return ToolError(f"{_detail(exc.detail)} (HTTP {exc.status_code})")
    if isinstance(exc, ProviderError):
        body = error_body(exc)
        provider = f" [provider {body['provider']}]" if body.get("provider") else ""
        return ToolError(f"{body['error']}: {body['detail']}{provider}")
    if isinstance(exc, ValidationError):
        return ToolError(_validation_text(exc))
    if isinstance(exc, ValueError):
        return ToolError(str(exc))
    return None


def _guard(fn: _F) -> _F:
    """Turn route exceptions into ToolError so the agent sees the reason (the SDK hides other errors)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            mapped = _tool_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

    return wrapper  # type: ignore[return-value]


# --------------------------------------------------------------------------- helpers


def _is_text(key: str, content_type: str | None) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("text/") or ct in (
        "application/json", "application/xml", "application/yaml", "application/x-yaml", "application/toml",
        "application/javascript", "application/x-sh", "application/sql", "image/svg+xml", "application/x-ndjson",
    ) or ct.endswith(("+json", "+xml")):
        return True
    name = key.rsplit("/", 1)[-1].lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else name
    return ext in _TEXT_EXTENSIONS


def _decode_text(data: bytes) -> str | None:
    """UTF-8 (BOM tolerated) text, or None for binary content."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 3:  # cut in the middle of a multi-byte character
            return data[: exc.start].decode("utf-8-sig", errors="strict")
        return None


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _note_preview(note: dict[str, Any]) -> dict[str, Any]:
    body = note.get("body") or ""
    out = {k: v for k, v in note.items() if k != "body"}
    out["preview"] = body if len(body) <= NOTE_PREVIEW_CHARS else body[: NOTE_PREVIEW_CHARS - 1].rstrip() + "…"
    out["body_chars"] = len(body)
    return out


def _resolve_due(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    resolved = notes.resolve_due(value, local_today())
    if resolved is None:
        raise ToolError(f"could not understand due date {value!r}: use YYYY-MM-DD or e.g. 'tomorrow', 'friday'")
    return resolved


# --------------------------------------------------------------------------- server


def build_mcp_server(app: FastAPI, *, version: str = "") -> MCPServer:
    """Build the MCP server for one hub app (tools close over its HubState and apps DB)."""
    state: HubState = app.state.hub

    def appdb() -> AppDB:
        return appdb_for(app)

    server: MCPServer = MCPServer(
        "omnifree",
        title="OmniFree",
        description="Personal cloud: LLM pool, Drive, Ask my Drive, notes, tasks, expenses, links, schedules.",
        instructions=INSTRUCTIONS,
        version=version,
        website_url=state.hub.public_base_url,
        log_level="WARNING",
    )
    tool = server.tool

    # ----------------------------------------------------------------- LLM

    @tool(
        description=(
            "Chat with the OmniFree LLM pool (free-tier providers with automatic failover). Give either "
            "`prompt` (a single user message) or `messages` (full conversation). Returns the answer text, "
            "the model and `routed_via` (which provider answered)."
        ),
        annotations=READ_ONLY_OPEN,
    )
    @_guard
    async def llm_chat(
        prompt: Annotated[str | None, Field(description="Single user message (shortcut for messages).")] = None,
        messages: Annotated[
            list[dict[str, Any]] | None,
            Field(description='Conversation, e.g. [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}].'),
        ] = None,
        system: Annotated[str | None, Field(description="Optional system prompt prepended to the conversation.")] = None,
        model: Annotated[str | None, Field(description="Model id (see llm_models); default: provider default.")] = None,
        provider: Annotated[str | None, Field(description="Pin a provider name; the pool still fails over.")] = None,
        temperature: Annotated[float | None, Field(ge=0, le=2)] = None,
        max_tokens: Annotated[int | None, Field(ge=1, le=32_000)] = None,
    ) -> dict[str, Any]:
        msgs = [ChatMessage.model_validate(m) for m in messages or []]
        if prompt:
            msgs.append(ChatMessage(role="user", content=prompt))
        if not msgs:
            raise ToolError("give `prompt` or `messages`")
        if system:
            msgs.insert(0, ChatMessage(role="system", content=system))
        if provider:
            spec_of_kind(state, provider, ProviderKind.LLM)
        result = await state.router.chat(
            ChatRequest(
                messages=msgs, model=model or None, provider=provider or None,
                temperature=temperature, max_tokens=max_tokens,
            )
        )
        return {
            "content": result.content,
            "model": result.model,
            "routed_via": result.routed_via,
            "finish_reason": result.finish_reason,
            "usage": result.usage.model_dump(mode="json"),
        }

    @tool(description="List the models available in the LLM pool, per provider, and the failover chain order.",
          annotations=READ_ONLY)
    @_guard
    async def llm_models() -> dict[str, Any]:
        return await llm.models(state=state)

    # ----------------------------------------------------------------- Drive

    @tool(
        description="List Drive files (all storage backends merged) under an optional key prefix, e.g. 'notes/'.",
        annotations=READ_ONLY,
    )
    @_guard
    async def drive_list(
        prefix: Annotated[str, Field(description="Key prefix ('' = everything).")] = "",
        provider: Annotated[str | None, Field(description="Only this storage backend.")] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> dict[str, Any]:
        return await drive.list_files(prefix=prefix, provider=provider, limit=limit, state=state)

    @tool(
        description="Search Drive files by case-insensitive substring of the key (path) or metadata values.",
        annotations=READ_ONLY,
    )
    @_guard
    async def drive_search(
        query: Annotated[str, Field(min_length=1, description="Text to find in file paths/metadata.")],
        prefix: Annotated[str, Field(description="Restrict to keys under this prefix.")] = "",
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        return await drive.search(q=query, prefix=prefix, limit=limit, state=state)

    @tool(
        description=(
            "Read a text-like Drive file (txt, md, csv, json, yaml, code ...) and return its content, truncated to "
            "`max_bytes`. Binary files are refused (use drive_share_link). `provider` and `key` come from drive_list."
        ),
        annotations=READ_ONLY,
    )
    @_guard
    async def drive_read_text(
        provider: Annotated[str, Field(min_length=1, description="Storage backend holding the file.")],
        key: Annotated[str, Field(min_length=1, description="Object key (path).")],
        max_bytes: Annotated[int, Field(ge=1, le=TEXT_READ_MAX)] = TEXT_READ_DEFAULT,
    ) -> dict[str, Any]:
        spec_of_kind(state, provider, ProviderKind.STORAGE)
        target = drive.normalise_key(key)
        obj = await state.router.storage_stat(target, provider)
        if obj is None:
            raise ToolError(f"'{target}' not found on {provider}")
        content_type = obj.content_type or mimetypes.guess_type(target)[0]
        if not _is_text(target, content_type):
            raise ToolError(f"'{target}' is not a text file ({content_type or 'unknown type'}); use drive_share_link")
        if obj.size and obj.size > TEXT_FETCH_MAX:
            raise ToolError(f"'{target}' is too large to read here ({obj.size} bytes); use drive_share_link")
        data = await state.router.storage_get(target, provider)
        text = _decode_text(data[: max_bytes + 4] if len(data) > max_bytes + 4 else data)
        if text is None:
            raise ToolError(f"'{target}' is not valid UTF-8 text; use drive_share_link")
        content, truncated = _truncate_utf8(text, max_bytes)
        return {
            "provider": provider,
            "key": target,
            "content_type": content_type,
            "size": len(data),
            "truncated": truncated or len(data) > max_bytes,
            "content": content,
        }

    @tool(
        description=(
            "Write a text file to the Drive (the backend with the most free space, unless `provider` is given). "
            "Overwrites an existing file with the same key on that backend."
        ),
        annotations=CREATES,
    )
    @_guard
    async def drive_upload_text(
        key: Annotated[str, Field(min_length=1, description="Destination key, e.g. 'notes/agent/summary.md'.")],
        content: Annotated[str, Field(description="UTF-8 text content.")],
        content_type: Annotated[str | None, Field(description="MIME type (default: guessed from the key).")] = None,
        provider: Annotated[str | None, Field(description="Pin a storage backend.")] = None,
    ) -> dict[str, Any]:
        if provider:
            spec_of_kind(state, provider, ProviderKind.STORAGE)
        target = drive.normalise_key(key)
        data = content.encode("utf-8")
        if len(data) > state.hub.max_upload_bytes:
            raise ToolError(f"content exceeds {state.hub.max_upload_bytes} bytes")
        ct = content_type or mimetypes.guess_type(target)[0] or "text/plain"
        if ct.startswith("text/") and "charset" not in ct:
            ct += "; charset=utf-8"
        obj = await state.router.storage_put(
            target, data, ct, {"uploaded-by": "mcp"}, provider=provider, allow_fallback=provider is None
        )
        return obj.model_dump(mode="json")

    @tool(
        description=(
            "Create a temporary presigned download URL for a Drive file (anyone with the URL can download it "
            "until it expires). For a password / download-limited public page use drops_create."
        ),
        annotations=READ_ONLY,
    )
    @_guard
    async def drive_share_link(
        provider: Annotated[str, Field(min_length=1, description="Storage backend holding the file.")],
        key: Annotated[str, Field(min_length=1, description="Object key (path).")],
        expires_s: Annotated[int, Field(ge=60, le=7 * 24 * 3600, description="Lifetime in seconds.")] = 3600,
    ) -> dict[str, Any]:
        return await drive.presign(provider=provider, key=key, expires_s=expires_s, method="GET", state=state)

    # ----------------------------------------------------------------- Ask my Drive

    @tool(
        description=(
            "Ask a question about the user's indexed Drive documents (RAG). Returns an answer with [n] citations "
            "and the cited sources (file, page, snippet). Only files indexed with rag_index are searched."
        ),
        annotations=READ_ONLY_OPEN,
    )
    @_guard
    async def ask_drive(
        question: Annotated[str, Field(min_length=1, max_length=4000)],
        top_k: Annotated[int, Field(ge=1, le=20, description="Number of passages to ground the answer on.")] = 8,
    ) -> dict[str, Any]:
        return await rag.ask(rag.AskBody(question=question, top_k=top_k), state=state)

    @tool(
        description=(
            "Index (or re-index) one Drive file for ask_drive: pdf, docx, txt, md, html, csv ... "
            "Returns the indexed document (chunk count, or `error` when the file cannot be indexed)."
        ),
        annotations=UPDATES,
    )
    @_guard
    async def rag_index(
        provider: Annotated[str, Field(min_length=1, description="Storage backend holding the file.")],
        key: Annotated[str, Field(min_length=1, description="Object key (path).")],
    ) -> dict[str, Any]:
        return await rag.index(rag.IndexBody(provider=provider, key=key), state=state)

    # ----------------------------------------------------------------- notes

    async def notes_db() -> AppDB:
        return await notes._db(appdb())

    @tool(
        description=(
            "Search notes by text (title, body, tags) and/or notebook; newest and pinned first. Returns previews; "
            "use notes_get for the full body. Also lists the notebooks with their note counts."
        ),
        annotations=READ_ONLY,
    )
    @_guard
    async def notes_search(
        q: Annotated[str | None, Field(description="Text to search for.")] = None,
        notebook: Annotated[str | None, Field(description="Only this notebook (e.g. 'Inbox').")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        result = await notes.list_notes(notebook=notebook, q=q, limit=limit, db=await notes_db())
        result["notes"] = [_note_preview(n) for n in result["notes"]]
        return result

    @tool(description="Get one note with its full markdown body.", annotations=READ_ONLY)
    @_guard
    async def notes_get(id: Annotated[str, Field(min_length=1, description="Note id.")]) -> dict[str, Any]:
        return await notes.get_note(id, db=await notes_db())

    @tool(
        description="Create a markdown note. The title defaults to the first line of the body; notebook to 'Inbox'.",
        annotations=CREATES,
    )
    @_guard
    async def notes_create(
        body: Annotated[str, Field(min_length=1, max_length=1_000_000, description="Markdown content.")],
        title: Annotated[str | None, Field(max_length=300)] = None,
        notebook: Annotated[str | None, Field(max_length=100)] = None,
        tags: Annotated[list[str] | None, Field(description="Tags (lowercased, '#' stripped).")] = None,
    ) -> dict[str, Any]:
        note = notes.NoteCreate(body=body, title=title, notebook=notebook, tags=tags or [])
        return await notes.create_note(note, db=await notes_db())

    # ----------------------------------------------------------------- tasks

    @tool(
        description="List tasks: open (default; overdue/soonest due first), done (latest first) or all.",
        annotations=READ_ONLY,
    )
    @_guard
    async def tasks_list(
        status: Literal["open", "done", "all"] = "open",
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        return await notes.list_tasks(status_=status, due_before=None, limit=limit, db=await notes_db())

    @tool(
        description=(
            "Add a task. `due` accepts YYYY-MM-DD or a relative day ('today', 'tomorrow', 'friday', 'in 3 days', "
            "'demain' ...) resolved in Africa/Casablanca."
        ),
        annotations=CREATES,
    )
    @_guard
    async def tasks_add(
        title: Annotated[str, Field(min_length=1, max_length=300)],
        due: Annotated[str | None, Field(description="Due date (YYYY-MM-DD or relative).")] = None,
        priority: Literal["low", "normal", "high"] = "normal",
        details: Annotated[str | None, Field(max_length=20_000)] = None,
    ) -> dict[str, Any]:
        body = notes.TaskCreate(title=title, due=_resolve_due(due), priority=priority, details=details)
        return await notes.create_task(body, db=await notes_db())

    @tool(description="Mark a task as done (by id, from tasks_list).", annotations=UPDATES)
    @_guard
    async def tasks_complete(id: Annotated[str, Field(min_length=1, description="Task id.")]) -> dict[str, Any]:
        return await notes.patch_task(id, notes.TaskPatch(done=True), db=await notes_db())

    # ----------------------------------------------------------------- saved AI chats

    @tool(
        description=(
            "List the saved AI conversations of the PWA chat (title, message count, last update), pinned and "
            "most recent first; `q` searches titles and message text."
        ),
        annotations=READ_ONLY,
    )
    @_guard
    async def chats_list(
        q: Annotated[str | None, Field(description="Text to search for.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        return await chats.list_chats(q=q, limit=limit, db=await chats._db(appdb()))

    # ----------------------------------------------------------------- expenses

    async def expenses_db() -> AppDB:
        return await expenses._db(appdb())

    @tool(
        description="Expenses of one month (YYYY-MM, default: current month) with totals per currency and category.",
        annotations=READ_ONLY,
    )
    @_guard
    async def expenses_month(
        month: Annotated[str | None, Field(description="Month as YYYY-MM.")] = None,
    ) -> dict[str, Any]:
        if month and not _MONTH_RE.match(month):
            raise ToolError("month must be YYYY-MM")
        return await expenses.list_expenses(month=month or None, db=await expenses_db())

    @tool(
        description=(
            "Record an expense. Currency defaults to MAD, date to today (Africa/Casablanca). Categories: "
            + ", ".join(expenses.CATEGORIES) + "."
        ),
        annotations=CREATES,
    )
    @_guard
    async def expense_add(
        total: Annotated[float, Field(ge=0, description="Amount paid.")],
        merchant: Annotated[str, Field(max_length=200)] = "",
        currency: Annotated[str, Field(description="ISO 4217 code (MAD, EUR, USD ...).")] = "MAD",
        category: expenses.Category = "other",
        date: Annotated[str | None, Field(description="YYYY-MM-DD (default today).")] = None,
        note: Annotated[str | None, Field(max_length=5000)] = None,
        items: Annotated[
            list[dict[str, Any]] | None,
            Field(description='Line items: [{"name": "...", "qty": 1, "price": 10.5}].'),
        ] = None,
    ) -> dict[str, Any]:
        body = expenses.ExpenseCreate.model_validate(
            {
                "total": total, "merchant": merchant, "currency": currency, "category": category,
                "date": date, "note": note, "items": items or [],
            }
        )
        return await expenses.create_expense(body, db=await expenses_db())

    # ----------------------------------------------------------------- links and drops

    async def links_db() -> AppDB:
        return await links._db(appdb())

    @tool(
        description="Create a short link <public_base_url>/s/<code> redirecting to `url` (random code unless given).",
        annotations=CREATES,
    )
    @_guard
    async def links_create(
        url: Annotated[str, Field(min_length=1, description="Absolute http(s) URL.")],
        code: Annotated[str | None, Field(description="Custom code (A-Z a-z 0-9 _ -).")] = None,
        title: Annotated[str | None, Field(max_length=300)] = None,
        expires_in_days: Annotated[int | None, Field(ge=1, le=3650)] = None,
    ) -> dict[str, Any]:
        body = links.LinkCreate(url=url, code=code, title=title, expires_in_days=expires_in_days)
        return await links.create_link(body, state=state, db=await links_db())

    @tool(
        description=(
            "Create a public file drop page <public_base_url>/d/<code> for a Drive file, with expiry, optional "
            "password and optional download limit. The backend must support presigned URLs (e.g. R2)."
        ),
        annotations=CREATES,
    )
    @_guard
    async def drops_create(
        provider: Annotated[str, Field(min_length=1, description="Storage backend holding the file.")],
        key: Annotated[str, Field(min_length=1, description="Object key (path).")],
        expires_in_hours: Annotated[int, Field(ge=1, le=24 * 365)] = 24,
        password: Annotated[str | None, Field(max_length=200)] = None,
        max_downloads: Annotated[int | None, Field(ge=1, le=1_000_000)] = None,
    ) -> dict[str, Any]:
        body = links.DropCreate(
            provider=provider, key=key, expires_in_hours=expires_in_hours, password=password,
            max_downloads=max_downloads,
        )
        return await links.create_drop(body, state=state, db=await links_db())

    # ----------------------------------------------------------------- schedules

    @tool(
        description="List scheduled HTTP jobs (cron) across the cron providers (QStash, GitHub Actions ...).",
        annotations=READ_ONLY,
    )
    @_guard
    async def schedules_list(
        provider: Annotated[str | None, Field(description="Only this cron provider.")] = None,
    ) -> dict[str, Any]:
        return await cron.jobs(provider=provider, state=state)

    @tool(
        description=(
            "Schedule an HTTP request on a cron expression (5 fields, UTC: minute hour day-of-month month "
            "day-of-week). `provider` defaults to the first enabled cron provider."
        ),
        annotations=CREATES,
    )
    @_guard
    async def schedule_create(
        name: Annotated[str, Field(min_length=1, max_length=200)],
        schedule: Annotated[str, Field(description="Cron expression, e.g. '0 7 * * 1' (Mondays 07:00 UTC).")],
        target: Annotated[str, Field(min_length=1, description="URL to call.")],
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "POST",
        headers: Annotated[dict[str, str] | None, Field(description="Extra request headers.")] = None,
        body: Annotated[str | None, Field(description="Request body.")] = None,
        provider: Annotated[str | None, Field(description="Cron provider name.")] = None,
    ) -> dict[str, Any]:
        job = cron.CronJobBody(
            provider=default_provider(state, ProviderKind.CRON, provider), name=name, schedule=schedule,
            target=target, method=method, headers=headers or {}, body=body,
        )
        return await cron.create_job(job, state=state)

    # ----------------------------------------------------------------- budget

    @tool(
        description=(
            "Free-tier budget: usage vs limit for every enabled provider (LLM tokens/requests, storage bytes, DB, "
            "cron ...), with ok / warn / over status."
        ),
        annotations=READ_ONLY,
    )
    @_guard
    async def budget_status() -> dict[str, Any]:
        return await build_budget(state.router)

    # ----------------------------------------------------------------- resources

    @server.resource(
        "omnifree://budget", name="budget", title="Free-tier budget",
        description="Usage vs limits of every enabled provider (same data as budget_status).",
        mime_type="application/json",
    )
    async def budget_resource() -> str:
        return json.dumps(await build_budget(state.router), ensure_ascii=False)

    @server.resource(
        "omnifree://tasks/open", name="open_tasks", title="Open tasks",
        description="Open tasks, overdue and soonest due first.", mime_type="application/json",
    )
    async def open_tasks_resource() -> str:
        result = await notes.list_tasks(status_="open", due_before=None, limit=500, db=await notes_db())
        return json.dumps(result, ensure_ascii=False)

    @server.resource(
        "omnifree://notes/{id}", name="note", title="Note",
        description="One note as markdown (title heading + body).", mime_type="text/markdown",
    )
    async def note_resource(id: str) -> str:
        try:
            note = await notes.get_note(id, db=await notes_db())
        except HTTPException as exc:
            raise ResourceError(f"note '{id}': {_detail(exc.detail)}") from exc
        return f"# {note['title']}\n\n{note['body']}"

    # ----------------------------------------------------------------- prompts

    @server.prompt(
        name="weekly_review", title="Weekly review",
        description="Review the week: open tasks, this month's expenses and the free-tier budget, then plan.",
    )
    def weekly_review(focus: str = "") -> str:
        extra = f"\nPay special attention to: {focus}." if focus else ""
        return (
            "Run my weekly review with the OmniFree tools.\n"
            "1. tasks_list (status 'open') and tasks_list (status 'done', limit 30): what got done, what is "
            "overdue, what is due in the next 7 days.\n"
            "2. expenses_month: total per currency and the top categories; flag anything unusual.\n"
            "3. budget_status: any provider at 'warn' or 'over'.\n"
            "4. notes_search (notebook 'Inbox'): notes that still need processing.\n"
            "Then give me a short summary and a prioritised plan for next week (at most 7 items). Ask before "
            f"creating tasks or notes; when I agree, use tasks_add / notes_create.{extra}"
        )

    @server.prompt(
        name="inbox_triage", title="Inbox triage",
        description="Process the notes in a notebook (default 'Inbox'): extract tasks, propose filing.",
    )
    def inbox_triage(notebook: str = "Inbox") -> str:
        return (
            f"Triage my '{notebook}' notebook with the OmniFree tools.\n"
            f"1. notes_search with notebook '{notebook}' (limit 50); open each note with notes_get.\n"
            "2. For each note: a one-line summary, the concrete actions it contains (with due dates if any), and "
            "the notebook it belongs in.\n"
            "3. Compare with tasks_list (open) so you do not propose duplicates.\n"
            "Present the result as a table, then ask me which tasks to create with tasks_add. Never delete anything."
        )

    return server


# --------------------------------------------------------------------------- HTTP endpoint


def _jsonrpc_error(status_code: int, code: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class MCPEndpoint:
    """ASGI endpoint for ``/mcp``: bearer auth, Origin check, then one stateless SDK exchange per request."""

    def __init__(self, app: FastAPI, server: MCPServer) -> None:
        self.app = app
        self.server = server
        self._router_started = False

    def _tokens(self, state: HubState) -> list[str]:
        return [t for t in (state.hub_token(), state.settings.secret(MCP_TOKEN_KEY)) if t]

    def _security(self, state: HubState) -> TransportSecuritySettings | None:
        hosts = state.hub.mcp_allowed_hosts
        if not hosts:
            return None  # Host checks off (bearer auth + Origin check below); set OMNIFREE_MCP_ALLOWED_HOSTS to enable
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(hosts),
            allowed_origins=list(state.settings.cors_origins),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - routes only see http here
            return
        state: HubState = self.app.state.hub
        headers = Headers(scope=scope)

        tokens = self._tokens(state)
        if not tokens:
            await _jsonrpc_error(
                503, -32000, "MCP token not configured (set HUB_API_TOKEN or MCP_API_TOKEN in the vault)"
            )(scope, receive, send)
            return
        presented = _bearer(headers.get("authorization"))
        if not any(token_matches(t, presented) for t in tokens):
            error = "invalid_token" if presented else "invalid_request"
            await _jsonrpc_error(
                401, -32001, "invalid or missing bearer token",
                {"WWW-Authenticate": f'Bearer realm="omnifree", error="{error}"'},
            )(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin and origin not in state.settings.cors_origins and "*" not in state.settings.cors_origins:
            await _jsonrpc_error(403, -32000, f"origin {origin!r} is not allowed")(scope, receive, send)
            return

        if scope["method"] != "POST":
            # Stateless JSON mode: no standalone SSE stream (GET) and no session to delete (DELETE).
            await _jsonrpc_error(405, -32000, "method not allowed: POST JSON-RPC messages", {"Allow": "POST"})(
                scope, receive, send
            )
            return

        if not self._router_started:  # no lifespan on serverless: open the router (idempotent) on first use
            await state.router.start()
            self._router_started = True

        manager = StreamableHTTPSessionManager(
            app=self.server._lowlevel_server,  # the public `session_manager` only exists after streamable_http_app()
            json_response=True,
            stateless=True,
            security_settings=self._security(state),
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)


def mount_mcp(app: FastAPI, *, version: str = "") -> MCPEndpoint:
    """Add ``/mcp`` (and ``/mcp/``) to the hub. Authentication is done by the endpoint itself."""
    for name in ("mcp.server.streamable_http_manager", "mcp.server.streamable_http"):
        logging.getLogger(name).setLevel(logging.WARNING)  # per-request start/stop lines are noise here
    endpoint = MCPEndpoint(app, build_mcp_server(app, version=version))
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint, include_in_schema=False))
    app.router.routes.append(Route(MCP_PATH + "/", endpoint=endpoint, include_in_schema=False))
    app.state.mcp = endpoint
    return endpoint
