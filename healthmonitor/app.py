"""Tiny HTTP surface for the monitor (Koyeb needs a listening port; GCE uses it for status).

* ``GET /healthz``: liveness of the worker itself
* ``GET /status``: per-target state, job results, current outages. The full report (error
  strings, latencies, job results) needs ``Authorization: Bearer <HEALTHMONITOR_STATUS_TOKEN>``
  (falls back to ``HUB_API_TOKEN``); without a valid token only up/down per target is shown,
  because this port is public on Koyeb.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request

from healthmonitor.monitor import HealthMonitor, load_config

__all__ = ["create_app", "STATUS_TOKEN_KEYS"]

STATUS_TOKEN_KEYS = ("HEALTHMONITOR_STATUS_TOKEN", "HUB_API_TOKEN")


def _authorised(request: Request, monitor: HealthMonitor) -> bool:
    expected = next((v for k in STATUS_TOKEN_KEYS if (v := monitor.secret_lookup(k))), None)
    scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not presented.strip():
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.strip().encode("utf-8"))


def _default_monitor() -> HealthMonitor:
    from core.config import get_settings

    settings = get_settings()
    return HealthMonitor(load_config(), secret_lookup=settings.secret)


def create_app(monitor: HealthMonitor | None = None, *, run_loop: bool = True) -> FastAPI:
    holder: dict[str, HealthMonitor] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mon = monitor or _default_monitor()
        holder["monitor"] = mon
        task = asyncio.create_task(mon.run_forever()) if run_loop else None
        try:
            yield
        finally:
            mon.stop()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await mon.close()

    app = FastAPI(title="OmniFree health monitor", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        mon = holder.get("monitor")
        return {"ok": mon is not None, "sweeps": mon.sweeps if mon else 0}

    @app.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        mon = holder["monitor"]
        return mon.snapshot() if _authorised(request, mon) else mon.public_snapshot()

    return app
