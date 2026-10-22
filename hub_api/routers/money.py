"""/money: incomes, recurring expenses, accounts, budgets, FX and a monthly overview; voice money capture.

Everything lives in the apps database next to ``app_expenses`` (see :mod:`hub_api.routers.expenses`):

* ``app_incomes``: one-off incomes, or monthly ones (``recurring = 1``) that count every month from the
  month of ``date`` until ``end_month`` (inclusive, open-ended when NULL);
* ``app_recurring``: monthly expenses (rent, subscriptions...) with the same model (``start_month`` ..
  ``end_month``); they count in the month totals without being copied into ``app_expenses``;
* ``app_accounts``: "the money I have", balances set by hand;
* ``app_budgets``: a monthly limit per expense category;
* ``app_fx_cache``: exchange rates (base MAD) from a free no-key API, kept 24 h, with a static fallback.

A monthly charge that an expense of the same month already paid (same merchant key, amount within 15 %,
see :mod:`hub_api.money_match`) counts once: the expense stays, the charge is shown as ``paid``. The
overview also carries an end-of-month ``forecast`` (see :func:`build_forecast`).

Totals are in MAD; every item keeps its original amount and currency. ``POST /money/voice`` turns a
recording ("j'ai payé 45 dh de taxi et 30 dh un café") into expenses (``source = 'voice'``) and incomes, and
loans to or from people ("j'ai prêté 200 dh à Ayoub", "Ayoub m'a rendu 100") into debts (see
:mod:`hub_api.routers.debts`). A bill the speaker paid and shares equally with named people ("j'ai payé 300 dh le
dîner avec Ayoub et Sara, chacun sa part") becomes a shared expense (``type = 'split'``, see
:func:`hub_api.routers.splits.record_voice_split`): the speaker's share goes to the expenses, the others' to Debts.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from core.interfaces import ChatMessage, ChatRequest
from hub_api import idempotency
from hub_api.appdata import AppDB, extract_json, get_appdb, iso, local_today, new_id, now_iso
from hub_api.money_match import amount_close, merchant_key
from hub_api.receipts import CATEGORIES
from hub_api.routers.expenses import (
    _MONTH_RE,
    Category,
    _Fields,
    _number,
    insert_expense,
    normalise_currency,
)
from hub_api.routers.expenses import ensure_schema as ensure_expenses
from hub_api.routers.notes import _WEEKDAYS, resolve_due, split_time, store_and_transcribe
from hub_api.state import HubState, get_state

__all__ = [
    "router", "SCHEMA", "INCOME_CATEGORIES", "FALLBACK_TO_MAD", "Rates", "ensure_schema", "rates_from_payload",
    "get_rates", "month_shift", "active_in", "occurrence", "resolve_spent_date", "parse_money_entries",
    "create_entries", "insert_income", "build_overview", "build_forecast", "paid_charges",
]

log = logging.getLogger("omnifree.hub.money")

router = APIRouter(prefix="/money", tags=["money"])

BASE = "MAD"
INCOME_CATEGORIES = ("salary", "freelance", "refund", "gift", "other")
IncomeCategory = Literal["salary", "freelance", "refund", "gift", "other"]
AccountKind = Literal["cash", "bank", "savings", "card", "other"]
SERIES_MONTHS = 6
FORECAST_HISTORY_MONTHS = 3  # past months whose day-to-day spend seeds the forecast early in the month
FORECAST_PRIOR_DAYS = 10  # the history weighs like this many days, fading as the month goes on
SUB_PAY_WINDOW = {"weekly": 3, "yearly": 20}  # days between a subscription charge and the expense that pays it

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_incomes ("
    " id TEXT PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'MAD',"
    " source TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'other', note TEXT,"
    " recurring INTEGER NOT NULL DEFAULT 0, end_month TEXT, origin TEXT, audio_key TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_incomes_date ON app_incomes (date)",
    "CREATE TABLE IF NOT EXISTS app_recurring ("
    " id TEXT PRIMARY KEY, name TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'MAD',"
    " category TEXT NOT NULL DEFAULT 'bills', day INTEGER NOT NULL DEFAULT 1, start_month TEXT NOT NULL,"
    " end_month TEXT, note TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_accounts ("
    " id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'bank', balance REAL NOT NULL DEFAULT 0,"
    " currency TEXT NOT NULL DEFAULT 'MAD', note TEXT, updated_at TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_budgets ("
    " category TEXT PRIMARY KEY, amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'MAD', updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_fx_cache (base TEXT PRIMARY KEY, rates TEXT NOT NULL, fetched_at TEXT NOT NULL)",
)


async def ensure_schema(db: AppDB) -> AppDB:
    """Create the money tables (and ``app_expenses``, which the overview reads)."""
    await ensure_expenses(db)
    await db.ensure("money", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- FX

FX_URL = "https://open.er-api.com/v6/latest/MAD"
FX_TTL_S = 24 * 3600
FX_TIMEOUT_S = 4.0
# MAD for one unit of each currency, used when the API and the cache are both unavailable (approximate).
FALLBACK_TO_MAD: dict[str, float] = {
    "MAD": 1.0, "EUR": 10.8, "USD": 9.3, "GBP": 12.5, "CHF": 11.5, "CAD": 6.7, "AED": 2.53, "SAR": 2.48,
    "QAR": 2.55, "TRY": 0.23, "CNY": 1.3, "JPY": 0.062, "TND": 3.15, "DZD": 0.07, "EGP": 0.19, "XOF": 0.0165,
}
# Transport for the FX request; tests replace it with an httpx.MockTransport.
fx_transport: httpx.AsyncBaseTransport | None = None


@dataclass
class Rates:
    """Conversion to MAD: ``to_mad[cur]`` MAD for one unit of ``cur``."""

    to_mad: dict[str, float]
    source: Literal["live", "cache", "stale", "fallback"]
    fetched_at: str | None = None

    def convert(self, amount: float, currency: str) -> float | None:
        rate = self.to_mad.get((currency or BASE).upper())
        return None if rate is None else round(amount * rate, 2)

    def public(self) -> dict[str, Any]:
        return {"base": BASE, "source": self.source, "fetched_at": self.fetched_at}


def rates_from_payload(payload: Any) -> dict[str, float] | None:
    """``{"result": "success", "rates": {"USD": 0.107, ...}}`` (units per MAD) -> MAD per unit."""
    if not isinstance(payload, dict) or payload.get("result", "success") != "success":
        return None
    raw = payload.get("rates")
    if not isinstance(raw, dict):
        return None
    out = {
        str(cur).upper(): 1.0 / float(r)
        for cur, r in raw.items()
        if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0
    }
    if len(out) < 2:
        return None
    out[BASE] = 1.0
    return out


async def _fetch_rates() -> dict[str, float] | None:
    async with httpx.AsyncClient(timeout=FX_TIMEOUT_S, transport=fx_transport) as client:
        resp = await client.get(FX_URL)
        resp.raise_for_status()
        return rates_from_payload(resp.json())


async def get_rates(db: AppDB, now: datetime | None = None) -> Rates:
    """Cached rates when fresh (< 24 h), else a fetch; a failed fetch uses the stale cache or the fallback."""
    now = now or datetime.now(timezone.utc)
    row = await db.fetch_one("SELECT rates, fetched_at FROM app_fx_cache WHERE base = ?", [BASE])
    cached: dict[str, float] | None = None
    if row:
        try:
            value = json.loads(row["rates"])
            cached = {str(k): float(v) for k, v in value.items()} if isinstance(value, dict) else None
        except (ValueError, TypeError):
            cached = None
        try:
            age = (now - datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = FX_TTL_S
        if cached and 0 <= age < FX_TTL_S:
            return Rates(cached, "cache", row["fetched_at"])
    try:
        fresh = await _fetch_rates()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("FX fetch failed: %r", exc)
        fresh = None
    if fresh:
        stamp = iso(now)
        await db.execute(
            "INSERT INTO app_fx_cache (base, rates, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT(base) DO UPDATE SET rates = excluded.rates, fetched_at = excluded.fetched_at",
            [BASE, json.dumps(fresh), stamp],
        )
        return Rates(fresh, "live", stamp)
    if cached:
        return Rates(cached, "stale", row["fetched_at"] if row else None)
    return Rates(dict(FALLBACK_TO_MAD), "fallback", None)


# --------------------------------------------------------------------------- months


def month_shift(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def active_in(start_month: str, end_month: str | None, month: str) -> bool:
    """A monthly item counts in ``month`` when ``start_month <= month <= end_month`` (open end when None)."""
    return start_month <= month and (not end_month or month <= end_month)


def occurrence(month: str, day: int) -> str:
    """The date a monthly item falls on in ``month`` (day clamped to the month's length)."""
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, max(1, min(int(day or 1), calendar.monthrange(y, m)[1]))).isoformat()


