"""/dns: Pi-hole blocking toggle, status, query log, local records, and Tailscale node status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from core.events import EventType
from core.interfaces import ProviderKind
from hub_api.deps import default_provider, spec_of_kind, with_adapter
from hub_api.state import HubState, get_state

__all__ = ["router", "BlockingToggle"]

router = APIRouter(prefix="/dns", tags=["dns"])


class BlockingToggle(BaseModel):
    enabled: bool
    duration_s: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)  # only meaningful when disabling
    provider: str | None = None


@router.get("/providers")
async def providers(state: HubState = Depends(get_state)) -> dict[str, Any]:
    return {"providers": [s.name for s in state.registry.list(ProviderKind.DNS)], "chain": state.registry.chain(ProviderKind.DNS)}


@router.get("/status")
async def dns_status(provider: str | None = None, state: HubState = Depends(get_state)) -> dict[str, Any]:
    name = default_provider(state, ProviderKind.DNS, provider)
    result = await with_adapter(state, name, lambda a: a.status())
    result.provider = result.provider or name
    return result.model_dump(mode="json")


@router.post("/blocking")
async def toggle_blocking(body: BlockingToggle, state: HubState = Depends(get_state)) -> dict[str, Any]:
    name = default_provider(state, ProviderKind.DNS, body.provider)
    duration = None if body.enabled else body.duration_s
    result = await with_adapter(state, name, lambda a: a.toggle_blocking(body.enabled, duration))
    result.provider = result.provider or name
    state.bus.publish(
        EventType.DNS_BLOCKING_CHANGED, provider=name, kind=ProviderKind.DNS,
        enabled=result.blocking_enabled, duration_s=duration,
    )
    return result.model_dump(mode="json")


@router.get("/queries")
async def query_log(
    limit: int = Query(100, ge=1, le=5000),
    blocked_only: bool = False,
    provider: str | None = None,
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    name = default_provider(state, ProviderKind.DNS, provider)
    entries = await with_adapter(state, name, lambda a: a.query_log(limit, blocked_only))
    return {"provider": name, "items": [e.model_dump(mode="json") for e in entries], "count": len(entries)}


@router.get("/records")
async def records(provider: str | None = None, state: HubState = Depends(get_state)) -> dict[str, Any]:
    name = default_provider(state, ProviderKind.DNS, provider)
    items = await with_adapter(state, name, lambda a: a.list_records())
    return {"provider": name, "items": [r.model_dump(mode="json") for r in items]}


@router.get("/nodes")
async def tailscale_nodes(provider: str | None = None, state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Tailscale devices (``VMInstance`` with state online/offline) from the tailscale compute plugin."""
    name = spec_of_kind(state, provider or state.hub.tailscale_provider, ProviderKind.COMPUTE).name
    nodes = await with_adapter(state, name, lambda a: a.list_instances())
    online = sum(1 for n in nodes if n.state in ("online", "running"))
    return {
        "provider": name,
        "items": [n.model_dump(mode="json") for n in nodes],
        "online": online,
        "total": len(nodes),
    }
