"""Money intelligence: detected subscriptions, bill reminders, live budget alerts and savings goals.

* **Subscriptions** (:func:`detect_subscriptions`, pure): expenses from every source (SMS, email, voice,
  receipt, manual) grouped by :func:`hub_api.money_match.merchant_key`; a group whose amounts stay within
  15 % and whose dates repeat weekly, monthly or yearly (2+ times, still active) becomes a suggestion with a
  confidence, its cadence, average amount and next expected date. Accepting a monthly one creates an
  ``app_recurring`` charge (the Money page model); weekly and yearly ones, which that monthly model cannot
  hold, stay in ``app_money_recurring_subs`` with ``active = 1``. Dismissals are remembered there too.
  The table name and its ``name/amount/currency/frequency/anchor_date/active`` columns follow the
  ``app_money_recurring*`` convention of :mod:`hub_api.recurring`, so the calendar feed and the month-end push
  pick the active weekly/yearly subscriptions up without knowing about them.
* **Bills** (:func:`run_bills`, ``POST /notify/run?kind=bills``, daily): one push per charge due in the next
  2 days (monthly charges and active weekly/yearly subscriptions), deduplicated per charge and date.
* **Live budget alerts** (:func:`budget_alert_after_insert`): called by the expenses insert path every expense
  goes through; pushes once when the category's monthly budget crosses 80 % and once at 100 % (deduplicated
  per category, month and threshold). Bounded by short timeouts, cached FX only, never raises.
* **Goals**: ``app_goals`` (target, optional deadline, saved so far, optional linked account, emoji/colour)
  and ``app_goal_contributions`` (signed amounts); :func:`goal_out` adds progress and the monthly saving needed.

Push texts are in French (the owner's language), like :mod:`hub_api.routers.notify`.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import statistics
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from hub_api import notifier, webpush
from hub_api.appdata import AppDB, local_today
from hub_api.money_match import KNOWN_SUBSCRIPTIONS, amount_close, merchant_key
from hub_api.routers import money
from hub_api.state import HubState

__all__ = [
    "SCHEMA", "ensure_schema", "cached_rates", "detect_subscriptions", "next_after", "occurrences_between",
    "subscription_charges", "subscription_overview", "upcoming_bills", "run_bills", "compose_bill", "budget_alert_after_insert",
    "budget_level", "goal_out", "months_until", "orphaned",
]

log = logging.getLogger("omnifree.hub.money_intel")

SUBS_TABLE = "app_money_recurring_subs"
SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {SUBS_TABLE} ("
    " id TEXT PRIMARY KEY, merchant_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, status TEXT NOT NULL,"
    " frequency TEXT NOT NULL DEFAULT 'monthly', amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'MAD',"
    " category TEXT NOT NULL DEFAULT 'bills', anchor_date TEXT, active INTEGER NOT NULL DEFAULT 0,"
    " recurring_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_goals ("
    " id TEXT PRIMARY KEY, name TEXT NOT NULL, target REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'MAD',"
    " deadline TEXT, saved REAL NOT NULL DEFAULT 0, account_id TEXT, emoji TEXT, color TEXT, note TEXT,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_goal_contributions ("
    " id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, amount REAL NOT NULL, note TEXT, date TEXT NOT NULL,"
    " created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_goal_contributions_goal ON app_goal_contributions (goal_id, date)",
)

# cadence -> (period in days, tolerance in days, no charge for this long = cancelled)
CADENCES: dict[str, tuple[float, float, int]] = {
    "weekly": (7.0, 2.0, 21),
    "monthly": (30.44, 5.0, 50),
    "yearly": (365.25, 20.0, 400),
}
LOOKBACK_DAYS = 800  # two years and a bit: a yearly charge needs two occurrences
MIN_CONFIDENCE = 0.5
BILLS_AHEAD_DAYS = 2
MAX_BILLS = 10  # per run: stays well inside the 60 s serverless limit
ALERT_TIMEOUT_S = 4.0  # the live budget checks (budget, spend, prefs) never hold an expense insert longer than this
ALERT_DELIVER_TIMEOUT_S = 8.0  # then the push itself; on timeout its dedupe claim is released for the next expense
ALERT_LEVELS = (100, 80)  # percent, highest first

_DISPLAY = {"chatgpt": "ChatGPT", "icloud": "iCloud", "github": "GitHub", "linkedin": "LinkedIn", "onee": "ONEE",
            "osn": "OSN", "ovh": "OVH", "bein": "beIN"}
_MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre")
_WEEKDAYS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_CATEGORY_FR = {
    "food": "restaurants", "groceries": "courses", "transport": "transport", "housing": "logement",
    "bills": "factures", "health": "santé", "shopping": "shopping", "entertainment": "loisirs",
    "education": "éducation", "travel": "voyages", "other": "autre",
}


async def ensure_schema(db: AppDB) -> AppDB:
    await money.ensure_schema(db)
    await db.ensure("money_intel", SCHEMA)
    return db


async def cached_rates(db: AppDB) -> money.Rates:
    """Rates from the D1 cache whatever their age, else the static fallback: never a network call."""
    try:
        row = await db.fetch_one("SELECT rates, fetched_at FROM app_fx_cache WHERE base = ?", [money.BASE])
        value = json.loads(row["rates"]) if row else None
        if isinstance(value, dict) and value:
            return money.Rates({str(k): float(v) for k, v in value.items()}, "cache", row["fetched_at"])  # type: ignore[index]
    except Exception as exc:  # noqa: BLE001 - fall back below
        log.debug("cached FX unavailable: %r", exc)
    return money.Rates(dict(money.FALLBACK_TO_MAD), "fallback", None)


# --------------------------------------------------------------------------- dates


def _add_months(d: date, n: int, day: int | None = None) -> date:
    idx = d.year * 12 + d.month - 1 + n
    y, m = idx // 12, idx % 12 + 1
    return date(y, m, min(day or d.day, calendar.monthrange(y, m)[1]))


def _add_years(d: date, n: int) -> date:
    y = d.year + n
    return date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))


def next_after(last: date, cadence: str, today: date, day: int | None = None) -> date:
    """The first charge on or after ``today`` following ``last`` (monthly ones on ``day``, clamped)."""
    k = 1
    while True:
        if cadence == "weekly":
            nxt = last + timedelta(days=7 * k)
        elif cadence == "yearly":
            nxt = _add_years(last, k)
        else:
            nxt = _add_months(last, k, day)
        if nxt >= today or k > 1000:
            return nxt
        k += 1


def occurrences_between(anchor: date, cadence: str, start: date, end: date) -> list[date]:
    """Charges of a weekly/monthly/yearly series anchored on ``anchor`` that fall in [start, end] (none before anchor)."""
    out: list[date] = []
    k = 0
    while k < 2000:
        if cadence == "weekly":
            d = anchor + timedelta(days=7 * k)
        elif cadence == "yearly":
            d = _add_years(anchor, k)
        else:
            d = _add_months(anchor, k)
        if d > end:
            break
        if d >= start:
            out.append(d)
        k += 1
    return out


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- subscription detection


def _display_name(key: str, merchants: list[str]) -> str:
    if key in KNOWN_SUBSCRIPTIONS:
        return _DISPLAY.get(key) or key.title()
    return (merchants[-1] or key).strip()[:200]


def _cluster(points: list[tuple[date, float, dict[str, Any]]]) -> list[tuple[date, float, dict[str, Any]]]:
    """The largest set of charges within 15 % of one of them (one per day), oldest first."""
    best: list[tuple[date, float, dict[str, Any]]] = []
    for _, center, _ in points:
        members = [p for p in points if amount_close(p[1], center)]
        if len(members) > len(best) or (len(members) == len(best) and members and best and members[-1][0] > best[-1][0]):
            best = members
    by_day: dict[date, tuple[date, float, dict[str, Any]]] = {}
    mean = statistics.fmean(p[1] for p in best) if best else 0.0
    for p in best:
        kept = by_day.get(p[0])
        if kept is None or abs(p[1] - mean) < abs(kept[1] - mean):
            by_day[p[0]] = p
    return sorted(by_day.values(), key=lambda p: p[0])


def _cadence(intervals: list[int]) -> str | None:
    med = statistics.median(intervals)
    if 5 <= med <= 9:
        return "weekly"
    if 25 <= med <= 35:
        return "monthly"
    if 330 <= med <= 400:
        return "yearly"
    return None


def detect_subscriptions(
    expenses: list[dict[str, Any]],
    today: date,
    to_mad: Callable[[float, str], float | None],
    *,
    skip: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Recurring merchants among ``expenses`` (``expense_out``-like dicts), most confident first.

    ``to_mad(amount, currency)`` compares amounts across currencies; ``skip`` holds merchant keys already
    tracked or dismissed.
    """
    groups: dict[str, list[tuple[date, float, dict[str, Any]]]] = {}
    for e in expenses:
        key = merchant_key(e.get("merchant"))
        day = _parse_date(e.get("date"))
        total = float(e.get("total") or 0)
        if not key or key in skip or day is None or day > today or total <= 0:
            continue
        value = to_mad(total, e.get("currency") or money.BASE)
        if value is None:
            continue
        groups.setdefault(key, []).append((day, value, e))

    out: list[dict[str, Any]] = []
    for key, points in groups.items():
        if len(points) < 2:
            continue
        members = _cluster(points)
        if len(members) < 2:
            continue
        intervals = [(b[0] - a[0]).days for a, b in zip(members, members[1:], strict=False)]
        cadence = _cadence(intervals)
        if cadence is None:
            continue
        period, tolerance, stale = CADENCES[cadence]
        regular = sum(1 for iv in intervals if abs(iv - period) <= tolerance) / len(intervals)
        last = members[-1][0]
        if regular < 0.5 or (today - last).days > stale:
            continue
        values = [p[1] for p in members]
        mean_mad = statistics.fmean(values)
        cv = statistics.pstdev(values) / mean_mad if mean_mad else 1.0
        rows = [p[2] for p in members]
        currency = Counter(r.get("currency") or money.BASE for r in rows).most_common(1)[0][0]
        same = [float(r["total"]) for r in rows if (r.get("currency") or money.BASE) == currency]
        avg = statistics.fmean(same) if len(same) == len(rows) else mean_mad
        if len(same) != len(rows):
            currency = money.BASE
        day = Counter(p[0].day for p in members).most_common(1)[0][0] if cadence == "monthly" else None
        n = len(members)
        known = key in KNOWN_SUBSCRIPTIONS
        confidence = 0.4 * min(1.0, (n - 1) / 3) + 0.3 * regular + 0.2 * max(0.0, 1 - cv / 0.15) + (0.1 if known else 0.0)
        if known:
            confidence = max(confidence, 0.6)
        confidence = round(min(1.0, confidence), 2)
        if confidence < MIN_CONFIDENCE:
            continue
        category = Counter(r.get("category") or "other" for r in rows).most_common(1)[0][0]
        out.append({
            "key": key,
            "name": _display_name(key, [str(r.get("merchant") or "") for r in rows]),
            "cadence": cadence,
            "avg_amount": round(avg, 2),
            "currency": currency,
            "avg_amount_mad": round(mean_mad, 2),
            "monthly_mad": round(mean_mad * {"weekly": 52 / 12, "monthly": 1.0, "yearly": 1 / 12}[cadence], 2),
            "occurrences": n,
            "first_date": members[0][0].isoformat(),
            "last_date": last.isoformat(),
            "next_date": next_after(last, cadence, today, day).isoformat(),
            "day": day,
            "category": category if category in money.CATEGORIES else "other",
            "confidence": confidence,
            "level": "high" if confidence >= 0.75 else "medium" if confidence >= 0.6 else "low",
            "sources": sorted({str(r.get("source") or "manual") for r in rows}),
            "expense_ids": [str(r.get("id")) for r in rows if r.get("id")][-6:],
        })
    out.sort(key=lambda s: (-s["confidence"], -s["avg_amount_mad"], s["key"]))
    return out


