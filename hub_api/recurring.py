"""Optional, table-guarded read of recurring incomes/expenses (the money app may or may not have them).

The calendar feed and the month-end push use recurring money items **only if** a table named like
``app_money_recurring%`` exists in the apps database; its exact columns are not fixed here, so each row
is read tolerantly (``name|label|title|merchant|description``, ``amount|total``, ``currency``,
``kind|type|direction`` = income/expense, ``frequency|cadence|period|interval`` = weekly/monthly/yearly,
``next_date|next_due|start_date|start|date|anchor_date`` and/or ``day_of_month|day``). Rows that cannot be
understood are skipped; any database error yields an empty list.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from hub_api.appdata import AppDB

__all__ = ["find_tables", "recurring_items", "monthly_equivalent"]

log = logging.getLogger("omnifree.hub.recurring")

_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_LABEL = ("name", "label", "title", "merchant", "description", "source")
_AMOUNT = ("amount", "total", "value")
_KIND = ("kind", "type", "direction", "flow")
_FREQ = ("frequency", "cadence", "period", "interval", "recurrence", "every")
_START = ("next_date", "next_due", "next_on", "start_date", "start", "date", "anchor_date", "first_date")
_DAY = ("day_of_month", "day", "dom")
_FREQS = {
    "daily": "DAILY", "day": "DAILY", "weekly": "WEEKLY", "week": "WEEKLY", "monthly": "MONTHLY", "month": "MONTHLY",
    "yearly": "YEARLY", "year": "YEARLY", "annual": "YEARLY", "annually": "YEARLY",
}


async def find_tables(db: AppDB, like: str) -> list[str]:
    """Names of the tables matching the SQL LIKE pattern ``like`` (empty on any error)."""
    try:
        rows = await db.fetch("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?", [like])
    except Exception as exc:  # noqa: BLE001 - optional feature, never fatal
        log.debug("table lookup %s failed: %r", like, exc)
        return []
    return sorted(str(r["name"]) for r in rows if _TABLE_RE.match(str(r.get("name") or "")))


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _normalise(row: dict[str, Any], table: str) -> dict[str, Any] | None:
    if any(str(row.get(k)).lower() in ("0", "false") for k in ("active", "enabled") if k in row):
        return None
    label = _first(row, _LABEL)
    try:
        amount = float(_first(row, _AMOUNT))
    except (TypeError, ValueError):
        return None
    kind_raw = str(_first(row, _KIND) or "").lower()
    if kind_raw.startswith(("inc", "in", "rev", "credit", "salary")):
        kind = "income"
    elif kind_raw.startswith(("exp", "out", "debit", "bill", "sub")) or not kind_raw:
        kind = "income" if "income" in table else "expense"
    else:
        kind = "expense"
    freq = _FREQS.get(str(_first(row, _FREQ) or "monthly").strip().lower(), "MONTHLY")
    start = _date(_first(row, _START))
    day = None
    try:
        day_raw = _first(row, _DAY)
        day = int(day_raw) if day_raw is not None else None
    except (TypeError, ValueError):
        day = None
    if day is not None and not 1 <= day <= 31:
        day = None
    return {
        "id": str(row.get("id") or f"{label}-{amount}"),
        "label": str(label or ("Revenu" if kind == "income" else "Dépense")).strip()[:200],
        "amount": abs(amount),
        "currency": str(row.get("currency") or "MAD").upper()[:8],
        "kind": kind,
        "frequency": freq,
        "start": start,
        "day": day,
        "table": table,
    }


async def recurring_items(db: AppDB) -> list[dict[str, Any]]:
    """Normalised recurring incomes/expenses, or ``[]`` when the money tables do not exist."""
    out: list[dict[str, Any]] = []
    for table in await find_tables(db, "app_money_recurring%"):
        try:
            rows = await db.fetch(f"SELECT * FROM {table}", [], limit=1000)  # noqa: S608 - name checked by _TABLE_RE
        except Exception as exc:  # noqa: BLE001
            log.info("reading %s failed: %r", table, exc)
            continue
        out.extend(item for row in rows if (item := _normalise(row, table)) is not None)
    return out


def monthly_equivalent(item: dict[str, Any]) -> float:
    factor = {"DAILY": 365 / 12, "WEEKLY": 52 / 12, "MONTHLY": 1.0, "YEARLY": 1 / 12}.get(item["frequency"], 1.0)
    return item["amount"] * factor
