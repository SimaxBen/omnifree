"""Free-tier budget aggregation for ``GET /budget``.

For every enabled plugin (LLM, storage, db, compute, dns, cron) the report merges, per
(metric, window):

1. the provider-reported quota (``adapter.quota()``; ``DBAdapter.usage()`` for databases),
2. the router counters for each ``limits`` entry of providers.yaml, when the router actually
   meters that metric (requests/tokens for every kind, bytes for storage),
3. otherwise the configured limit alone, with ``used: null`` and status ``unknown``.

A plugin without any limit or provider number still shows its router day/month counters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from core.interfaces import DBAdapter, ProviderKind, QuotaInfo
from core.router import SmartRouter

__all__ = ["build_budget", "status_of", "fraction_of", "WARN_AT", "OVER_AT"]

log = logging.getLogger("omnifree.hub.budget")

WARN_AT = 0.8
OVER_AT = 1.0
PROVIDER_TIMEOUT_S = 10.0
_ALIASES = {"calls": "requests", "storage_bytes": "bytes"}
_ROUTER_METRICS = frozenset({"requests", "tokens"})


def _metric(name: str) -> str:
    return _ALIASES.get(name, name)


def fraction_of(used: float | None, limit: float | None) -> float | None:
    if used is None or limit is None or limit <= 0:
        return None
    return round(used / limit, 4)


def status_of(used: float | None, limit: float | None) -> str:
    if used is None:
        return "unknown"
    fraction = fraction_of(used, limit)
    if fraction is None:
        return "ok" if limit is None else "unknown"
    if fraction >= OVER_AT:
        return "over"
    if fraction >= WARN_AT:
        return "warn"
    return "ok"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _item(
    spec: Any, metric: str, window: str | None, used: float | None, limit: float | None,
    unit: str, reset_at: datetime | None, source: str,
) -> dict[str, Any]:
    return {
        "provider": spec.name,
        "kind": str(spec.kind),
        "group": spec.group,
        "metric": metric,
        "window": window,
        "used": used,
        "limit": limit,
        "unit": unit,
        "fraction": fraction_of(used, limit),
        "status": status_of(used, limit),
        "reset_at": _iso(reset_at),
        "source": source,
    }


async def _provider_reported(router: SmartRouter, spec: Any) -> list[QuotaInfo]:
    """Live numbers from the provider itself (empty when it reports nothing usable)."""
    out: list[QuotaInfo] = []
    try:
        q = await asyncio.wait_for(router.provider_quota(spec.name), PROVIDER_TIMEOUT_S)
        if q.unit != "unknown" and q.source != "config":
            out.append(q)
    except Exception as exc:  # a broken provider must not break the page
        log.debug("quota of %s failed: %r", spec.name, exc)
    if not out and spec.kind == ProviderKind.DB:
        try:
            adapter = await router.adapter(spec.name)
            if isinstance(adapter, DBAdapter):
                q = await asyncio.wait_for(adapter.usage(), PROVIDER_TIMEOUT_S)
                if q.unit != "unknown":
                    out.append(q)
        except Exception as exc:
            log.debug("db usage of %s failed: %r", spec.name, exc)
    return out


async def _plugin_items(router: SmartRouter, spec: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    for q in await _provider_reported(router, spec):
        metric = _metric(q.unit)
        window = q.window
        if q.limit is None:  # fill the limit from providers.yaml when the provider omits it
            q_limit = next(
                (lim.limit for lim in spec.limits if _metric(lim.metric) == metric and lim.window == (window or "total")),
                None,
            )
        else:
            q_limit = q.limit
        items.append(_item(spec, metric, window, q.used, q_limit, q.unit, q.reset_at, "provider"))
        seen.add((metric, window))
        if window is None:
            seen.add((metric, "total"))

    try:
        counters = await router.usage(spec.name)
    except Exception as exc:
        log.debug("router usage of %s failed: %r", spec.name, exc)
        counters = []
    by_key = {(_metric(q.unit), q.window): q for q in counters}

    tracked = set(_ROUTER_METRICS)
    if spec.kind == ProviderKind.STORAGE:
        tracked.add("bytes")
    for lim in spec.limits:
        metric = _metric(lim.metric)
        key = (metric, lim.window)
        if key in seen:
            continue
        seen.add(key)
        counter = by_key.get(key)
        if metric in tracked and counter is not None:
            items.append(_item(spec, metric, lim.window, counter.used, lim.limit, lim.metric, counter.reset_at, "router"))
        else:
            items.append(_item(spec, metric, lim.window, None, lim.limit, lim.metric, None, "config"))

    if not items:  # nothing configured or reported: show what the router counted
        for q in counters:
            items.append(_item(spec, _metric(q.unit), q.window, q.used, None, q.unit, q.reset_at, "router"))
    return items


async def build_budget(router: SmartRouter) -> dict[str, Any]:
    specs = router.registry.list(None, enabled_only=True)
    per_plugin = await asyncio.gather(*(_plugin_items(router, spec) for spec in specs))
    items = [item for group in per_plugin for item in group]
    summary = {"ok": 0, "warn": 0, "over": 0, "unknown": 0}
    for item in items:
        summary[item["status"]] += 1
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "items": items,
        "summary": summary,
    }