def _check_month(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not re.match(_MONTH_RE, value):
        raise ValueError(f"{field} must be YYYY-MM")
    return value


# --------------------------------------------------------------------------- rows


def _income_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "date": row["date"],
        "amount": float(row.get("amount") or 0),
        "currency": row.get("currency") or BASE,
        "source": row.get("source") or "",
        "category": row.get("category") or "other",
        "note": row.get("note"),
        "recurring": bool(row.get("recurring")),
        "end_month": row.get("end_month"),
        "origin": row.get("origin"),
        "audio_key": row.get("audio_key"),
        "created_at": row["created_at"],
    }


def _recurring_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "amount": float(row.get("amount") or 0),
        "currency": row.get("currency") or BASE,
        "category": row.get("category") or "bills",
        "day": int(row.get("day") or 1),
        "start_month": row["start_month"],
        "end_month": row.get("end_month"),
        "note": row.get("note"),
        "created_at": row["created_at"],
    }


def _account_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row.get("kind") or "bank",
        "balance": float(row.get("balance") or 0),
        "currency": row.get("currency") or BASE,
        "note": row.get("note"),
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
    }


def _budget_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": row["category"],
        "amount": float(row.get("amount") or 0),
        "currency": row.get("currency") or BASE,
        "updated_at": row["updated_at"],
    }


async def _insert(db: AppDB, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    await db.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )


async def insert_income(db: AppDB, fields: dict[str, Any]) -> dict[str, Any]:
    row = {
        "id": fields.get("id") or new_id(),
        "date": fields["date"],
        "amount": float(fields["amount"]),
        "currency": fields.get("currency") or BASE,
        "source": (fields.get("source") or "").strip()[:200],
        "category": fields.get("category") if fields.get("category") in INCOME_CATEGORIES else "other",
        "note": fields.get("note"),
        "recurring": 1 if fields.get("recurring") else 0,
        "end_month": fields.get("end_month"),
        "origin": fields.get("origin"),
        "audio_key": fields.get("audio_key"),
        "created_at": now_iso(),
    }
    await _insert(db, "app_incomes", row)
    return _income_out(row)


async def _get(db: AppDB, table: str, key: str, value: str, what: str) -> dict[str, Any]:
    row = await db.fetch_one(f"SELECT * FROM {table} WHERE {key} = ?", [value])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{what} not found")
    return row


async def _update(db: AppDB, table: str, row_id: str, changes: dict[str, Any]) -> None:
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*changes.values(), row_id])