async def tracked_keys(db: AppDB, today: date) -> set[str]:
    """Merchant keys not to suggest: accepted or dismissed ones, and monthly charges still running.

    An accepted monthly subscription whose charge was deleted since no longer counts (see :func:`orphaned`).
    """
    recurring = await db.fetch("SELECT id, name, end_month FROM app_recurring")
    ids = {str(r["id"]) for r in recurring}
    keys = {str(r["merchant_key"]) for r in await db.fetch(f"SELECT merchant_key, status, recurring_id FROM {SUBS_TABLE}")
            if not orphaned(r, ids)}
    month = today.strftime("%Y-%m")
    for r in recurring:
        if not r.get("end_month") or r["end_month"] >= month:
            if key := merchant_key(r.get("name")):
                keys.add(key)
    return keys


def orphaned(row: dict[str, Any], recurring_ids: set[str]) -> bool:
    """An accepted subscription row tracked through a monthly charge that no longer exists."""
    rid = row.get("recurring_id")
    return row.get("status") == "accepted" and bool(rid) and str(rid) not in recurring_ids


async def suggestions(db: AppDB, today: date, *, include_tracked: bool = False) -> list[dict[str, Any]]:
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = await db.fetch(
        "SELECT id, date, merchant, total, currency, category, source FROM app_expenses WHERE date >= ? ORDER BY date",
        [since],
    )
    rates = await cached_rates(db)
    skip = set() if include_tracked else await tracked_keys(db, today)
    return detect_subscriptions(rows, today, rates.convert, skip=skip)


