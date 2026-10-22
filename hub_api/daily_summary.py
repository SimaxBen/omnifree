"""The persistent daily summary notification (``POST /notify/run?kind=summary``), a stand-in for a home screen widget.

One notification holds today's Google Calendar events, the next open tasks and the money left this month. The
schedule template "Daily summary: 07:00 and 13:00" refreshes it twice a day.

Replacing instead of stacking: the web push carries the stable tag ``omnifree-summary`` with ``renotify: false``,
so the service worker swaps the previous summary for the new one without buzzing again. ntfy has no way to
replace a delivered message: each run posts a new one there (low priority, the same title and the ``summary``
tag); turn the ntfy channel off on the Notifications page to keep only the replacing web push.

Every source is best effort and bounded (the calendar read and the money overview each have a timeout), so the
whole run stays far under the 60 s serverless limit. Texts are in French like the other pushes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from hub_api import gcal
from hub_api.appdata import AppDB
from hub_api.routers.notify import MAX_TASK_LINES, Push, _day_label, _due_at, _event_time, _plural, fmt_money
from hub_api.state import HubState

__all__ = ["compose_summary", "SUMMARY_TAG", "summary_lines", "money_line", "next_tasks"]

log = logging.getLogger("omnifree.hub.summary")

SUMMARY_TAG = "omnifree-summary"
MONEY_TIMEOUT_S = 8.0
AGENDA_TIMEOUT_S = 6.0
MAX_EVENTS = 4
UPCOMING_DAYS = 7


async def next_tasks(db: AppDB, today: date) -> tuple[list[dict[str, Any]], int]:
    """(open tasks due today, then the next dated ones within a week, at most :data:`MAX_TASK_LINES`; overdue count)."""
    rows = await db.fetch(
        "SELECT id, title, priority, due, due_time FROM app_tasks WHERE done = 0 AND due IS NOT NULL AND due >= ?"
        " ORDER BY due, (due_time IS NULL), due_time,"
        " CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at LIMIT ?",
        [today.isoformat(), MAX_TASK_LINES * 2],
        limit=MAX_TASK_LINES * 2,
    )
    horizon = date.fromordinal(today.toordinal() + UPCOMING_DAYS).isoformat()
    tasks = [r for r in rows if str(r["due"])[:10] <= horizon][:MAX_TASK_LINES]
    overdue = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM app_tasks WHERE done = 0 AND due IS NOT NULL AND due < ?", [today.isoformat()]
    )
    return tasks, int((overdue or {}).get("n") or 0)


def _task_line(t: dict[str, Any], today: date, now: datetime) -> str:
    due = str(t["due"])[:10]
    when = "" if due == today.isoformat() else f"{_day_label(date.fromisoformat(due))} "
    clock = f"{t['due_time']} " if t.get("due_time") else ""
    at = _due_at(t)
    late = " (passé)" if at is not None and at < now and due == today.isoformat() else ""
    return f"• {when}{clock}{'❗ ' if t.get('priority') == 'high' else ''}{t['title']}{late}"


def money_line(overview: dict[str, Any] | None) -> str | None:
    """"Reste ce mois" from a money overview (income − spending − recurring), or the month's spending without income."""
    if not overview:
        return None
    cur = overview.get("currency") or "MAD"
    income = float((overview.get("income") or {}).get("total") or 0)
    spent = float((overview.get("spent") or {}).get("total") or 0)
    if income <= 0:
        return f"Dépensé ce mois : {fmt_money(spent, cur)}" if spent else None
    net = float(overview.get("net") or income - spent)
    line = f"Reste ce mois : {'' if net >= 0 else '−'}{fmt_money(abs(net), cur)} (revenus {fmt_money(income, cur)})"
    budgets = [c for c in overview.get("categories") or [] if c.get("budget")]
    if budgets:
        left = sum(max(float(c["budget"]) - float(c.get("spent") or 0), 0.0) for c in budgets)
        line += f"\nBudgets : {fmt_money(left, cur)} disponibles"
    return line


def summary_lines(events: list[dict[str, Any]], tasks: list[dict[str, Any]], overdue: int, money: str | None,
                  today: date, now: datetime) -> list[str]:
    lines: list[str] = []
    if events:
        lines.append("Agenda :")
        lines += [f"• {_event_time(e)} {e['title']}" for e in events[:MAX_EVENTS]]
        if len(events) > MAX_EVENTS:
            lines.append(f"… et {len(events) - MAX_EVENTS} autre{'s' if len(events) - MAX_EVENTS > 1 else ''}")
    if tasks:
        lines.append("Tâches :")
        lines += [_task_line(t, today, now) for t in tasks]
    if overdue:
        lines.append(f"En retard : {_plural(overdue, 'tâche')}")
    if money:
        lines.append(money)
    return lines or ["Rien de prévu aujourd'hui."]


async def _overview(db: AppDB, today: date) -> dict[str, Any] | None:
    from hub_api.routers import money

    try:
        await money.ensure_schema(db)
        return await asyncio.wait_for(money.overview_for(db, today.strftime("%Y-%m"), today), MONEY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the summary still goes out without the money line
        log.info("money overview for the summary unavailable: %r", exc)
        return None


async def compose_summary(state: HubState, db: AppDB, today: date, now: datetime) -> Push:
    events, (tasks, overdue), overview = await asyncio.gather(
        gcal.agenda_day(state, today, timeout_s=AGENDA_TIMEOUT_S), next_tasks(db, today), _overview(db, today),
    )
    today_tasks = [t for t in tasks if str(t["due"])[:10] == today.isoformat()]
    lines = summary_lines(events, tasks, overdue, money_line(overview), today, now)
    parts = [p for p in (events and _plural(len(events), 'événement'), today_tasks and _plural(len(today_tasks), 'tâche')) if p]
    return Push(
        title=f"Aujourd'hui, {_day_label(today)}" + (f" : {', '.join(parts)}" if parts else ""),
        message="\n".join(lines), tags=["summary", "spiral_calendar"], priority=2, path="#/home",
        actions=[("Tâches", "#/notes/tasks"), ("Argent", "#/more/money")],
        dedupe=f"summary:{today}T{now:%H}", tag=SUMMARY_TAG, renotify=False,
    )
