"""FastAPI application factory and lifespan wiring (registry + router + event bus)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from core.config import Settings, get_settings
from core.events import EventBus
from core.registry import PluginRegistry
from core.router import SmartRouter
from hub_api.auth import require_token
from hub_api.errors import install_error_handlers
from hub_api.mcp_server import mount_mcp
from hub_api.routers import agent, compute, cron, db, dns, drive, expenses, links, llm, money, notes, plugins, ws
from hub_api.routers import budget as budget_router
from hub_api.routers import calendar as calendar_router
from hub_api.routers import chats as chats_router
from hub_api.routers import digest as digest_router
from hub_api.routers import inbox as inbox_router
from hub_api.routers import notify as notify_router
from hub_api.routers import rag as rag_router
from hub_api.routers.llm import ROUTED_VIA_HEADER
from hub_api.settings import HubSettings
from hub_api.state import HubState

__all__ = ["create_app"]

log = logging.getLogger("omnifree.hub")

VERSION = "0.1.0"


async def _health_loop(state: HubState, interval_s: float) -> None:
    """Background sweep keeping ``last_health``/latency fresh for the switchboard.

    Every ``_MODEL_REFRESH_EVERY`` sweeps (starting with the first) it also refreshes the live
    model lists, so model ids that are missing from providers.yaml (OpenRouter ':free',
    new Gemini models) become routable; the YAML lists are only fallbacks.
    """
    sweep = 0
    while True:
        try:
            await state.router.health_all()
            if sweep % _MODEL_REFRESH_EVERY == 0:
                await state.router.list_models()
        except Exception as exc:  # never let the sweep die
            log.warning("health sweep failed: %r", exc)
        sweep += 1
        await asyncio.sleep(interval_s)


_MODEL_REFRESH_EVERY = 12


def create_app(
    settings: Settings | None = None,
    hub_settings: HubSettings | None = None,
    *,
    bus: EventBus | None = None,
    registry: PluginRegistry | None = None,
    router: SmartRouter | None = None,
    instinct_transport: httpx.AsyncBaseTransport | None = None,
    state_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the hub. Everything is injectable for tests; production uses the defaults.

    The registry/router are created eagerly (no I/O: the registry only parses
    YAML) and started/closed by the lifespan.
    """
    settings = settings or get_settings()
    hub_settings = hub_settings or HubSettings()
    logging.basicConfig(level=settings.log_level.upper())
    state = HubState.build(
        settings,
        hub_settings,
        bus=bus,
        registry=registry,
        router=router,
        instinct_transport=instinct_transport,
        state_transport=state_transport,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_path.mkdir(parents=True, exist_ok=True)
        await state.router.start()
        health_task: asyncio.Task[None] | None = None
        if hub_settings.background_health and settings.health_interval_s > 0:
            health_task = asyncio.create_task(_health_loop(state, settings.health_interval_s))
        if not state.hub_token():
            log.error("no hub token configured: every protected route will answer 503")
        try:
            yield
        finally:
            if health_task is not None:
                health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_task
            await state.instinct.close()
            await state.router.close()
            await state.bus.flush()
            if state.shared_state is not None:
                await state.shared_state.aclose()

    production = settings.environment == "production"
    app = FastAPI(
        title="OmniFree Hub API",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.hub = state

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # bearer tokens, no cookies
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        # Mcp-Protocol-Version / Mcp-Session-Id: browser-based MCP clients (e.g. the MCP Inspector) on /mcp.
        allow_headers=["Authorization", "Content-Type", "Mcp-Protocol-Version", "Mcp-Session-Id"],
        expose_headers=[
            ROUTED_VIA_HEADER, "X-Storage-Backend", "Retry-After", "Content-Disposition", "WWW-Authenticate",
        ],
        max_age=600,
    )
    install_error_handlers(app)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, Any]:
        """Unauthenticated liveness probe (Cloud Run / uptime monitors)."""
        return {"ok": True, "version": VERSION, "plugins": len(state.registry.specs)}

    protected = [Depends(require_token)]
    for module in (llm, agent, drive, dns, db, plugins, compute, cron, rag_router, budget_router):
        app.include_router(module.router, dependencies=protected)
    for module in (notes, expenses, money, links, inbox_router, digest_router, chats_router, notify_router, calendar_router):  # personal apps (data in D1)
        app.include_router(module.router, dependencies=protected)
    app.include_router(links.public_router)  # PUBLIC: /s/{code}, /d/{code} (no token)
    app.include_router(calendar_router.public_router)  # PUBLIC: /calendar/{token}.ics (token in the path)

    @app.get("/events/recent", tags=["meta"], dependencies=protected)
    async def recent_events(limit: int = 100, types: str | None = None) -> list[dict[str, Any]]:
        patterns = [t for t in (types or "").split(",") if t.strip()] or None
        events = await state.bus.fetch_recent(min(max(limit, 1), 500), patterns)
        return [e.model_dump(mode="json") for e in events]

    if state.bus.shared is not None:

        @app.middleware("http")
        async def _flush_shared_events(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            # Serverless instances may be frozen right after the response: push this request's
            # events to the shared list first (bounded wait, errors are swallowed by the bus).
            response = await call_next(request)
            await state.bus.flush()
            return response

    app.include_router(ws.router)  # authenticates itself (query token / subprotocol)
    mount_mcp(app, version=VERSION)  # /mcp: MCP server for agents, authenticates itself (hub or MCP token)
    return app
