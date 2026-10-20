"""/compute: instances across compute plugins (OCI, GCE, Cloud Run, Koyeb, Workers, Pages ...) and power actions."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from core.events import EventType
from core.interfaces import Capability, CapabilityNotSupported, ProviderError, ProviderKind, VMInstance
from hub_api.deps import spec_of_kind, with_adapter
from hub_api.state import HubState, get_state

__all__ = ["router"]

router = APIRouter(prefix="/compute", tags=["compute"])


@router.get("/instances")
async def instances(provider: str | None = None, state: HubState = Depends(get_state)) -> dict[str, Any]:
    names = (
        [spec_of_kind(state, provider, ProviderKind.COMPUTE).name]
        if provider
        else state.registry.chain(ProviderKind.COMPUTE)
    )
    result: dict[str, Any] = {}
    errors: dict[str, str] = {}

    async def one(name: str) -> None:
        try:
            items: list[VMInstance] = await with_adapter(state, name, lambda a: a.list_instances())
            result[name] = [i.model_dump(mode="json") for i in items]
        except ProviderError as exc:
            errors[name] = str(exc)

    await asyncio.gather(*(one(n) for n in names))
    return {"providers": {n: result[n] for n in names if n in result}, "errors": errors}


@router.get("/{provider}/instances/{instance_id}")
async def instance_status(provider: str, instance_id: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.COMPUTE)
    inst = await with_adapter(state, provider, lambda a: a.status(instance_id))
    return inst.model_dump(mode="json")


async def _power(state: HubState, provider: str, instance_id: str, action: str) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.COMPUTE)
    adapter = await state.router.adapter(provider)
    if not adapter.supports(Capability.POWER):
        raise CapabilityNotSupported(f"capability '{Capability.POWER}' not supported", provider=provider)
    inst: VMInstance = await with_adapter(
        state, provider, lambda a: a.start(instance_id) if action == "start" else a.stop(instance_id)
    )
    state.bus.publish(
        EventType.COMPUTE_STATE_CHANGED, provider=provider, kind=ProviderKind.COMPUTE,
        instance_id=instance_id, state=inst.state, action=action,
    )
    return inst.model_dump(mode="json")


@router.post("/{provider}/instances/{instance_id}/start")
async def start(provider: str, instance_id: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await _power(state, provider, instance_id, "start")


@router.post("/{provider}/instances/{instance_id}/stop")
async def stop(provider: str, instance_id: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await _power(state, provider, instance_id, "stop")