def subscription_out(row: dict[str, Any], today: date) -> dict[str, Any]:
    anchor = _parse_date(row.get("anchor_date"))
    nxt = None
    if anchor and row.get("active"):
        nxt = (anchor if anchor >= today else next_after(anchor, row["frequency"], today)).isoformat()
    return {
        "id": row["id"],
        "key": row["merchant_key"],
        "name": row["name"],
        "status": row["status"],
        "cadence": row.get("frequency") or "monthly",
        "amount": float(row.get("amount") or 0),
        "currency": row.get("currency") or money.BASE,
        "category": row.get("category") or "bills",
        "anchor_date": row.get("anchor_date"),
        "next_date": nxt,
        "active": bool(row.get("active")),
        "recurring_id": row.get("recurring_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _active_subs(db: AppDB) -> list[dict[str, Any]]:
    await db.ensure("money_intel", SCHEMA)
    return await db.fetch(f"SELECT * FROM {SUBS_TABLE} WHERE active = 1 AND status = 'accepted'")


async def subscription_charges(db: AppDB, month: str) -> list[dict[str, Any]]:
    """Charges of the active weekly/yearly subscriptions in ``month`` (``id, key, name, amount, currency, category,
    on, frequency``)."""
    return (await subscription_overview(db, month))[1]


async def subscription_overview(db: AppDB, month: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(active weekly/yearly subscriptions as id/key/name/amount/currency/frequency, their charges in month)``."""
    try:
        rows = await _active_subs(db)
    except Exception as exc:  # noqa: BLE001 - the overview must not fail on this optional part
        log.warning("subscriptions unavailable for the overview: %r", exc)
        return [], []
    subs = [{"id": r["id"], "key": r["merchant_key"], "name": r["name"], "amount": float(r["amount"] or 0),
             "currency": r["currency"], "frequency": r["frequency"]} for r in rows]
    start = date(int(month[:4]), int(month[5:7]), 1)
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    out = []
    for r in rows:
        anchor = _parse_date(r.get("anchor_date"))
        if anchor is None:
            continue
        for d in occurrences_between(anchor, r["frequency"], start, end):
            out.append({"id": r["id"], "key": r["merchant_key"], "name": r["name"], "amount": float(r["amount"] or 0),
                        "currency": r["currency"], "category": r.get("category") or "bills", "on": d.isoformat(),
                        "frequency": r["frequency"]})
    return subs, out


# --------------------------------------------------------------------------- bills


async def upcoming_bills(db: AppDB, start: date, end: date) -> list[dict[str, Any]]:
    """Monthly charges and active weekly/yearly subscriptions due in [start, end], soonest first."""
    await ensure_schema(db)
    out: list[dict[str, Any]] = []
    recurring = await db.fetch("SELECT * FROM app_recurring")
    day = start
    while day <= end:
        month = day.strftime("%Y-%m")
        for r in recurring:
            if money.active_in(r["start_month"], r.get("end_month"), month) and money.occurrence(month, r["day"]) == day.isoformat():
                out.append({"source": "recurring", "id": r["id"], "name": r["name"], "amount": float(r["amount"] or 0),
                            "currency": r["currency"], "category": r.get("category") or "bills", "date": day.isoformat()})
        day += timedelta(days=1)
    for r in await _active_subs(db):
        anchor = _parse_date(r.get("anchor_date"))
        if anchor is None:
            continue
        for d in occurrences_between(anchor, r["frequency"], start, end):
            out.append({"source": "subscription", "id": r["id"], "name": r["name"], "amount": float(r["amount"] or 0),
                        "currency": r["currency"], "category": r.get("category") or "bills", "date": d.isoformat(),
                        "cadence": r["frequency"]})
    out.sort(key=lambda b: (b["date"], b["name"]))
    return out


def compose_bill(bill: dict[str, Any], today: date) -> Any:
    from hub_api.routers.notify import Push, fmt_money

    due = date.fromisoformat(bill["date"])
    days = (due - today).days
    when = "aujourd'hui" if days <= 0 else "demain" if days == 1 else "après-demain" if days == 2 else f"dans {days} jours"
    label = f"{_WEEKDAYS_FR[due.weekday()]} {due.day} {_MONTHS_FR[due.month - 1]}"
    cadence = {"weekly": " (hebdomadaire)", "yearly": " (annuel)"}.get(bill.get("cadence") or "", "")
    return Push(
        title=f"💳 {bill['name']} : {fmt_money(bill['amount'], bill['currency'])} {when}"[:250],
        message=f"Prélèvement prévu {label}{cadence}. Vérifie que le compte est approvisionné.",
        tags=["credit_card"], path="#/more/money", actions=[("Money", "#/more/money")],
        dedupe=f"bills:{bill['source']}:{bill['id']}:{bill['date']}",
    )


async def run_bills(state: HubState, db: AppDB, *, preview: bool, force: bool) -> dict[str, Any]:
    """One push per charge due tomorrow or the day after (a daily schedule sees each charge twice: deduped)."""
    from hub_api.routers.notify import deliver

    today = local_today()
    bills = (await upcoming_bills(db, today + timedelta(days=1), today + timedelta(days=BILLS_AHEAD_DAYS)))[:MAX_BILLS]
    pushes = [compose_bill(b, today) for b in bills]
    out: dict[str, Any] = {"kind": "bills", "sent": False, "skipped": None, "count": 0, "title": None,
                           "message": None, "results": []}
    if not pushes:
        out["skipped"] = "nothing to report"
        return out
    out["title"] = pushes[0].title if len(pushes) == 1 else f"{len(pushes)} prélèvements à venir"
    out["message"] = "\n".join(p.title for p in pushes)
    if preview:
        out["skipped"] = "preview"
        return out
    errors: list[notifier.NotifyError] = []
    for push in pushes:
        try:
            res = await deliver(state, db, "bills", push, force=force)
        except notifier.NotifyError as exc:  # keep going; the caller answers 502 so QStash retries (sent ones are deduped)
            errors.append(exc)
            res = {"sent": False, "skipped": f"failed: {exc}", "title": push.title}
        out["results"].append({k: res.get(k) for k in ("sent", "skipped", "title")})
    if errors:
        raise errors[0]
    out["count"] = sum(1 for r in out["results"] if r["sent"])
    out["sent"] = out["count"] > 0
    if not out["sent"]:
        out["skipped"] = out["results"][0]["skipped"]
    return out


# --------------------------------------------------------------------------- live budget alerts


def budget_level(fraction: float) -> int | None:
    """The highest alert threshold (percent) ``fraction`` reached, None below 80 %."""
    return next((lvl for lvl in ALERT_LEVELS if fraction >= lvl / 100), None)


async def category_spend(db: AppDB, category: str, month: str, rates: money.Rates) -> float:
    """MAD spent in ``category`` during ``month``: expenses plus the monthly charges no expense paid (as the overview)."""
    expenses = await db.fetch(
        "SELECT id, date, merchant, total, currency FROM app_expenses WHERE category = ? AND date >= ? AND date <= ?",
        [category, f"{month}-01", f"{month}-31"],
    )
    recurring = [r for r in await db.fetch("SELECT * FROM app_recurring WHERE category = ?", [category])
                 if money.active_in(r["start_month"], r.get("end_month"), month)]

    def mad(amount: float, currency: str) -> float:
        return rates.convert(float(amount or 0), currency or money.BASE) or 0.0

    rows = [{**e, "currency": e.get("currency") or money.BASE, "total": float(e.get("total") or 0)} for e in expenses]
    paid = money.paid_charges([month], recurring, rows, mad)
    total = sum(mad(e["total"], e["currency"]) for e in rows)
    total += sum(mad(r["amount"], r["currency"]) for r in recurring if (r["id"], month) not in paid)
    return round(total, 2)


async def _budget_push(db: AppDB, expense: dict[str, Any]) -> Any:
    """The spending :class:`~hub_api.routers.notify.Push` the expense calls for, or None (reads only)."""
    from hub_api.routers import notify

    category = expense.get("category") or "other"
    month = str(expense.get("date") or "")[:7]
    if month != local_today().strftime("%Y-%m"):
        return None  # an old receipt or import: that month is over, an alert would be noise
    try:
        budget = await db.fetch_one("SELECT amount, currency FROM app_budgets WHERE category = ?", [category])
    except Exception:  # noqa: BLE001 - no money tables yet: no budget
        return None
    if budget is None:
        return None
    rates = await cached_rates(db)
    limit = rates.convert(float(budget["amount"] or 0), budget.get("currency") or money.BASE)
    if not limit:
        return None
    spent = await category_spend(db, category, month, rates)
    level = budget_level(spent / limit)
    if level is None:
        return None
    await notify.ensure_schema(db)
    if not (await notify.get_prefs(db)).get("spending", True):
        return None
    if level == 80:
        done = await db.fetch_one("SELECT status FROM app_notify_log WHERE key = ?", [f"spending:{category}:{month}:100"])
        if done and done.get("status") == "sent":
            return None  # already told it is over
    label = _CATEGORY_FR.get(category, category)
    pct = round(spent / limit * 100)
    amounts = f"{notify.fmt_money(spent, money.BASE)} sur {notify.fmt_money(limit, money.BASE)}"
    if level == 100:
        title = f"⛔ Budget {label} dépassé : {pct} %"
        message = f"{amounts} ce mois-ci (dernière dépense : {expense.get('merchant') or 'sans nom'})."
    else:
        title = f"⚠ Budget {label} : {pct} %"
        left = notify.fmt_money(max(0.0, limit - spent), money.BASE)
        message = f"{amounts} ce mois-ci : il reste {left}."
    return notify.Push(title=title, message=message, tags=["warning"], priority=4 if level == 100 else 3,
                       path="#/more/money", actions=[("Money", "#/more/money")],
                       dedupe=f"spending:{category}:{month}:{level}")


async def _deliver_alert(state: HubState, db: AppDB, push: Any) -> dict[str, Any] | None:
    """Send the alert within :data:`ALERT_DELIVER_TIMEOUT_S`; when cut short, a claim left 'sending' is set to
    'failed' so the next expense retries it instead of finding it 'already sent' for the rest of the month."""
    from hub_api.routers import notify

    try:
        return await asyncio.wait_for(notify.deliver(state, db, "spending", push), ALERT_DELIVER_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("live budget alert timed out: %s", push.dedupe)
        try:
            await db.execute("UPDATE app_notify_log SET status = 'failed', error = 'timed out' WHERE key = ?"
                             " AND status = 'sending'", [push.dedupe])
        except Exception as exc:  # noqa: BLE001 - nothing more to do
            log.warning("could not release %s: %r", push.dedupe, exc)
        return None


async def budget_alert_after_insert(db: AppDB, expense: dict[str, Any]) -> dict[str, Any] | None:
    """Live budget alert for a just-inserted expense. Best effort: the checks are bounded by :data:`ALERT_TIMEOUT_S`
    and the push by :data:`ALERT_DELIVER_TIMEOUT_S` (see :func:`_deliver_alert`); never raises.

    The hub state comes from the AppDB (:func:`hub_api.appdata.appdb_for` attaches it); without it, or
    without any push channel configured, nothing happens.
    """
    state: HubState | None = getattr(db, "hub_state", None)
    if state is None:
        return None
    try:
        if not (notifier.load_config(state.settings.secret).configured
                or webpush.load_config(state.settings.secret).configured):
            return None
        push = await asyncio.wait_for(_budget_push(db, expense), ALERT_TIMEOUT_S)
        return None if push is None else await _deliver_alert(state, db, push)
    except Exception as exc:  # noqa: BLE001 - the expense is already saved
        log.warning("live budget alert failed: %r", exc)
        return None


# --------------------------------------------------------------------------- goals


def months_until(deadline: date, today: date) -> int:
    """Monthly contributions left before ``deadline``: whole months from today, at least 1 (0 when past)."""
    if deadline < today:
        return 0
    n = (deadline.year - today.year) * 12 + deadline.month - today.month
    if deadline.day < today.day:
        n -= 1
    return max(1, n)


def goal_out(row: dict[str, Any], today: date, rates: money.Rates, accounts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    target = float(row.get("target") or 0)
    saved = float(row.get("saved") or 0)
    currency = row.get("currency") or money.BASE
    remaining = round(max(0.0, target - saved), 2)
    deadline = _parse_date(row.get("deadline"))
    months_left = months_until(deadline, today) if deadline else None
    if deadline is None or remaining <= 0:
        per_month = None if deadline is None else 0.0
    else:
        per_month = round(remaining / months_left, 2) if months_left else remaining
    account = (accounts or {}).get(row.get("account_id") or "")
    return {
        "id": row["id"],
        "name": row["name"],
        "target": round(target, 2),
        "currency": currency,
        "saved": round(saved, 2),
        "remaining": remaining,
        "progress": round(min(1.0, saved / target), 4) if target > 0 else 0.0,
        "done": target > 0 and saved >= target,
        "deadline": row.get("deadline"),
        "months_left": months_left,
        "overdue": bool(deadline and deadline < today and remaining > 0),
        "per_month_needed": per_month,
        "saved_mad": rates.convert(saved, currency),
        "target_mad": rates.convert(target, currency),
        "account_id": row.get("account_id"),
        "account": {"id": account["id"], "name": account["name"]} if account else None,
        "emoji": row.get("emoji"),
        "color": row.get("color"),
        "note": row.get("note"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
