"""Shared helpers for the Cloudflare v4 REST API (envelope ``{success, errors, result}``)."""

from __future__ import annotations

from typing import Any

import httpx

from core.interfaces import ProviderAdapter, ProviderError, QuotaExhausted

API_BASE = "https://api.cloudflare.com/client/v4"

# Phrases Cloudflare uses when a free-tier allowance (not the HTTP rate limit) is used up,
# e.g. D1 "Exceeded daily rows read limit".
_QUOTA_HINTS = ("exceeded", "limit reached", "quota")


class CloudflareAPIMixin:
    """Mixed into Cloudflare adapters; relies on ``ProviderAdapter`` helpers."""

    def cf_base(self: ProviderAdapter) -> str:  # type: ignore[misc]
        return str(self.setting("api_base", API_BASE)).rstrip("/")

    def cf_headers(self: ProviderAdapter) -> dict[str, str]:  # type: ignore[misc]
        return {"Authorization": f"Bearer {self.require_secret('api_token')}"}

    def cf_account_url(self: ProviderAdapter, path: str) -> str:  # type: ignore[misc]
        account_id = self.require_secret("account_id")
        return f"{self.cf_base()}/accounts/{account_id}/{path.lstrip('/')}"  # type: ignore[attr-defined]

    async def cf_call(self: ProviderAdapter, method: str, url: str, **kwargs: Any) -> Any:  # type: ignore[misc]
        """Send a request and return the envelope's ``result`` (errors mapped to ProviderError)."""
        headers = {**self.cf_headers(), **kwargs.pop("headers", {})}  # type: ignore[attr-defined]
        try:
            response = await self.request(method, url, headers=headers, **kwargs)
        except ProviderError as exc:
            _maybe_quota(exc, self.name)
            raise
        return _envelope_result(response, self.name)

    async def cf_call_envelope(self: ProviderAdapter, method: str, url: str, **kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
        headers = {**self.cf_headers(), **kwargs.pop("headers", {})}  # type: ignore[attr-defined]
        try:
            response = await self.request(method, url, headers=headers, **kwargs)
        except ProviderError as exc:
            _maybe_quota(exc, self.name)
            raise
        _envelope_result(response, self.name)
        return response.json()


def _maybe_quota(exc: ProviderError, provider: str) -> None:
    if isinstance(exc, QuotaExhausted):
        return
    text = (exc.message or "").lower()
    if "limit" in text and any(h in text for h in _QUOTA_HINTS):
        raise QuotaExhausted(exc.message, provider=provider, status_code=exc.status_code) from exc


def _envelope_result(response: httpx.Response, provider: str) -> Any:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError(f"invalid JSON from Cloudflare: {response.text[:200]}", provider=provider) from exc
    if isinstance(body, dict) and body.get("success") is False:
        errors = body.get("errors") or []
        message = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors if isinstance(e, dict))
        message = message or "Cloudflare API returned success=false"
        err = ProviderError(message, provider=provider, status_code=response.status_code)
        _maybe_quota(err, provider)
        raise err
    return body.get("result") if isinstance(body, dict) else body
