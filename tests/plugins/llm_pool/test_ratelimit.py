from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.llm_pool._ratelimit import parse_rate_limit_headers, parse_reset, parse_retry_hint

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2m59.56s", timedelta(minutes=2, seconds=59.56)),
        ("6ms", timedelta(milliseconds=6)),
        ("1h2m", timedelta(hours=1, minutes=2)),
        ("33011.38", timedelta(seconds=33011.38)),
        ("0", timedelta(0)),
    ],
)
def test_parse_reset_relative(value: str, expected: timedelta) -> None:
    assert parse_reset(value, NOW) == NOW + expected


def test_parse_reset_epochs_and_garbage() -> None:
    ms = int((NOW + timedelta(seconds=30)).timestamp() * 1000)
    assert parse_reset(str(ms), NOW) == NOW + timedelta(seconds=30)
    assert parse_reset(str(int(NOW.timestamp()) + 60), NOW) == NOW + timedelta(seconds=60)
    assert parse_reset("2026-10-20T00:00:00Z", NOW) == datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert parse_reset("soon", NOW) is None
    assert parse_reset("", NOW) is None


def test_groq_style_headers_use_configured_windows() -> None:
    snap = parse_rate_limit_headers(
        {
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "990",
            "x-ratelimit-reset-requests": "1m26.4s",
            "x-ratelimit-limit-tokens": "6000",
            "x-ratelimit-remaining-tokens": "5000",
            "x-ratelimit-reset-tokens": "10s",
            "content-type": "application/json",
        },
        {"requests": "day", "tokens": "minute"},
        NOW,
    )
    day = snap.buckets[("requests", "day")]
    assert (day.limit, day.remaining, day.used) == (1000, 990, 10)
    assert day.reset_at == NOW + timedelta(seconds=86.4)
    assert snap.buckets[("tokens", "minute")].remaining == 5000
    assert snap.primary() is day


def test_cerebras_style_explicit_windows_and_exhaustion() -> None:
    snap = parse_rate_limit_headers(
        {
            "x-ratelimit-limit-requests-day": "14400",
            "x-ratelimit-remaining-requests-day": "0",
            "x-ratelimit-reset-requests-day": "3600",
            "x-ratelimit-limit-tokens-minute": "60000",
            "x-ratelimit-remaining-tokens-minute": "59000",
        },
        now=NOW,
    )
    assert set(snap.buckets) == {("requests", "day"), ("tokens", "minute")}
    assert snap.exhausted(NOW) is snap.buckets[("requests", "day")]
    assert snap.exhausted(NOW + timedelta(hours=2)) is None


def test_openrouter_generic_headers() -> None:
    reset_ms = int((NOW + timedelta(seconds=40)).timestamp() * 1000)
    snap = parse_rate_limit_headers(
        {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "19", "X-RateLimit-Reset": str(reset_ms)}, now=NOW
    )
    bucket = snap.buckets[("requests", None)]
    assert (bucket.limit, bucket.remaining) == (20, 19)
    assert bucket.reset_at == NOW + timedelta(seconds=40)


def test_no_headers_is_falsy() -> None:
    assert not parse_rate_limit_headers({"content-type": "x"})


@pytest.mark.parametrize(
    ("body", "seconds"),
    [
        ("Rate limit reached ... Please try again in 1m20.5s. Visit", 80.5),
        ('{"error": {"details": [{"retryDelay": "31s"}]}}', 31),
        ("Retry after 12 seconds", 12),
        ("nothing here", None),
    ],
)
def test_retry_hint(body: str, seconds: float | None) -> None:
    hint = parse_retry_hint(body)
    if seconds is None:
        assert hint is None
    else:
        assert hint == pytest.approx(seconds, abs=1)
