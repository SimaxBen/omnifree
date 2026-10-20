"""Serverless ASGI entry point (Vercel Python functions) for when no container host is available.

Differences from ``hub_api.main`` (Cloud Run / Docker):
* the platform may not run the ASGI lifespan, so a middleware opens the router (quota DB) lazily on the
  first request; ``SmartRouter.start`` is idempotent;
* no background health sweep (functions are frozen between requests); the switchboard shows the health of
  the last real calls instead;
* writable state lives in ``/tmp`` and is per instance (quota counters reset on cold starts);
* WebSockets are not supported by the platform: the PWA falls back to ``/events/recent`` polling.
"""

from __future__ import annotations

import os

os.environ.setdefault("OMNIFREE_BACKGROUND_HEALTH", "false")
os.environ.setdefault("OMNIFREE_DATA_DIR", "/tmp/omnifree-data")
os.environ.setdefault("OMNIFREE_QUOTA_DB", "/tmp/omnifree-data/quota.db")
os.environ.setdefault("OMNIFREE_STATE_BACKEND", "upstash")

from collections.abc import Awaitable, Callable  # noqa: E402

from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from core.config import get_settings  # noqa: E402
from hub_api.app import create_app  # noqa: E402

app = create_app()
_started = False


@app.middleware("http")
async def _ensure_started(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    global _started
    if not _started:
        get_settings().data_path.mkdir(parents=True, exist_ok=True)
        await app.state.hub.router.start()
        _started = True
    return await call_next(request)
