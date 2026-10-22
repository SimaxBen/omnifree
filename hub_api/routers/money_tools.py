"""The money tools under ``/money``: merchant rules, debts and split groups.

One router so the app factory includes them in one line (see each module for the routes).
"""

from __future__ import annotations

from fastapi import APIRouter

from hub_api.routers import debts, money_rules, splits

__all__ = ["router"]

router = APIRouter()
for _module in (money_rules, debts, splits):
    router.include_router(_module.router)
