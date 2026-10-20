"""/plugins: switchboard (status + health/latency, enable/disable, reorder, settings, reset, reload).

Every mutation goes through core.registry, which persists providers.yaml atomically.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from core.interfaces import NotConfigured, ProviderKind
from core.router import ProviderStatus
from hub_api.state import HubState, get_state

__all__ = ["router", "ReorderBody", "EnabledBody", "SettingsPatch"]

router = APIRouter(prefix="/plugins", tags=["plugins"])


class ReorderBody(BaseModel):
    kind: ProviderKind
    names: list[str] = Field(min_length=1)


class EnabledBody(BaseModel):
    enabled: bool


class SettingsPatch(BaseModel):
    settings: dict[str, Any]


class PriorityBody(BaseModel):
    priority: int = Field(ge=0, le=100_000)


# Settings that decide where a plugin sends its credentials (or whether TLS is checked).
# Changing them over HTTP would let a leaked hub token redirect provider API keys to an
# arbitrary host, so they are refused unless OMNIFREE_ALLOW_ENDPOINT_SETTINGS=true.
_ENDPOINT_KEY_RE = re.compile(r"(url|endpoint|host|api_base|api_root|verify_tls|account_id)", re.I)


def endpoint_keys(patch: dict[str, Any]) -> list[str]:
    return sorted(k for k in patch if _ENDPOINT_KEY_RE.search(str(k)))


def _known(state: HubState, name: str) -> None:
    if name not in state.registry.specs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown plugin '{name}'")


def _entry(state: HubState, st: ProviderStatus) -> dict[str, Any]:
    spec = state.registry.spec(st.name)
    health = st.last_health
    return {
        **st.model_dump(mode="json"),
        "description": spec.description,
        "module": spec.module,
        "limits": [lim.model_dump(mode="json") for lim in spec.limits],
        "healthy": health.ok if health else None,
        "latency_ms": (health.latency_ms if health and health.latency_ms is not None else st.avg_latency_ms),
    }


async def _status_one(state: HubState, name: str) -> ProviderStatus:
    spec = state.registry.spec(name)
    return next(s for s in await state.router.status(spec.kind) if s.name == name)


@router.get("")
async def list_plugins(
    kind: ProviderKind | None = None,
    health: bool = Query(False, description="run a live health check on enabled plugins first"),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    if health:
        await state.router.health_all(kind)
    statuses = await state.router.status(kind)
    kinds = [kind] if kind else list(ProviderKind)
    return {
        "plugins": [_entry(state, s) for s in statuses],
        "chains": {str(k): state.registry.chain(k) for k in kinds},
    }


@router.get("/chains")
async def chains(state: HubState = Depends(get_state)) -> dict[str, list[str]]:
    return {str(k): state.registry.chain(k) for k in ProviderKind}


@router.post("/reorder")
async def reorder(body: ReorderBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    try:
        chain = await state.registry.reorder(body.kind, body.names)
    except NotConfigured as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return {"kind": str(body.kind), "chain": chain}


@router.post("/reload")
async def reload(state: HubState = Depends(get_state)) -> dict[str, Any]:
    await state.registry.reload()
    return {"count": len(state.registry.specs)}


@router.get("/{name}")
async def get_plugin(name: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    _known(state, name)
    return {
        **_entry(state, await _status_one(state, name)),
        "settings": state.registry.spec(name).settings,
        "class_meta": state.registry.class_meta(name),
    }


@router.post("/{name}/health")
async def check_health(name: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    _known(state, name)
    result = await state.router.health_check(name)
    return result.model_dump(mode="json")


async def _set_enabled(state: HubState, name: str, enabled: bool) -> dict[str, Any]:
    _known(state, name)
    await state.registry.set_enabled(name, enabled)
    return _entry(state, await _status_one(state, name))


@router.post("/{name}/enable")
async def enable(name: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await _set_enabled(state, name, True)


@router.post("/{name}/disable")
async def disable(name: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await _set_enabled(state, name, False)


@router.put("/{name}/enabled")
async def put_enabled(name: str, body: EnabledBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await _set_enabled(state, name, body.enabled)


@router.put("/{name}/priority")
async def put_priority(name: str, body: PriorityBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    _known(state, name)
    spec = await state.registry.set_priority(name, body.priority)
    return {"name": name, "priority": spec.priority, "chain": state.registry.chain(spec.kind)}


@router.patch("/{name}/settings")
async def patch_settings(name: str, body: SettingsPatch, state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Shallow-merge settings (a null value deletes the key); the plugin is re-instantiated."""
    _known(state, name)
    blocked = endpoint_keys(body.settings)
    if blocked and not state.hub.allow_endpoint_settings:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                f"endpoint/TLS settings cannot be changed over the API: {', '.join(blocked)} "
                "(edit config/providers.yaml or set OMNIFREE_ALLOW_ENDPOINT_SETTINGS=true)"
            ),
        )
    spec = await state.registry.update_settings(name, body.settings)
    return {"name": name, "settings": spec.settings}


@router.post("/{name}/reset")
async def reset(name: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Close the circuit breaker and clear the quota cooldown."""
    _known(state, name)
    state.router.reset_provider(name)
    return _entry(state, await _status_one(state, name))
