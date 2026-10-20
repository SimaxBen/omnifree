"""Shared plumbing for the llm_pool adapters.

* :class:`RateLimitAwareMixin` captures ``x-ratelimit-*`` headers from every response (including
  streamed and error responses, through an httpx response event hook), exposes them through
  ``quota()`` as a provider-sourced :class:`QuotaInfo`, refuses requests locally while a
  request bucket is known to be at zero (``QuotaExhausted`` with ``retry_after``), and remaps
  provider-specific "quota" errors (HTTP 400/403 bodies, in-stream error objects) to
  :class:`QuotaExhausted`.
* :class:`PooledOpenAIAdapter` is ``core.openai_compat.OpenAICompatibleAdapter`` plus that mixin;
  every llm_pool adapter except Cohere derives from it.

Settings understood here (all optional):

=========================  ===================================================================
rate_limit_header_windows  ``{metric: window}`` for headers without a window suffix,
                           e.g. ``{requests: day, tokens: minute}`` (Groq's documented meaning)
respect_rate_headers       default true: fail fast with QuotaExhausted while a request bucket
                           reported ``remaining: 0`` and its reset time is in the future
=========================  ===================================================================
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, ClassVar, TypeVar

import httpx

from core.interfaces import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelInfo,
    ProviderAuthError,
    ProviderError,
    QuotaExhausted,
    QuotaInfo,
)
from core.openai_compat import OpenAICompatibleAdapter
from plugins.llm_pool._ratelimit import (
    RateLimitSnapshot,
    parse_rate_limit_headers,
    parse_retry_hint,
)

__all__ = ["RateLimitAwareMixin", "PooledOpenAIAdapter", "QUOTA_BODY_RE"]

T = TypeVar("T")

QUOTA_BODY_RE = re.compile(
    r"quota|rate[ _-]?limit|resource[_ ]exhausted|insufficient[_ ]credits|too many requests"
    r"|out of credits|credits? (?:exhausted|depleted)|daily limit|limit exceeded",
    re.IGNORECASE,
)


class RateLimitAwareMixin:
    """Mixin for :class:`core.interfaces.ProviderAdapter` subclasses (place it first in the bases)."""

    #: Window meaning of headers that carry no window suffix (overridable by settings).
    default_rate_limit_windows: ClassVar[dict[str, str]] = {}

    name: str
    context: Any

    def __init__(self, context: Any) -> None:
        super().__init__(context)  # type: ignore[call-arg]
        self._rate: RateLimitSnapshot = RateLimitSnapshot()

    # ------------------------------------------------------------------ http
    def build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.context.timeout_s,
            transport=self.context.http_transport,
            follow_redirects=True,
            event_hooks={"response": [self._observe_response]},
        )

    async def _observe_response(self, response: httpx.Response) -> None:
        snapshot = parse_rate_limit_headers(response.headers, self.rate_limit_windows())
        if snapshot:
            self._rate = snapshot

    def rate_limit_windows(self) -> dict[str, str]:
        configured = self.setting("rate_limit_header_windows") or {}  # type: ignore[attr-defined]
        return {**self.default_rate_limit_windows, **{str(k): str(v) for k, v in configured.items()}}

    @property
    def rate_limits(self) -> RateLimitSnapshot:
        return self._rate

    # ------------------------------------------------------------------ quota
    def _reset_hint(self) -> float | None:
        now = datetime.now(timezone.utc)
        candidates = [
            b.reset_at
            for b in self._rate.buckets.values()
            if b.reset_at is not None and b.reset_at > now and b.remaining is not None and b.remaining <= 0
        ]
        if not candidates:
            return None
        return (min(candidates) - now).total_seconds()

    def preflight(self) -> None:
        """Fail fast while the provider told us a request budget is empty."""
        if not self.setting("respect_rate_headers", True):  # type: ignore[attr-defined]
            return
        bucket = self._rate.exhausted()
        if bucket is None or bucket.reset_at is None:
            return
        wait = max((bucket.reset_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
        raise QuotaExhausted(
            f"{bucket.metric} budget ({bucket.window or 'window'}) exhausted, resets {bucket.reset_at.isoformat()}",
            provider=self.name,
            status_code=429,
            retry_after=wait,
        )

    def remap_error(self, exc: ProviderError) -> ProviderError:
        """Normalise provider errors: quota-looking bodies become QuotaExhausted with a retry hint."""
        if isinstance(exc, QuotaExhausted):
            if exc.retry_after is None:
                exc.retry_after = parse_retry_hint(exc.message) or self._reset_hint()
            return exc
        generic = type(exc) is ProviderError and exc.status_code in (None, 200, 400)
        forbidden = isinstance(exc, ProviderAuthError) and exc.status_code == 403
        if (generic or forbidden) and QUOTA_BODY_RE.search(exc.message or ""):
            return QuotaExhausted(
                exc.message,
                provider=exc.provider or self.name,
                status_code=exc.status_code or 429,
                retry_after=parse_retry_hint(exc.message) or self._reset_hint(),
            )
        return exc

    async def guarded(self, fn: Callable[[], Awaitable[T]]) -> T:
        self.preflight()
        try:
            return await fn()
        except ProviderError as exc:
            mapped = self.remap_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    async def guarded_stream(self, gen: AsyncIterator[ChatChunk]) -> AsyncIterator[ChatChunk]:
        try:
            async for chunk in gen:
                yield chunk
        except ProviderError as exc:
            mapped = self.remap_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    async def quota(self) -> QuotaInfo:
        bucket = self._rate.primary()
        if bucket is None or bucket.limit is None:
            return await super().quota()  # type: ignore[misc]
        return QuotaInfo(
            provider=self.name,
            used=bucket.used or 0.0,
            limit=bucket.limit,
            unit=bucket.metric,
            window=bucket.window,
            reset_at=bucket.reset_at,
            source="provider",
            extra={
                "observed_at": self._rate.observed_at.isoformat(),
                "buckets": [b.as_dict() for b in self._rate.buckets.values()],
                "source_detail": "rate-limit response headers",
            },
        )


class PooledOpenAIAdapter(RateLimitAwareMixin, OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with rate-limit tracking and quota-error normalisation."""

    async def _post_json(
        self, path_setting: str, default: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], httpx.Response]:
        data, response = await super()._post_json(path_setting, default, body)
        if isinstance(data, list):  # e.g. Gemini's OpenAI endpoint can wrap errors in a list
            data = data[0] if data and isinstance(data[0], dict) else {"error": data}
        if not isinstance(data, dict):
            raise ProviderError(f"unexpected response type {type(data).__name__}", provider=self.name)
        if "error" in data and not data.get("choices") and not data.get("data"):
            raise ProviderError(str(data["error"])[:500], provider=self.name, status_code=response.status_code)
        return data, response

    async def chat(self, request: ChatRequest) -> ChatResponse:
        return await self.guarded(lambda: OpenAICompatibleAdapter.chat(self, request))

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.preflight()
        async for chunk in self.guarded_stream(OpenAICompatibleAdapter.stream(self, request)):
            yield chunk

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        return await self.guarded(lambda: OpenAICompatibleAdapter.embed(self, request))

    def configured_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=str(m), provider=self.name) for m in (self.setting("models") or [])]

    async def list_models(self) -> list[ModelInfo]:
        """Live model list; a 404 on the models endpoint falls back to ``settings.models``."""
        try:
            return await OpenAICompatibleAdapter.list_models(self)
        except ProviderError as exc:
            if type(exc) is ProviderError and exc.status_code == 404:
                return self.configured_models()
            raise