def _changes(body: BaseModel, nullable: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in nullable:
            raise HTTPException(422, detail=f"{field} cannot be null")
        if isinstance(value, bool):
            value = 1 if value else 0
        out[field] = value
    return out


# --------------------------------------------------------------------------- models


class _MonthFields(_Fields):
    @field_validator("start_month", "end_month", check_fields=False)
    @classmethod
    def _month(cls, v: str | None, info: Any) -> str | None:
        return _check_month(v, info.field_name)


class IncomeCreate(_MonthFields):
    id: str | None = Field(None, description="client-generated id (offline queue); a replay answers the existing income")
    date: str | None = None  # default: today; for a monthly income, its first month and pay day
    amount: float = Field(ge=0)
    currency: str = BASE
    source: str = Field("", max_length=200)
    category: IncomeCategory = "other"
    note: str | None = Field(None, max_length=5000)
    recurring: bool = False
    end_month: str | None = None


class IncomePatch(_MonthFields):
    date: str | None = None
    amount: float | None = Field(None, ge=0)
    currency: str | None = None
    source: str | None = Field(None, max_length=200)
    category: IncomeCategory | None = None
    note: str | None = Field(None, max_length=5000)
    recurring: bool | None = None
    end_month: str | None = None


class RecurringCreate(_MonthFields):
    name: str = Field(min_length=1, max_length=200)
    amount: float = Field(ge=0)
    currency: str = BASE
    category: Category = "bills"
    day: int = Field(1, ge=1, le=31)
    start_month: str | None = None  # default: this month
    end_month: str | None = None
    note: str | None = Field(None, max_length=5000)


class RecurringPatch(_MonthFields):
    name: str | None = Field(None, min_length=1, max_length=200)
    amount: float | None = Field(None, ge=0)
    currency: str | None = None
    category: Category | None = None
    day: int | None = Field(None, ge=1, le=31)
    start_month: str | None = None
    end_month: str | None = None
    note: str | None = Field(None, max_length=5000)


class AccountCreate(_Fields):
    name: str = Field(min_length=1, max_length=100)
    kind: AccountKind = "bank"
    balance: float = 0.0
    currency: str = BASE
    note: str | None = Field(None, max_length=2000)


class AccountPatch(_Fields):
    name: str | None = Field(None, min_length=1, max_length=100)
    kind: AccountKind | None = None
    balance: float | None = None
    currency: str | None = None
    note: str | None = Field(None, max_length=2000)


class BudgetSet(_Fields):
    amount: float = Field(gt=0)
    currency: str = BASE


def _check_range(start: str | None, end: str | None) -> None:
    if start and end and end < start:
        raise HTTPException(422, detail="end_month must not be before the start month")


# --------------------------------------------------------------------------- overview


def _income_months(row: dict[str, Any]) -> tuple[str, str | None]:
    return str(row["date"])[:7], row.get("end_month")


def build_overview(
    month: str,
    today: date,
    rates: Rates,
    *,
    expenses: list[dict[str, Any]],
    incomes: list[dict[str, Any]],
    recurring: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    budgets: list[dict[str, Any]],
    extra_recurring: list[dict[str, Any]] | None = None,
    subscriptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The month's figures in MAD (pure: rows in, JSON out). Rows are the ``*_out`` shapes.

    ``extra_recurring``: weekly/yearly subscription charges falling in ``month`` (``id, name, amount, currency,
    on``, optional ``key, frequency``); they only feed ``upcoming`` and the forecast, the expense that pays them
    counts in the totals. ``subscriptions``: the active weekly/yearly subscriptions (``key, amount, currency``;
    default: the ones of ``extra_recurring``); their expenses are not day-to-day spend for the forecast.
    """
    months = [month_shift(month, -i) for i in range(SERIES_MONTHS - 1, -1, -1)]
    unconverted: set[str] = set()

    def mad(amount: float, currency: str) -> float:
        value = rates.convert(amount, currency)
        if value is None:
            unconverted.add(currency)
            return 0.0
        return value

    income_by: dict[str, float] = {m: 0.0 for m in months}
    spent_by: dict[str, float] = {m: 0.0 for m in months}
    variable_by: dict[str, float] = {m: 0.0 for m in months}  # expenses that pay no recurring charge
    paid = paid_charges(months, recurring, expenses, mad)
    extra = [{**x, "amount_mad": mad(x["amount"], x["currency"])} for x in extra_recurring or []
             if str(x["on"])[:7] == month]
    if subscriptions is None:
        subscriptions = list({x["id"]: x for x in extra}.values())
    sub_paying, extra_paid = paid_subscriptions(subscriptions, extra, expenses, mad, set(paid.values()))
    extra = [x for i, x in enumerate(extra) if i not in extra_paid]  # paid charges are in the totals already
    paying = set(paid.values()) | sub_paying
    for idx, e in enumerate(expenses):
        m = str(e["date"])[:7]
        if m in spent_by:
            value = mad(float(e["total"]), e["currency"])
            spent_by[m] += value
            if idx not in paying:
                variable_by[m] += value
    for r in recurring:
        for m in months:
            if active_in(r["start_month"], r.get("end_month"), m) and (r["id"], m) not in paid:
                spent_by[m] += mad(r["amount"], r["currency"])
    for i in incomes:
        start, end = _income_months(i)
        for m in months:
            if (i["recurring"] and active_in(start, end, m)) or (not i["recurring"] and start == m):
                income_by[m] += mad(i["amount"], i["currency"])

    current = month == today.strftime("%Y-%m")
    future = month > today.strftime("%Y-%m")

    def upcoming(day: str) -> bool:
        return future or (current and day >= today.isoformat())

    month_incomes = []
    for i in incomes:
        start, end = _income_months(i)
        if (i["recurring"] and active_in(start, end, month)) or (not i["recurring"] and start == month):
            day = occurrence(month, int(str(i["date"])[8:10])) if i["recurring"] else i["date"]
            month_incomes.append({**i, "amount_mad": mad(i["amount"], i["currency"]), "on": day})
    month_incomes.sort(key=lambda x: (x["on"], x["created_at"]), reverse=True)

    month_recurring = []
    for r in recurring:
        if active_in(r["start_month"], r.get("end_month"), month):
            day = occurrence(month, r["day"])
            is_paid = (r["id"], month) in paid
            month_recurring.append({**r, "amount_mad": mad(r["amount"], r["currency"]), "on": day,
                                    "upcoming": upcoming(day) and not is_paid, "paid": is_paid})
    month_recurring.sort(key=lambda x: (x["on"], x["name"]))

    month_expenses = [e for e in expenses if str(e["date"])[:7] == month]
    expense_total = round(sum(mad(float(e["total"]), e["currency"]) for e in month_expenses), 2)
    recurring_total = round(sum(r["amount_mad"] for r in month_recurring if not r["paid"]), 2)

    per_cat: dict[str, float] = {}
    for e in month_expenses:
        per_cat[e["category"]] = per_cat.get(e["category"], 0.0) + mad(float(e["total"]), e["currency"])
    for r in month_recurring:
        if not r["paid"]:
            per_cat[r["category"]] = per_cat.get(r["category"], 0.0) + r["amount_mad"]
    budget_of = {b["category"]: b for b in budgets}
    categories = []
    for cat in sorted(set(per_cat) | set(budget_of), key=lambda c: (-per_cat.get(c, 0.0), c)):
        spent = round(per_cat.get(cat, 0.0), 2)
        b = budget_of.get(cat)
        limit = mad(b["amount"], b["currency"]) if b else None
        categories.append(
            {
                "category": cat,
                "spent": spent,
                "budget": limit,
                "budget_original": {"amount": b["amount"], "currency": b["currency"]} if b else None,
                "fraction": round(spent / limit, 4) if limit else None,
            }
        )

    upcoming_items = [
        {"kind": "expense", "id": r["id"], "name": r["name"], "amount": r["amount"], "currency": r["currency"],
         "amount_mad": r["amount_mad"], "date": r["on"]}
        for r in month_recurring if r["upcoming"]
    ] + [
        {"kind": "income", "id": i["id"], "name": i["source"] or i["category"], "amount": i["amount"],
         "currency": i["currency"], "amount_mad": i["amount_mad"], "date": i["on"]}
        for i in month_incomes if i["recurring"] and upcoming(i["on"])
    ]
    upcoming_items += [
        {"kind": "expense", "id": x["id"], "name": x["name"], "amount": x["amount"], "currency": x["currency"],
         "amount_mad": x["amount_mad"], "date": x["on"]}
        for x in extra if upcoming(x["on"])
    ]
    upcoming_items.sort(key=lambda x: (x["date"], x["name"]))

    account_items = [{**a, "balance_mad": mad(a["balance"], a["currency"])} for a in accounts]
    income_total = round(income_by[month], 2)
    spent_total = round(expense_total + recurring_total, 2)
    net = round(income_total - spent_total, 2)
    return {
        "month": month,
        "currency": BASE,
        "income": {"total": income_total, "items": month_incomes},
        "spent": {"total": spent_total, "expenses": expense_total, "recurring": recurring_total,
                  "count": len(month_expenses)},
        "net": net,
        "savings_rate": round(net / income_total, 4) if income_total > 0 else None,
        "categories": categories,
        "series": [{"month": m, "income": round(income_by[m], 2), "spent": round(spent_by[m], 2)} for m in months],
        "recurring": month_recurring,
        "upcoming": upcoming_items,
        "accounts": {"total": round(sum(a["balance_mad"] for a in account_items), 2), "items": account_items},
        "fx": {**rates.public(), "unconverted": sorted(unconverted)},
        "forecast": build_forecast(
            month, today, income_total=income_total, expense_total=expense_total, variable_by=variable_by,
            charges=[(r["on"], r["amount_mad"]) for r in month_recurring if not r["paid"]],
            extra=[(x["on"], x["amount_mad"]) for x in extra],
        ),
    }


def paid_charges(
    months: list[str], recurring: list[dict[str, Any]], expenses: list[dict[str, Any]], mad: Any
) -> dict[tuple[str, str], int]:
    """``{(recurring id, month): index in expenses of the expense that paid it}``.

    An expense pays a monthly charge of its month when the merchant keys match and the amounts (in MAD) are
    within 15 %; each expense pays at most one charge.
    """
    by_month: dict[str, list[tuple[int, str, float]]] = {}
    for idx, e in enumerate(expenses):
        key = merchant_key(e.get("merchant"))
        if key and str(e["date"])[:7] in months:
            by_month.setdefault(str(e["date"])[:7], []).append((idx, key, mad(float(e["total"]), e["currency"])))
    out: dict[tuple[str, str], int] = {}
    if not by_month:
        return out
    used: set[int] = set()
    for r in recurring:
        key = merchant_key(r.get("name"))
        if not key:
            continue
        target = mad(r["amount"], r["currency"])
        for m in months:
            if not active_in(r["start_month"], r.get("end_month"), m):
                continue
            for idx, k, value in by_month.get(m, []):
                if idx not in used and k == key and amount_close(value, target):
                    out[(r["id"], m)] = idx
                    used.add(idx)
                    break
    return out


def paid_subscriptions(
    subscriptions: list[dict[str, Any]], extra: list[dict[str, Any]], expenses: list[dict[str, Any]], mad: Any,
    used: set[int],
) -> tuple[set[int], set[int]]:
    """``(indexes in expenses that pay a weekly/yearly subscription, indexes in extra of charges already paid)``.

    An expense pays a subscription when the merchant keys match and the amounts (in MAD) are within 15 %
    (expenses in ``used`` already pay a monthly charge); a charge of ``extra`` is paid by one of those expenses
    dated within :data:`SUB_PAY_WINDOW` days of it, each expense paying at most one charge.
    """
    targets: dict[str, list[float]] = {}
    for sub in subscriptions:
        if key := str(sub.get("key") or merchant_key(sub.get("name"))):
            targets.setdefault(key, []).append(mad(float(sub["amount"]), sub["currency"]))
    if not targets:
        return set(), set()
    paying: dict[int, tuple[str, date]] = {}
    for idx, e in enumerate(expenses):
        key = merchant_key(e.get("merchant"))
        if idx in used or key not in targets:
            continue
        value = mad(float(e["total"]), e["currency"])
        if any(amount_close(value, t) for t in targets[key]):
            paying[idx] = (key, date.fromisoformat(str(e["date"])[:10]))
    paid: set[int] = set()
    consumed: set[int] = set()
    for i, x in sorted(enumerate(extra), key=lambda p: str(p[1]["on"])):
        key = str(x.get("key") or merchant_key(x.get("name")))
        on = date.fromisoformat(str(x["on"])[:10])
        window = SUB_PAY_WINDOW.get(str(x.get("frequency") or "weekly"), 3)
        for idx, (k, day) in paying.items():
            if idx not in consumed and k == key and abs((day - on).days) <= window:
                paid.add(i)
                consumed.add(idx)
                break
    return set(paying), paid


def build_forecast(
    month: str,
    today: date,
    *,
    income_total: float,
    expense_total: float,
    variable_by: dict[str, float],
    charges: list[tuple[str, float]],
    extra: list[tuple[str, float]],
) -> dict[str, Any]:
    """End of ``month`` in MAD: spent so far + recurring still to come + daily variable spend x days left.

    ``charges``: the month's unpaid monthly charges ``(date, MAD)`` (already in the totals); ``extra``: the month's
    unpaid weekly or yearly subscription charges (not in the totals: only the ones still to come count). The daily
    average is this month's variable spend (expenses that pay no recurring charge) per elapsed day, blended
    with the average of the last months, which weighs like :data:`FORECAST_PRIOR_DAYS` days and fades as days
    pass. Past months answer their actual figures; a future month is projected from the history only.
    """
    y, m = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(y, m)[1]
    this = today.strftime("%Y-%m")
    elapsed = days if month < this else 0 if month > this else today.day
    remaining_days = days - elapsed
    cut = today.isoformat() if month == this else ("9999" if month < this else "0000")
    past_charges = sum(v for d, v in charges if d < cut)
    remaining_recurring = sum(v for d, v in charges if d >= cut) + sum(v for d, v in extra if d >= cut)

    history = [p for p in sorted(variable_by) if p < month and p < this][-FORECAST_HISTORY_MONTHS:]
    history = [p for p in history if variable_by[p] > 0]
    hist_days = sum(calendar.monthrange(int(p[:4]), int(p[5:7]))[1] for p in history)
    hist_daily = sum(variable_by[p] for p in history) / hist_days if hist_days else None
    variable = variable_by.get(month, 0.0)
    prior = max(0, FORECAST_PRIOR_DAYS - elapsed) if hist_daily is not None else 0
    weight = elapsed + prior
    daily_avg = (variable + (hist_daily or 0.0) * prior) / weight if weight else 0.0
    if month > this and hist_daily is not None:
        daily_avg = hist_daily

    spent_so_far = expense_total + past_charges
    projected_spent = spent_so_far + remaining_recurring + daily_avg * remaining_days
    if month < this:
        confidence = "high"
    elif month > this:
        confidence = "medium" if len(history) >= 2 else "low"
    elif elapsed >= 20 or (elapsed >= 10 and len(history) >= 2):
        confidence = "high"
    elif elapsed >= 7 or history:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "projected_spent": round(projected_spent, 2),
        "projected_left": round(income_total - projected_spent, 2),
        "daily_avg": round(daily_avg, 2),
        "remaining_recurring": round(remaining_recurring, 2),
        "spent_so_far": round(spent_so_far, 2),
        "variable_spent": round(variable, 2),
        "remaining_days": remaining_days,
        "month_end": date(y, m, days).isoformat(),
        "history_months": len(history),
        "history_daily_avg": None if hist_daily is None else round(hist_daily, 2),
        "confidence": confidence,
    }


async def overview_for(db: AppDB, month: str, today: date) -> dict[str, Any]:
    first = month_shift(month, -(SERIES_MONTHS - 1))
    expense_rows = await db.fetch(
        "SELECT id, date, merchant, total, currency, category FROM app_expenses WHERE date >= ? AND date <= ?",
        [f"{first}-01", f"{month}-31"],
    )
    income_rows = await db.fetch(
        "SELECT * FROM app_incomes WHERE (recurring = 0 AND date >= ? AND date <= ?) OR (recurring = 1 AND date <= ?)"
        " ORDER BY date DESC",
        [f"{first}-01", f"{month}-31", f"{month}-31"],
    )
    recurring_rows = await db.fetch("SELECT * FROM app_recurring WHERE start_month <= ? ORDER BY name", [month])
    account_rows = await db.fetch("SELECT * FROM app_accounts ORDER BY name")
    budget_rows = await db.fetch("SELECT * FROM app_budgets ORDER BY category")
    rates = await get_rates(db)
    from hub_api.money_intel import subscription_overview  # weekly/yearly subscriptions (that module imports this one)

    subs, charges = await subscription_overview(db, month)

    return build_overview(
        month,
        today,
        rates,
        expenses=[
            {"id": r.get("id"), "date": r["date"], "merchant": r.get("merchant") or "",
             "total": float(r.get("total") or 0), "currency": r.get("currency") or BASE,
             "category": r.get("category") or "other"}
            for r in expense_rows
        ],
        incomes=[_income_out(r) for r in income_rows],
        recurring=[_recurring_out(r) for r in recurring_rows],
        accounts=[_account_out(r) for r in account_rows],
        budgets=[_budget_out(r) for r in budget_rows],
        extra_recurring=charges,
        subscriptions=subs,
    )


# --------------------------------------------------------------------------- voice

_PAST_OFFSETS = {
    "yesterday": 1, "hier": 1, "lbare7": 1, "lbareh": 1, "l-bare7": 1, "lbar7": 1, "elbare7": 1,
    "day before yesterday": 2, "the day before yesterday": 2, "avant-hier": 2, "avant hier": 2,
    "walbare7": 2, "wlbare7": 2, "lwlbare7": 2,
}


def resolve_spent_date(value: Any, today: date) -> str:
    """A spending/income date: ISO or relative ("hier", "lbare7", "lundi"), never in the future; else today."""
    text = str(value or "").strip().lower().rstrip(".!")
    rest, hhmm, _, _ = split_time(text)
    if hhmm and rest:  # "hier soir", "lbare7 f 8": the day part
        text = rest
    if text in _PAST_OFFSETS:
        return (today - timedelta(days=_PAST_OFFSETS[text])).isoformat()
    for prefix in ("last ", "dernier ", "nhar "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    for suffix in (" dernier", " derniere", " dernière", " li fat", " last"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if text in _WEEKDAYS:
        return (today - timedelta(days=(today.weekday() - _WEEKDAYS[text]) % 7)).isoformat()
    resolved = resolve_due(value, today)
    if resolved is None or resolved > today.isoformat():
        return today.isoformat()
    return resolved


def parse_money_entries(
    data: dict[str, Any] | None, today: date, kind: Literal["auto", "expense", "income"] = "auto"
) -> list[dict[str, Any]]:
    """Validated money entries from the LLM's ``{"entries": [...]}``; entries without a positive amount are dropped.

    Debt entries (``lend``, ``borrow``, ``repaid``, ``repay``) keep their type whatever ``kind`` says and need a
    ``person``; a ``split`` needs ``people`` (the others sharing it, at most :data:`MAX_SPLIT_PEOPLE`) and is a plain
    expense without them (or when ``kind`` is ``income``); the others become ``expense``/``income`` (forced by
    ``kind`` unless it is ``auto``).
    """
    from hub_api.routers.debts import DEBT_ENTRY_TYPES  # debts imports this module

    out: list[dict[str, Any]] = []
    raw = (data or {}).get("entries")
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        amount = _number(e.get("amount"))
        if amount is None or amount <= 0:
            continue
        typ = str(e.get("type") or "expense").strip().lower()
        if typ == "split":
            people = _split_people(e.get("people"))
            if people and kind != "income":
                merchant = e.get("merchant") if isinstance(e.get("merchant"), str) else ""
                category = str(e.get("category") or "").strip().lower()
                note = e.get("note")
                out.append(
                    {
                        "type": "split",
                        "amount": round(amount, 2),
                        "currency": normalise_currency(e.get("currency")) or BASE,
                        "merchant": merchant.strip()[:200],
                        "people": people,
                        "category": category if category in CATEGORIES else "other",
                        "date": resolve_spent_date(e.get("date"), today),
                        "note": note.strip()[:2000] if isinstance(note, str) and note.strip() else None,
                    }
                )
                continue
            typ = "expense"
        if typ in DEBT_ENTRY_TYPES:
            person = next((v.strip() for v in (e.get("person"), e.get("merchant")) if isinstance(v, str) and v.strip()), "")
            if not person:
                continue
            note = e.get("note")
            out.append(
                {
                    "type": typ,
                    "amount": round(amount, 2),
                    "currency": normalise_currency(e.get("currency")) or BASE,
                    "person": person[:100],
                    "merchant": person[:100],
                    "category": "other",
                    "date": resolve_spent_date(e.get("date"), today),
                    "note": note.strip()[:2000] if isinstance(note, str) and note.strip() else None,
                }
            )
            continue
        typ = kind if kind != "auto" else ("income" if typ == "income" else "expense")
        category = str(e.get("category") or "").strip().lower()
        allowed = INCOME_CATEGORIES if typ == "income" else CATEGORIES
        merchant = e.get("merchant") if isinstance(e.get("merchant"), str) else ""
        note = e.get("note")
        out.append(
            {
                "type": typ,
                "amount": round(amount, 2),
                "currency": normalise_currency(e.get("currency")) or BASE,
                "merchant": merchant.strip()[:200],
                "category": category if category in allowed else "other",
                "date": resolve_spent_date(e.get("date"), today),
                "note": note.strip()[:5000] if isinstance(note, str) and note.strip() else None,
            }
        )
    return out[:50]


MAX_SPLIT_PEOPLE = 10
_SPEAKER = {"moi", "me", "i", "ana", "je", "myself"}


def _split_people(raw: Any) -> list[str]:
    """The other people of a voice split: names, no duplicates, never the speaker ("moi", "me")."""
    out: dict[str, str] = {}
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, str) or not n.strip() or n.strip().lower() in _SPEAKER:
            continue
        out.setdefault(n.strip().lower(), n.strip()[:60])
    people = list(out.values())
    return people if len(people) <= MAX_SPLIT_PEOPLE else []


async def create_entries(db: AppDB, entries: list[dict[str, Any]], *, audio_key: str | None) -> dict[str, list[dict[str, Any]]]:
    """Store parsed entries: expenses through the expenses insert path (``source = 'voice'``), incomes here, loans and
    repayments in :mod:`hub_api.routers.debts` (``debts`` lists one outcome per debt entry)."""
    from hub_api.routers.debts import DEBT_ENTRY_TYPES, record_voice_debt  # debts imports this module

    await ensure_schema(db)
    created: dict[str, list[dict[str, Any]]] = {"expenses": [], "incomes": [], "debts": []}
    for e in entries:
        if e["type"] == "split":
            from hub_api.routers.splits import record_voice_split  # splits imports debts, which imports this module

            created["debts"].append(await record_voice_split(db, e, audio_key=audio_key))
        elif e["type"] in DEBT_ENTRY_TYPES:
            created["debts"].append(await record_voice_debt(db, e, audio_key=audio_key))
        elif e["type"] == "income":
            created["incomes"].append(
                await insert_income(
                    db,
                    {"date": e["date"], "amount": e["amount"], "currency": e["currency"], "source": e["merchant"],
                     "category": e["category"], "note": e["note"], "origin": "voice", "audio_key": audio_key},
                )
            )
        else:
            created["expenses"].append(
                await insert_expense(
                    db,
                    {"date": e["date"], "merchant": e["merchant"], "total": e["amount"], "currency": e["currency"],
                     "category": e["category"], "note": e["note"], "source": "voice", "source_ref": audio_key},
                )
            )
    return created


def _money_prompt(today: date, kind: str) -> str:
    past = ", ".join(f"{(today - timedelta(days=i)):%A} = {(today - timedelta(days=i)).isoformat()}" for i in range(1, 8))
    hint = {
        "expense": "The speaker says every amount is an expense.\n",
        "income": "The speaker says every amount is an income.\n",
    }.get(kind, "")
    return (
        "You extract money movements from a voice memo transcript (Moroccan user: French, English, Darija or a mix).\n"
        f"Today is {today:%A} {today.isoformat()} (Africa/Casablanca). Past days: {past}.\n"
        f"{hint}"
        "Answer with ONE JSON object only, no prose, no code fences:\n"
        '{"entries": [{"type": "expense|income|lend|borrow|repaid|repay|split", "amount": number, '
        '"currency": "ISO 4217 code (dh, dhs, dirham, drahem = MAD; euro = EUR; dollar = USD) or null", '
        '"merchant": "for an expense who was paid (shop, café, person); for an income who paid (employer, client) or '
        'a label like Salaire", '
        f'"category": "expense: one of {"|".join(CATEGORIES)}; income: one of {"|".join(INCOME_CATEGORIES)}", '
        '"date": "YYYY-MM-DD or null", "note": "short detail or null"}]}\n'
        "Loans between the speaker and a person use the type lend, borrow, repaid or repay with an extra "
        '"person": "the person\'s name" (merchant = the same name, category null): lend = the speaker lent money '
        "(j'ai prêté 200 dh à Ayoub, sl7t Ayoub 200); borrow = the speaker borrowed (j'ai emprunté 500 à Karim, "
        "tsellft men Karim); repaid = someone paid the speaker back (Ayoub m'a rendu 100, Ayoub rjje3 lia 100); "
        "repay = the speaker paid someone back (j'ai rendu 200 à Karim). A loan is never an expense or an income.\n"
        "A bill the speaker paid alone and explicitly shares equally between themselves and named people (j'ai payé "
        "300 dh le dîner avec Ayoub et Sara, chacun sa part; on a partagé) uses the type split with an extra "
        '"people": ["Ayoub", "Sara"] (the others, never the speaker; merchant = what it was). Only when the speaker '
        "paid and says the cost is shared; otherwise it is a plain expense.\n"
        "Rules: one entry per amount; only money actually spent or received, not plans or prices mentioned in passing; "
        "salary, payment received, refund or gift = income; convert relative dates (hier, yesterday, lbare7, lundi "
        "dernier) to absolute dates, never in the future; date null when no day is said; never invent amounts; "
        'an empty list when no amount is said. Example: "j\'ai payé 45 dirhams de taxi et 30 dh un café chez Bab Café" -> '
        '[{"type": "expense", "amount": 45, "currency": "MAD", "merchant": "Taxi", "category": "transport", ...}, '
        '{"type": "expense", "amount": 30, "currency": "MAD", "merchant": "Bab Café", "category": "food", ...}].'
    )


async def _extract_money(
    state: HubState, transcript: str, today: date, kind: Literal["auto", "expense", "income"], language: str | None
) -> list[dict[str, Any]]:
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_money_prompt(today, kind)),
            ChatMessage(role="user", content=f"Transcript{f' (language: {language})' if language else ''}:\n{transcript}"),
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    resp = await state.router.chat(request)
    data = extract_json(resp.content)
    if data is None:
        log.warning("voice money extraction returned no JSON (routed via %s)", resp.routed_via)
    return parse_money_entries(data, today, kind)


# --------------------------------------------------------------------------- routes: overview, fx, voice


@router.get("/overview")
async def overview(month: str | None = Query(None, pattern=_MONTH_RE), db: AppDB = Depends(_db)) -> dict[str, Any]:
    today = local_today()
    return await overview_for(db, month or today.strftime("%Y-%m"), today)


@router.get("/fx")
async def fx(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rates = await get_rates(db)
    return {**rates.public(), "to_mad": rates.to_mad}


@router.post("/voice", status_code=status.HTTP_201_CREATED)
async def voice_money(
    file: UploadFile = File(...),
    kind: Literal["auto", "expense", "income"] = Form("auto"),
    language: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """``Idempotency-Key`` (header or form field): a replayed upload answers the first result again."""

    async def run() -> dict[str, Any]:
        today = local_today()
        key, transcript = await store_and_transcribe(state, file, today, language)
        entries = await _extract_money(state, transcript, today, kind, language)
        created = await create_entries(db, entries, audio_key=key)
        return {"transcript": transcript, "audio_key": key, **created}

    idem = idempotency_key or (request.headers.get("idempotency-key") if request is not None else None)
    return await idempotency.run_once(db, scope="money.voice", key=idem, run=run, response=response)


# --------------------------------------------------------------------------- routes: incomes


@router.get("/incomes")
async def list_incomes(month: str | None = Query(None, pattern=_MONTH_RE), db: AppDB = Depends(_db)) -> dict[str, Any]:
    """One-off incomes of ``month`` plus the monthly incomes active in it (all incomes without ``month``)."""
    if not month:
        rows = await db.fetch("SELECT * FROM app_incomes ORDER BY date DESC, created_at DESC")
        return {"incomes": [_income_out(r) for r in rows]}
    rows = await db.fetch(
        "SELECT * FROM app_incomes WHERE (recurring = 0 AND date >= ? AND date <= ?) OR (recurring = 1 AND date <= ?)"
        " ORDER BY date DESC, created_at DESC",
        [f"{month}-01", f"{month}-31", f"{month}-31"],
    )
    items = [_income_out(r) for r in rows]
    return {"incomes": [i for i in items if not i["recurring"] or active_in(i["date"][:7], i["end_month"], month)]}


@router.post("/incomes", status_code=status.HTTP_201_CREATED)
async def create_income(
    body: IncomeCreate,
    db: AppDB = Depends(_db),
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI; None when called directly (MCP)
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    fields = body.model_dump()
    fields["date"] = fields["date"] or local_today().isoformat()
    _check_range(fields["date"][:7], fields["end_month"])
    fields["origin"] = "manual"

    async def fetch(income_id: str) -> dict[str, Any] | None:
        row = await db.fetch_one("SELECT * FROM app_incomes WHERE id = ?", [income_id])
        return None if row is None else _income_out(row)

    async def insert(income_id: str) -> dict[str, Any]:
        return await insert_income(db, {**fields, "id": income_id})

    key = idempotency.header_key(request) if request is not None else None
    return await idempotency.create_once(
        db, scope="incomes", client_id=body.id, key=key, fetch=fetch, insert=insert, response=response
    )


@router.patch("/incomes/{income_id}")
async def patch_income(income_id: str, body: IncomePatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    current = _income_out(await _get(db, "app_incomes", "id", income_id, "income"))
    changes = _changes(body, ("note", "end_month"))
    _check_range(str(changes.get("date", current["date"]))[:7], changes.get("end_month", current["end_month"]))
    await _update(db, "app_incomes", income_id, changes)
    return _income_out(await _get(db, "app_incomes", "id", income_id, "income"))


@router.delete("/incomes/{income_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_income(income_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, "app_incomes", "id", income_id, "income")
    await db.execute("DELETE FROM app_incomes WHERE id = ?", [income_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routes: recurring expenses


@router.get("/recurring")
async def list_recurring(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_recurring ORDER BY day, name")
    return {"recurring": [_recurring_out(r) for r in rows]}


@router.post("/recurring", status_code=status.HTTP_201_CREATED)
async def create_recurring(body: RecurringCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    start = body.start_month or local_today().strftime("%Y-%m")
    _check_range(start, body.end_month)
    row = {
        "id": new_id(),
        "name": body.name.strip(),
        "amount": float(body.amount),
        "currency": body.currency,
        "category": body.category,
        "day": body.day,
        "start_month": start,
        "end_month": body.end_month,
        "note": body.note,
        "created_at": now_iso(),
    }
    await _insert(db, "app_recurring", row)
    return _recurring_out(row)


@router.patch("/recurring/{item_id}")
async def patch_recurring(item_id: str, body: RecurringPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    current = _recurring_out(await _get(db, "app_recurring", "id", item_id, "recurring expense"))
    changes = _changes(body, ("note", "end_month"))
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    _check_range(changes.get("start_month", current["start_month"]), changes.get("end_month", current["end_month"]))
    await _update(db, "app_recurring", item_id, changes)
    return _recurring_out(await _get(db, "app_recurring", "id", item_id, "recurring expense"))


@router.delete("/recurring/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recurring(item_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, "app_recurring", "id", item_id, "recurring expense")
    await db.execute("DELETE FROM app_recurring WHERE id = ?", [item_id])
    from hub_api.money_intel import SUBS_TABLE  # that module imports this one

    try:  # an accepted subscription behind this charge: forget it, so the merchant can be suggested again
        await db.execute(f"DELETE FROM {SUBS_TABLE} WHERE recurring_id = ?", [item_id])
    except Exception as exc:  # noqa: BLE001 - no subscriptions table yet
        log.debug("no subscription to forget for %s: %r", item_id, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routes: accounts


@router.get("/accounts")
async def list_accounts(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_accounts ORDER BY name")
    rates = await get_rates(db)
    items = [_account_out(r) for r in rows]
    total = round(sum(rates.convert(a["balance"], a["currency"]) or 0.0 for a in items), 2)
    return {"accounts": items, "total": total, "currency": BASE, "fx": rates.public()}


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    now = now_iso()
    row = {
        "id": new_id(),
        "name": body.name.strip(),
        "kind": body.kind,
        "balance": round(float(body.balance), 2),
        "currency": body.currency,
        "note": body.note,
        "updated_at": now,
        "created_at": now,
    }
    await _insert(db, "app_accounts", row)
    return _account_out(row)


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: str, body: AccountPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    await _get(db, "app_accounts", "id", account_id, "account")
    changes = _changes(body, ("note",))
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    if changes:
        changes["updated_at"] = now_iso()
    await _update(db, "app_accounts", account_id, changes)
    return _account_out(await _get(db, "app_accounts", "id", account_id, "account"))


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, "app_accounts", "id", account_id, "account")
    await db.execute("DELETE FROM app_accounts WHERE id = ?", [account_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def set_account_balance(db: AppDB, name: str, balance: float, currency: str | None, kind: str | None) -> dict[str, Any]:
    """Upsert by name (case-insensitive): used by the MCP ``account_set_balance`` tool."""
    await ensure_schema(db)
    row = await db.fetch_one("SELECT * FROM app_accounts WHERE lower(name) = lower(?)", [name.strip()])
    if row is None:
        body = AccountCreate(name=name, balance=balance, currency=currency or BASE, kind=kind or "bank")  # type: ignore[arg-type]
        return await create_account(body, db=db)
    patch = AccountPatch.model_validate(
        {"balance": balance, **({"currency": currency} if currency else {}), **({"kind": kind} if kind else {})}
    )
    return await patch_account(row["id"], patch, db=db)


# --------------------------------------------------------------------------- routes: budgets


@router.get("/budgets")
async def list_budgets(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_budgets ORDER BY category")
    return {"budgets": [_budget_out(r) for r in rows]}


@router.put("/budgets/{category}")
async def set_budget(category: Category, body: BudgetSet, db: AppDB = Depends(_db)) -> dict[str, Any]:
    row = {"category": category, "amount": round(float(body.amount), 2), "currency": body.currency, "updated_at": now_iso()}
    await db.execute(
        "INSERT INTO app_budgets (category, amount, currency, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(category) DO UPDATE SET amount = excluded.amount, currency = excluded.currency,"
        " updated_at = excluded.updated_at",
        [row["category"], row["amount"], row["currency"], row["updated_at"]],
    )
    return _budget_out(row)


@router.delete("/budgets/{category}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(category: Category, db: AppDB = Depends(_db)) -> Response:
    await _get(db, "app_budgets", "category", category, "budget")
    await db.execute("DELETE FROM app_budgets WHERE category = ?", [category])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
