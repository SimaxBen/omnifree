"""Shared route helpers: provider lookup by kind and timed direct adapter calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import HTTPException, status

from core.interfaces import ProviderAdapter, ProviderKind, ProviderTimeout
from core.registry import PluginSpec
from hub_api.state import HubState

__all__ = ["spec_of_kind", "default_provider", "with_adapter"]

T = TypeVar("T")


def spec_of_kind(state: HubState, name: str, kind: ProviderKind) -> PluginSpec:
    spec = state.registry.specs.get(name)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown plugin '{name}'")
    if spec.kind != kind:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"plugin '{name}' is {spec.kind}, not {kind}"
        )
    return spec


def default_provider(state: HubState, kind: ProviderKind, requested: str | None) -> str:
    """``requested`` (validated) or the first enabled provider of ``kind``."""
    if requested:
        return spec_of_kind(state, requested, kind).name
    chain = state.registry.chain(kind)
    if not chain:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"no enabled {kind} provider")
    return chain[0]


async def with_adapter(
    state: HubState,
    name: str,
    op: Callable[[Any], Awaitable[T]],
    *,
    timeout_s: float | None = None,
) -> T:
    """Run ``op(adapter)`` directly on one enabled adapter (no failover), with a timeout.

    Provider exceptions propagate unchanged so the error handlers can map them
    precisely (e.g. a SQL error stays a 400 instead of becoming a chain failure).
    """
    adapter: ProviderAdapter = await state.router.adapter(name)
    limit = timeout_s if timeout_s is not None else state.registry.timeout_for(name)
    try:
        return await asyncio.wait_for(op(adapter), limit)
    except TimeoutError as exc:
        raise ProviderTimeout(f"no answer within {limit:g}s", provider=name) from exc
