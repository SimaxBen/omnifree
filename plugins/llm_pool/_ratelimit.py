"""Rate-limit header parsing shared by the llm_pool adapters.

Upstreams expose their remaining budget in response headers, with several dialects:

* Groq / OpenAI style: ``x-ratelimit-limit-requests``, ``x-ratelimit-remaining-tokens``,
  ``x-ratelimit-reset-requests: 2m59.56s`` (Go duration strings).
* Cerebras style: ``x-ratelimit-limit-requests-day``, ``x-ratelimit-remaining-tokens-minute``,
  ``x-ratelimit-reset-requests-day: 33011.38`` (seconds).
* OpenRouter / generic style: ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``,
  ``X-RateLimit-Reset`` (epoch milliseconds or seconds).

Every bucket is normalised to :class:`RateBucket` keyed ``(metric, window)``; ``window`` is the
explicit header suffix when present, otherwise the adapter's ``rate_limit_header_windows`` setting
(e.g. Groq documents ``requests`` as per-day and ``tokens`` as per-minute), otherwise ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = ["RateBucket", "RateLimitSnapshot", "parse_rate_limit_headers", "parse_reset", "parse_retry_hint"]

_HEADER_RE = re.compile(
    r"^x-ratelimit-(?P<field>limit|remaining|reset)"
    r"(?:-(?P<metric>requests|tokens|calls))?"
    r"(?:-(?P<window>second|minute|hour|day|month))?$"
)
_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|h|m|s)")
_WINDOWS = ("second", "minute", "hour", "day", "month")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_reset(value: str | None, now: datetime | None = None) -> datetime | None:
    """Convert a reset header (duration, seconds, epoch s/ms or HTTP date) to an absolute UTC time."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    now = now or _now()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if number > 1e12:  # epoch milliseconds
            return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc)
        if number > 1e9:  # epoch seconds
            return datetime.fromtimestamp(number, tz=timezone.utc)
        return now + timedelta(seconds=max(number, 0.0))
    parts = list(_DURATION_RE.finditer(text))
    if parts and "".join(p.group(0) for p in parts) == text.replace(" ", ""):
        seconds = 0.0
        for part in parts:
            amount = float(part.group("value"))
            unit = part.group("unit")
            seconds += {"ms": amount / 1000.0, "s": amount, "m": amount * 60.0, "h": amount * 3600.0}[unit]
        return now + timedelta(seconds=seconds)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_RETRY_PATTERNS = (
    re.compile(r"try again in\s+(?P<d>(?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE),
    re.compile(r'"retryDelay"\s*:\s*"(?P<d>\d+(?:\.\d+)?s)"'),
    re.compile(r"retry after\s+(?P<d>\d+(?:\.\d+)?)\s*(?:seconds|s)\b", re.IGNORECASE),
)


def parse_retry_hint(text: str) -> float | None:
    """Extract a retry delay (seconds) from an error body such as Groq's
    "Please try again in 1m20.5s" or Gemini's ``"retryDelay": "31s"``."""
    for pattern in _RETRY_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        when = parse_reset(match.group("d"))
        if when is not None:
            return max((when - _now()).total_seconds(), 0.0)
    return None


@dataclass
class RateBucket:
    metric: str
    window: str | None
    limit: float | None = None
    remaining: float | None = None
    reset_at: datetime | None = None

    @property
    def used(self) -> float | None:
        if self.limit is None or self.remaining is None:
            return None
        return max(self.limit - self.remaining, 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "window": self.window,
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }


@dataclass
class RateLimitSnapshot:
    buckets: dict[tuple[str, str | None], RateBucket] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=_now)

    def __bool__(self) -> bool:
        return bool(self.buckets)

    def primary(self) -> RateBucket | None:
        """The bucket that best represents the provider's free-tier budget.

        Preference: requests per day, month, hour, minute, unknown window; then tokens likewise.
        """
        order = ("day", "month", "hour", "minute", None, "second")
        for metric in ("requests", "tokens"):
            for window in order:
                bucket = self.buckets.get((metric, window))
                if bucket is not None and bucket.limit is not None:
                    return bucket
        return next(iter(self.buckets.values()), None)

    def exhausted(self, now: datetime | None = None) -> RateBucket | None:
        """A request bucket with ``remaining == 0`` whose reset is still in the future."""
        now = now or _now()
        for bucket in self.buckets.values():
            if bucket.metric != "requests" or bucket.remaining is None or bucket.remaining > 0:
                continue
            if bucket.reset_at is not None and bucket.reset_at > now:
                return bucket
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_rate_limit_headers(
    headers: Mapping[str, str],
    windows: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> RateLimitSnapshot:
    """Parse every ``x-ratelimit-*`` header into a :class:`RateLimitSnapshot`."""
    now = now or _now()
    windows = {str(k): str(v) for k, v in (windows or {}).items()}
    snapshot = RateLimitSnapshot(observed_at=now)
    for raw_name, raw_value in headers.items():
        name = raw_name.lower()
        match = _HEADER_RE.match(name)
        if not match:
            continue
        metric = match.group("metric") or "requests"
        if metric == "calls":
            metric = "requests"
        window = match.group("window") or windows.get(metric)
        if window not in _WINDOWS:
            window = None
        key = (metric, window)
        bucket = snapshot.buckets.setdefault(key, RateBucket(metric=metric, window=window))
        field_name = match.group("field")
        if field_name == "limit":
            bucket.limit = _to_float(raw_value)
        elif field_name == "remaining":
            bucket.remaining = _to_float(raw_value)
        else:
            bucket.reset_at = parse_reset(raw_value, now)
    # drop buckets that carry nothing useful
    snapshot.buckets = {
        k: b for k, b in snapshot.buckets.items() if b.limit is not None or b.remaining is not None
    }
    return snapshot
