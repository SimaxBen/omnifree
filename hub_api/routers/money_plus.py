"""/money subscriptions and savings goals (logic in :mod:`hub_api.money_intel`).

Subscriptions detected in the expenses:

* ``GET /money/subscriptions/suggestions``: recurring merchants not tracked yet, with confidence, cadence, next
  expected date and average amount;
* ``POST /money/subscriptions/accept`` ``{"key", ...overrides}``: a monthly one becomes an ``app_recurring``
  charge; a weekly or yearly one is tracked as an active subscription (bills, forecast, calendar);
* ``POST /money/subscriptions/dismiss`` ``{"key"}``: never suggest it again;
* ``GET /money/subscriptions``: what was accepted or dismissed; ``DELETE /money/subscriptions/{id}`` forgets one
  (a dismissed merchant can be suggested again; a weekly/yearly subscription stops being tracked).

Savings goals: ``GET|POST /money/goals``, ``GET|PATCH|DELETE /money/goals/{id}`` and
``POST /money/goals/{id}/contribute`` ``{"amount", "note"?, "date"?}`` (negative = take money out).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from hub_api.appdata import AppDB, get_appdb, local_today, new_id, now_iso
from hub_api.money_intel import (
    SUBS_TABLE,
    cached_rates,
    ensure_schema,
    goal_out,
    subscription_out,
    suggestions,
)
from hub_api.routers import money
from hub_api.routers.expenses import Category, _Fields, _valid_date

__all__ = ["router", "list_goals", "contribute", "GoalCreate", "ContributionBody", "find_goal"]

router = APIRouter(prefix="/money", tags=["money"])

_COLOR_RE = r"^#[0-9A-Fa-f]{6}$"


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- subscriptions


class AcceptBody(_Fields):
    key: str = Field(min_length=1, max_length=200)
    name: str | None = Field(None, min_length=1, max_length=200)
    amount: float | None = Field(None, gt=0)
    currency: str | None = None
    category: Category | None = None
    day: int | None = Field(None, ge=1, le=31)


class DismissBody(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@router.get("/subscriptions/suggestions")
async def subscription_suggestions(db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    items = await suggestions(db, today)
    return {"suggestions": items, "count": len(items), "as_of": today.isoformat()}


@router.get("/subscriptions")
async def list_subscriptions(db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    rows = await db.fetch(f"SELECT * FROM {SUBS_TABLE} ORDER BY status, name")
    return {"subscriptions": [subscription_out(r, today) for r in rows]}


async def _upsert(db: AppDB, row: dict[str, Any]) -> None:
    cols = list(row)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("id", "merchant_key", "created_at"))
    await db.execute(
        f"INSERT INTO {SUBS_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        f" ON CONFLICT(merchant_key) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


@router.post("/subscriptions/accept", status_code=status.HTTP_201_CREATED)
async def accept_subscription(body: AcceptBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    existing = await db.fetch_one(f"SELECT status FROM {SUBS_TABLE} WHERE merchant_key = ?", [body.key])
    if existing and existing["status"] == "accepted":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="this subscription is already tracked")
    found = next((s for s in await suggestions(db, today, include_tracked=True) if s["key"] == body.key), None)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no recurring charge detected for this merchant")
    name = (body.name or found["name"]).strip()
    amount = round(float(body.amount or found["avg_amount"]), 2)
    currency = body.currency or found["currency"]
    category = body.category or found["category"]
    now = now_iso()
    recurring: dict[str, Any] | None = None
    if found["cadence"] == "monthly":
        recurring = await money.create_recurring(
            money.RecurringCreate(
                name=name, amount=amount, currency=currency, category=category,
                day=body.day or found["day"] or int(found["next_date"][8:10]),
                note=f"Detected from {found['occurrences']} expenses", start_month=today.strftime("%Y-%m"),
            ),
            db=db,
        )
    row = {
        "id": new_id(), "merchant_key": body.key, "name": name, "status": "accepted", "frequency": found["cadence"],
        "amount": amount, "currency": currency, "category": category, "anchor_date": found["next_date"],
        "active": 0 if recurring else 1, "recurring_id": recurring["id"] if recurring else None,
        "created_at": now, "updated_at": now,
    }
    await _upsert(db, row)
    saved = await db.fetch_one(f"SELECT * FROM {SUBS_TABLE} WHERE merchant_key = ?", [body.key])
    return {"subscription": subscription_out(saved or row, today), "recurring": recurring}


@router.post("/subscriptions/dismiss")
async def dismiss_subscription(body: DismissBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    found = next((s for s in await suggestions(db, today, include_tracked=True) if s["key"] == body.key), None)
    now = now_iso()
    row = {
        "id": new_id(), "merchant_key": body.key, "name": found["name"] if found else body.key, "status": "dismissed",
        "frequency": found["cadence"] if found else "monthly", "amount": found["avg_amount"] if found else 0.0,
        "currency": found["currency"] if found else money.BASE, "category": found["category"] if found else "other",
        "anchor_date": None, "active": 0, "recurring_id": None, "created_at": now, "updated_at": now,
    }
    await _upsert(db, row)
    saved = await db.fetch_one(f"SELECT * FROM {SUBS_TABLE} WHERE merchant_key = ?", [body.key])
    return {"subscription": subscription_out(saved or row, today)}


@router.delete("/subscriptions/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_subscription(sub_id: str, db: AppDB = Depends(_db)) -> Response:
    if not await db.fetch_one(f"SELECT id FROM {SUBS_TABLE} WHERE id = ?", [sub_id]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="subscription not found")
    await db.execute(f"DELETE FROM {SUBS_TABLE} WHERE id = ?", [sub_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- goals


class _GoalFields(_Fields):
    @field_validator("deadline", check_fields=False)
    @classmethod
    def _deadline(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        d = _valid_date(v)
        if d is None or len(v.strip()) != 10:
            raise ValueError("deadline must be YYYY-MM-DD")
        return d


class GoalCreate(_GoalFields):
    name: str = Field(min_length=1, max_length=100)
    target: float = Field(gt=0)
    currency: str = money.BASE
    deadline: str | None = None
    saved: float = Field(0.0, ge=0)
    account_id: str | None = Field(None, max_length=64)
    emoji: str | None = Field(None, max_length=16)
    color: str | None = Field(None, pattern=_COLOR_RE)
    note: str | None = Field(None, max_length=2000)


class GoalPatch(_GoalFields):
    name: str | None = Field(None, min_length=1, max_length=100)
    target: float | None = Field(None, gt=0)
    currency: str | None = None
    deadline: str | None = None
    account_id: str | None = Field(None, max_length=64)
    emoji: str | None = Field(None, max_length=16)
    color: str | None = Field(None, pattern=_COLOR_RE)
    note: str | None = Field(None, max_length=2000)


class ContributionBody(_Fields):
    amount: float
    note: str | None = Field(None, max_length=500)
    date: str | None = None  # default: today

    @field_validator("amount")
    @classmethod
    def _nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("amount must not be 0")
        return round(v, 2)


async def _accounts(db: AppDB) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in await db.fetch("SELECT id, name FROM app_accounts")}


async def _goal_row(db: AppDB, goal_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_goals WHERE id = ?", [goal_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="goal not found")
    return row


async def _check_account(db: AppDB, account_id: str | None) -> None:
    if account_id and not await db.fetch_one("SELECT id FROM app_accounts WHERE id = ?", [account_id]):
        raise HTTPException(422, detail="account_id does not match an account")


async def _one(db: AppDB, goal_id: str) -> dict[str, Any]:
    return goal_out(await _goal_row(db, goal_id), local_today(), await cached_rates(db), await _accounts(db))


async def find_goal(db: AppDB, ref: str) -> dict[str, Any]:
    """A goal by id, else by name (case-insensitive): used by the MCP tools."""
    await ensure_schema(db)
    row = await db.fetch_one("SELECT * FROM app_goals WHERE id = ?", [ref])
    row = row or await db.fetch_one("SELECT * FROM app_goals WHERE lower(name) = lower(?)", [ref.strip()])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="goal not found")
    return row


@router.get("/goals")
async def list_goals(db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    rates = await cached_rates(db)
    accounts = await _accounts(db)
    rows = await db.fetch("SELECT * FROM app_goals ORDER BY (deadline IS NULL), deadline, created_at")
    goals = [goal_out(r, today, rates, accounts) for r in rows]
    return {
        "goals": goals,
        "total_saved_mad": round(sum(g["saved_mad"] or 0.0 for g in goals), 2),
        "total_target_mad": round(sum(g["target_mad"] or 0.0 for g in goals), 2),
        "currency": money.BASE,
    }


@router.post("/goals", status_code=status.HTTP_201_CREATED)
async def create_goal(body: GoalCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    await _check_account(db, body.account_id)
    now = now_iso()
    row = {
        "id": new_id(), "name": body.name.strip(), "target": round(body.target, 2), "currency": body.currency,
        "deadline": body.deadline, "saved": round(body.saved, 2), "account_id": body.account_id or None,
        "emoji": body.emoji, "color": body.color, "note": body.note, "created_at": now, "updated_at": now,
    }
    cols = list(row)
    await db.execute(f"INSERT INTO app_goals ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     [row[c] for c in cols])
    return await _one(db, row["id"])


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    goal = await _one(db, goal_id)
    rows = await db.fetch(
        "SELECT id, amount, note, date, created_at FROM app_goal_contributions WHERE goal_id = ?"
        " ORDER BY date DESC, created_at DESC",
        [goal_id], limit=50,
    )
    return {**goal, "contributions": [{**r, "amount": float(r["amount"])} for r in rows]}


@router.patch("/goals/{goal_id}")
async def patch_goal(goal_id: str, body: GoalPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    await _goal_row(db, goal_id)
    changes = money._changes(body, ("deadline", "account_id", "emoji", "color", "note"))
    if "account_id" in changes:
        changes["account_id"] = changes["account_id"] or None
        await _check_account(db, changes["account_id"])
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    if changes:
        changes["updated_at"] = now_iso()
        await money._update(db, "app_goals", goal_id, changes)
    return await _one(db, goal_id)


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(goal_id: str, db: AppDB = Depends(_db)) -> Response:
    await _goal_row(db, goal_id)
    await db.execute("DELETE FROM app_goal_contributions WHERE goal_id = ?", [goal_id])
    await db.execute("DELETE FROM app_goals WHERE id = ?", [goal_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/goals/{goal_id}/contribute", status_code=status.HTTP_201_CREATED)
async def contribute(goal_id: str, body: ContributionBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    row = await _goal_row(db, goal_id)
    saved = round(float(row["saved"] or 0) + body.amount, 2)
    if saved < 0:
        raise HTTPException(422, detail=f"only {float(row['saved'] or 0):g} {row['currency']} saved: cannot take out more")
    now = now_iso()
    # Guarded update: a concurrent contribution that changed `saved` in between makes this one retry-safe (409).
    changed = await db.execute("UPDATE app_goals SET saved = ?, updated_at = ? WHERE id = ? AND saved = ?",
                               [saved, now, goal_id, row["saved"]])
    if not changed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the goal changed meanwhile: try again")
    contribution = {"id": new_id(), "goal_id": goal_id, "amount": body.amount, "note": body.note,
                    "date": body.date or local_today().isoformat(), "created_at": now}
    cols = list(contribution)
    await db.execute(
        f"INSERT INTO app_goal_contributions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [contribution[c] for c in cols],
    )
    return {"goal": await _one(db, goal_id), "contribution": contribution}
