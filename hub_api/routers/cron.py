"""/cron: scheduled HTTP jobs across cron plugins (Upstash QStash, GitHub Actions ...).

Header values that look like credentials are masked in every response: the hub never
echoes a secret it was given back to a client.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, field_validator

from core.interfaces import Capability, CapabilityNotSupported, CronJob, ProviderError, ProviderKind
from hub_api.deps import spec_of_kind, with_adapter
from hub_api.state import HubState, get_state

__all__ = ["router", "CronJobBody", "mask_headers", "validate_cron"]

router = APIRouter(prefix="/cron", tags=["cron"])

MASK = "***"
_SECRET_HEADER_RE = re.compile(r"auth|token|secret|key", re.I)
_CRON_FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-?#LW]+$")


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace the value of every header whose name hints at a credential."""
    return {k: (MASK if _SECRET_HEADER_RE.search(k) else v) for k, v in headers.items()}


def validate_cron(expr: str) -> str:
    """Normalise a 5-field cron expression (minute hour day-of-month month day-of-week, UTC)."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields (minute hour dom month dow), got {len(fields)}")
    for f in fields:
        if not _CRON_FIELD_RE.match(f):
            raise ValueError(f"invalid cron field {f!r}")
    return " ".join(fields)


def _job_json(job: CronJob) -> dict[str, Any]:
    data = job.model_dump(mode="json")
    data["headers"] = mask_headers(job.headers)
    return data


class CronJobBody(BaseModel):
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    schedule: str
    target: str = Field(min_length=1)
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("method")
    @classmethod
    def _method(cls, v: str) -> str:
        method = v.strip().upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise ValueError(f"unsupported HTTP method {v!r}")
        return method


@router.get("/providers")
async def providers(state: HubState = Depends(get_state)) -> dict[str, Any]:
    out = []
    for spec in state.registry.list(ProviderKind.CRON):
        health = state.router.runtime(spec.name).last_health
        out.append(
            {
                "name": spec.name,
                "enabled": spec.enabled,
                "healthy": health.ok if health else None,
                "description": spec.description,
            }
        )
    return {"providers": out}


@router.get("/jobs")
async def jobs(provider: str | None = None, state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Jobs of one provider (errors propagate) or of every enabled one (failures go to ``errors``)."""
    if provider:
        name = spec_of_kind(state, provider, ProviderKind.CRON).name
        items: list[CronJob] = await with_adapter(state, name, lambda a: a.list())
        return {"jobs": [_job_json(j) for j in items], "errors": {}}
    names = state.registry.chain(ProviderKind.CRON)
    found: dict[str, list[CronJob]] = {}
    errors: dict[str, str] = {}

    async def one(name: str) -> None:
        try:
            found[name] = await with_adapter(state, name, lambda a: a.list())
        except ProviderError as exc:
            errors[name] = str(exc)

    await asyncio.gather(*(one(n) for n in names))
    out = [_job_json(j) for n in names for j in found.get(n, [])]
    return {"jobs": out, "errors": errors}


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(body: CronJobBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    name = spec_of_kind(state, body.provider, ProviderKind.CRON).name
    job = CronJob(
        name=body.name,
        schedule=body.schedule,
        target=body.target,
        method=body.method,
        headers=body.headers,
        body=body.body,
        provider=name,
    )
    created: CronJob = await with_adapter(state, name, lambda a: a.create(job))
    created.provider = created.provider or name
    return _job_json(created)


@router.delete("/jobs/{provider}/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(provider: str, job_id: str, state: HubState = Depends(get_state)) -> Response:
    name = spec_of_kind(state, provider, ProviderKind.CRON).name
    await with_adapter(state, name, lambda a: a.delete(job_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs/{provider}/{job_id}/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_job(provider: str, job_id: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    name = spec_of_kind(state, provider, ProviderKind.CRON).name
    adapter = await state.router.adapter(name)
    if not adapter.supports(Capability.CRON_TRIGGER):
        raise CapabilityNotSupported(f"capability '{Capability.CRON_TRIGGER}' not supported", provider=name)
    await with_adapter(state, name, lambda a: a.trigger(job_id))
    return {"triggered": True}
