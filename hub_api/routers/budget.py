"""/budget: free-tier usage across every enabled plugin, as one flat list of gauges."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from hub_api.budget_report import build_budget
from hub_api.state import HubState, get_state

__all__ = ["router"]

router = APIRouter(tags=["budget"])


@router.get("/budget")
async def budget(state: HubState = Depends(get_state)) -> dict[str, Any]:
    return await build_budget(state.router)
