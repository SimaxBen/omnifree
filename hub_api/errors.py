"""Map the OmniFree provider exception hierarchy to HTTP responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.interfaces import (
    CapabilityNotSupported,
    NoProviderAvailable,
    NotConfigured,
    PluginLoadError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
)

__all__ = ["install_error_handlers", "status_for"]

log = logging.getLogger("omnifree.hub")


def status_for(exc: ProviderError) -> tuple[int, str]:
    """(HTTP status, machine-readable error code) for a provider exception."""
    if isinstance(exc, NoProviderAvailable):
        return 503, "no_provider_available"
    if isinstance(exc, QuotaExhausted):
        return 429, "quota_exhausted"
    if isinstance(exc, ProviderTimeout):
        return 504, "provider_timeout"
    if isinstance(exc, ProviderUnavailable):
        return 503, "provider_unavailable"
    if isinstance(exc, ProviderAuthError):
        return 502, "provider_auth_error"
    if isinstance(exc, PluginLoadError):
        return 500, "plugin_load_error"
    if isinstance(exc, NotConfigured):
        if exc.message.startswith("unknown plugin"):
            return 404, "unknown_plugin"
        return 409, "not_configured"
    if isinstance(exc, CapabilityNotSupported):
        return 501, "capability_not_supported"
    if exc.status_code == 404:
        return 404, "not_found"
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        return 400, "provider_rejected"
    return 502, "provider_error"


def error_body(exc: ProviderError) -> dict[str, object]:
    code, kind = status_for(exc)
    body: dict[str, object] = {
        "error": kind,
        "detail": exc.message or str(exc),
        "provider": exc.provider,
        "status": code,
    }
    if exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
    if isinstance(exc, NoProviderAvailable):
        body["attempts"] = exc.attempts
        body["kind"] = exc.kind
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProviderError)
    async def _provider_error(_request: Request, exc: ProviderError) -> JSONResponse:
        code, _kind = status_for(exc)
        if code >= 500:
            log.warning("provider error: %s", exc)
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(error_body(exc), status_code=code, headers=headers)
