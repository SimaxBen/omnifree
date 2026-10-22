"""/notify: push notifications to the owner's phone through ntfy (see :mod:`hub_api.notifier`).

* ``GET /notify/status``: configured?, the topic (the PWA shows it behind a reveal toggle), per-kind
  switches and the last pushes;
* ``PUT /notify/prefs``: turn kinds on or off (stored in D1, all on by default);
* ``POST /notify/test``: one test push;
* ``POST /notify/run?kind=morning|evening|weekly|budget|month|reminders``: what the QStash schedules call (no
  body needed). Daily/weekly/monthly kinds are deduplicated in D1 (one push per kind and day, week or month;
  ``force=1`` bypasses it, ``preview=1`` composes without sending); ``budget`` only pushes items that
  became ``warn``/``over`` since the last budget push; ``reminders`` (every 15 min) pushes one notification
  per open task whose due date + time falls in [now - 5 min, now + 15 min], deduplicated per task and due
  time, with a "Fait ✓" button;
* ``POST /notify/done/{task_id}?sig=...`` (``public_router``, no bearer: the ntfy app calls it from the
  notification button): marks that one task done. ``sig`` is an HMAC-SHA256 of the task id under a key
  derived from the hub token (see :func:`done_sig`), compared in constant time; anything else answers 404.

The email receipt import calls :func:`push_inbox_import` after a sync that created expenses (best effort).
Push texts are in French (the owner's language); days are Africa/Casablanca days.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from hub_api import notifier
from hub_api.appdata import AppDB, get_appdb, iso, local_now, local_today, local_tz, new_id, now_iso
from hub_api.budget_report import build_budget
from hub_api.recurring import monthly_equivalent, recurring_items
from hub_api.routers.expenses import ensure_schema as ensure_expenses
from hub_api.routers.expenses import expense_out, summarise
from hub_api.routers.notes import ensure_schema as ensure_notes
from hub_api.state import HubState, get_state

__all__ = [
    "router", "public_router", "SCHEMA", "KINDS", "RUN_KINDS", "Push", "push_inbox_import", "fmt_money",
    "ensure_schema", "done_sig", "reminder_window",
]

log = logging.getLogger("omnifree.hub.notify")

router = APIRouter(prefix="/notify", tags=["notify"])
public_router = APIRouter(tags=["public"])

RunKind = Literal["morning", "evening", "weekly", "budget", "month", "reminders"]
RUN_KINDS: tuple[str, ...] = ("morning", "evening", "weekly", "budget", "month", "reminders")
KINDS: tuple[str, ...] = (*RUN_KINDS, "inbox")
BUDGET_TIMEOUT_S = 40.0  # the whole call must stay under the 60 s serverless limit
INBOX_PUSH_TIMEOUT_S = 5.0
LOG_KEEP_DAYS = 60
MAX_TASK_LINES = 5
REMIND_BEFORE = timedelta(minutes=15)  # a reminder goes out up to 15 min before the due time...
REMIND_AFTER = timedelta(minutes=5)  # ...or up to 5 min after it (a late or retried run)
MAX_REMINDERS = 10  # per run: stays well inside the 60 s serverless limit
_DONE_KEY_LABEL = b"omnifree/notify-done/v1"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_notify_prefs (kind TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_notify_log ("
    " key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, message TEXT, status TEXT NOT NULL,"
    " error TEXT, ntfy_id TEXT, sent_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_notify_log_sent ON app_notify_log (sent_at)",
    "CREATE TABLE IF NOT EXISTS app_notify_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
)

_CATEGORY_FR = {
    "food": "restaurants", "groceries": "courses", "transport": "transport", "housing": "logement",
    "bills": "factures", "health": "santé", "shopping": "shopping", "entertainment": "loisirs",
    "education": "éducation", "travel": "voyages", "other": "autre",
}
_MONTHS_FR = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre")


async def ensure_schema(db: AppDB) -> AppDB:
    await ensure_expenses(db)
    await ensure_notes(db)
    await db.ensure("notify", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


@dataclass
class Push:
    title: str
    message: str
    tags: list[str] = field(default_factory=list)
    priority: int = 3
    path: str = ""  # PWA hash route opened by a tap, e.g. "#/notes/tasks"
    actions: list[tuple[str, str]] = field(default_factory=list)  # (label, PWA hash route)
    http_actions: list[dict[str, Any]] = field(default_factory=list)  # ntfy "http" actions, sent first
    dedupe: str | None = None  # one push per key (kind + day/week/month)


# --------------------------------------------------------------------------- formatting


def fmt_money(amount: float, currency: str) -> str:
    """French style: ``1 234,50 MAD`` (no decimals for whole amounts)."""
    text = f"{amount:,.0f}" if abs(amount - round(amount)) < 0.005 else f"{amount:,.2f}"
    return f"{text.replace(',', ' ').replace('.', ',')} {currency}"


def _totals(totals: dict[str, float]) -> str:
    return ", ".join(fmt_money(v, k) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])) or "0"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _day_label(d: date) -> str:
    return f"{d.day} {_MONTHS_FR[d.month - 1]}"


def _task_lines(tasks: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"• {t['due_time'] + ' ' if t.get('due_time') else ''}{'❗ ' if t.get('priority') == 'high' else ''}{t['title']}"
        for t in tasks[:MAX_TASK_LINES]
    ]
    if len(tasks) > MAX_TASK_LINES:
        lines.append(f"… et {len(tasks) - MAX_TASK_LINES} autre{'s' if len(tasks) - MAX_TASK_LINES > 1 else ''}")
    return lines


# --------------------------------------------------------------------------- data


async def _tasks_due(db: AppDB, day: date) -> list[dict[str, Any]]:
    """Open tasks due on ``day``: timed ones in time order, then the others by priority."""
    return await db.fetch(
        "SELECT id, title, priority, due, due_time FROM app_tasks WHERE done = 0 AND due = ?"
        " ORDER BY (due_time IS NULL), due_time,"
        " CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at",
        [day.isoformat()],
    )


def _due_at(task: dict[str, Any]) -> datetime | None:
    """The task's local due datetime (aware, Africa/Casablanca), None without a valid date and time."""
    try:
        h, m = (int(x) for x in str(task.get("due_time") or "").split(":"))
        return datetime.combine(date.fromisoformat(str(task.get("due"))[:10]), time(h, m), tzinfo=local_tz())
    except (TypeError, ValueError):
        return None


async def _count(db: AppDB, sql: str, params: list[Any]) -> int:
    row = await db.fetch_one(sql, params)
    return int((row or {}).get("n") or 0)


async def _expenses(db: AppDB, start: date, end: date) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT * FROM app_expenses WHERE date >= ? AND date <= ? ORDER BY date DESC", [start.isoformat(), end.isoformat()]
    )
    return [expense_out(r) for r in rows]


async def _state_get(db: AppDB, key: str, table: str = "app_notify_state") -> dict[str, Any] | None:
    try:
        row = await db.fetch_one(f"SELECT value FROM {table} WHERE key = ?", [key])  # noqa: S608 - fixed names
    except Exception as exc:  # noqa: BLE001 - e.g. the inbox table does not exist yet
        log.debug("state %s.%s unavailable: %r", table, key, exc)
        return None
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _state_set(db: AppDB, key: str, value: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO app_notify_state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [key, json.dumps(value, ensure_ascii=False), now_iso()],
    )


def _recent(stamp: Any, hours: float) -> bool:
    if not isinstance(stamp, str):
        return False
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return 0 <= (datetime.now(timezone.utc) - t).total_seconds() <= hours * 3600


# --------------------------------------------------------------------------- composers


async def compose_morning(db: AppDB, today: date) -> Push:
    due = await _tasks_due(db, today)
    overdue = await _count(db, "SELECT COUNT(*) AS n FROM app_tasks WHERE done = 0 AND due IS NOT NULL AND due < ?",
                           [today.isoformat()])
    yesterday = today - timedelta(days=1)
    spent = summarise(await _expenses(db, yesterday, yesterday))
    lines = _task_lines(due)
    if overdue:
        lines.append(f"En retard : {_plural(overdue, 'tâche')}")
    lines.append(
        f"Hier : {_totals(spent['total_by_currency'])} ({_plural(spent['count'], 'dépense')})" if spent["count"]
        else "Hier : aucune dépense notée"
    )
    sync = await _state_get(db, "receipts_sync", "app_inbox_state") or {}
    created = (sync.get("last_result") or {}).get("created") or []
    if created and _recent(sync.get("last_sync_at"), 24):
        lines.append(f"Import e-mail : {_plural(len(created), 'nouvelle dépense', 'nouvelles dépenses')}")
    title = (f"Bonjour : {_plural(len(due), 'tâche')} aujourd'hui" if due
             else "Bonjour : rien de prévu aujourd'hui")
    return Push(title=title, message="\n".join(lines), tags=["sunrise"], path="#/notes/tasks",
                actions=[("Tâches", "#/notes/tasks"), ("Dépenses", "#/more/expenses")], dedupe=f"morning:{today}")


async def compose_evening(db: AppDB, today: date, now: datetime | None = None) -> Push | None:
    """Today's tasks still open. A timed task whose time is still ahead tonight is "à venir", not late."""
    due_today = await _tasks_due(db, today)
    now = now or local_now()
    upcoming = [t for t in due_today if (at := _due_at(t)) is not None and at > now]
    still_open = [t for t in due_today if t not in upcoming]
    logged = await _count(db, "SELECT COUNT(*) AS n FROM app_expenses WHERE date = ?", [today.isoformat()])
    if not due_today and logged:
        return None
    lines = _task_lines(still_open)
    if upcoming:
        lines.append("À venir : " + ", ".join(f"{t['due_time']} {t['title']}" for t in upcoming[:MAX_TASK_LINES]))
    if not logged:
        lines.append("Aucune dépense notée aujourd'hui : tu veux ajouter celles du jour ?")
    if still_open:
        title = f"Ce soir : {_plural(len(still_open), 'tâche encore ouverte', 'tâches encore ouvertes')}"
        if upcoming:
            title += f", {len(upcoming)} à venir"
    elif upcoming:
        title = f"Ce soir : {_plural(len(upcoming), 'tâche', 'tâches')} à venir"
    else:
        title = "Ce soir : les dépenses du jour ?"
    actions = [("Tâches", "#/notes/tasks")] if due_today else []
    actions.append(("Dépenses", "#/more/expenses"))
    return Push(title=title, message="\n".join(lines), tags=["crescent_moon"],
                path="#/notes/tasks" if due_today else "#/more/expenses", actions=actions, dedupe=f"evening:{today}")


async def compose_weekly(db: AppDB, today: date) -> Push:
    from hub_api.routers.digest import _note_stats, _task_stats, top_merchants  # digest stats, no LLM call

    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    expenses = await _expenses(db, start, end)
    exp = summarise(expenses)
    tasks = await _task_stats(db, start, today)
    notes = await _note_stats(db, start)
    totals = exp["total_by_currency"]
    lines = [
        f"Du {_day_label(start)} au {_day_label(end)} : {_plural(exp['count'], 'dépense')}, "
        f"{_plural(tasks['done'], 'tâche faite', 'tâches faites')}, {_plural(notes['created'], 'note')}."
    ]
    if totals:
        main = max(totals, key=lambda c: totals[c])
        cats = sorted(((c, m.get(main, 0.0)) for c, m in exp["by_category"].items()), key=lambda kv: -kv[1])
        if cats and cats[0][1] > 0:
            lines.append(f"Surtout : {_CATEGORY_FR.get(cats[0][0], cats[0][0])} ({fmt_money(cats[0][1], main)}).")
        tops = top_merchants(expenses, 1)
        if tops:
            t = tops[0]
            lines.append(f"Top marchand : {t['merchant']} ({fmt_money(t['total'], t['currency'])}, {t['count']}x).")
    if tasks["overdue"]:
        lines.append(f"À rattraper : {_plural(tasks['overdue'], 'tâche')} en retard.")
    title = f"Ta semaine : {_totals(totals)} dépensés" if totals else "Ta semaine en bref"
    return Push(title=title, message="\n".join(lines), tags=["bar_chart"], path="#/more/summary",
                actions=[("Résumé", "#/more/summary")], dedupe=f"weekly:{start}")


def _budget_key(item: dict[str, Any]) -> str:
    return f"{item['provider']}/{item['metric']}/{item.get('window') or 'total'}"


def budget_changes(items: list[dict[str, Any]], previous: dict[str, str]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """(current alerts, items to push): an item is pushed when it becomes warn/over or goes warn -> over."""
    current = {_budget_key(i): i["status"] for i in items if i.get("status") in ("warn", "over")}
    fresh = [
        i for i in items
        if i.get("status") in ("warn", "over") and previous.get(_budget_key(i)) not in (i["status"], "over")
    ]
    return current, fresh


async def compose_budget(state: HubState, db: AppDB) -> tuple[Push | None, dict[str, str]]:
    report = await asyncio.wait_for(build_budget(state.router), BUDGET_TIMEOUT_S)
    previous = (await _state_get(db, "budget_alerts") or {}).get("alerts") or {}
    current, fresh = budget_changes(report.get("items") or [], previous if isinstance(previous, dict) else {})
    if not fresh:
        return None, current
    over = [i for i in fresh if i["status"] == "over"]
    lines = []
    for i in sorted(fresh, key=lambda i: (i["status"] != "over", -(i.get("fraction") or 0))):
        pct = f"{round((i.get('fraction') or 0) * 100)} %" if i.get("fraction") is not None else i["status"]
        window = f"/{i['window']}" if i.get("window") else ""
        lines.append(f"{'⛔' if i['status'] == 'over' else '⚠'} {i['provider']} {i['metric']}{window} : {pct}")
    title = (f"Quotas : {_plural(len(over), 'limite dépassée', 'limites dépassées')}" if over
             else f"Quotas : {_plural(len(fresh), 'limite proche', 'limites proches')}")
    push = Push(title=title, message="\n".join(lines), tags=["warning"], priority=4 if over else 3,
                path="#/more/budget", actions=[("Budget", "#/more/budget")])
    return push, current


async def compose_month(db: AppDB, today: date) -> Push:
    start = today.replace(day=1)
    prev_end = start - timedelta(days=1)
    exp = summarise(await _expenses(db, start, today))
    prev = summarise(await _expenses(db, prev_end.replace(day=1), prev_end))
    totals = exp["total_by_currency"]
    month = _MONTHS_FR[today.month - 1]
    lines = [f"{month.capitalize()} au {today.day} : {_plural(exp['count'], 'dépense')}, {_totals(totals)}"
             + (f" (mois dernier : {_totals(prev['total_by_currency'])})." if prev["count"] else ".")]
    if totals:
        main = max(totals, key=lambda c: totals[c])
        cats = sorted(((c, m.get(main, 0.0)) for c, m in exp["by_category"].items() if m.get(main)), key=lambda kv: -kv[1])
        if cats:
            lines.append("Top : " + ", ".join(f"{_CATEGORY_FR.get(c, c)} {fmt_money(v, main)}" for c, v in cats[:3]) + ".")
    recurring = await recurring_items(db, local_today())  # empty unless the money tables exist
    if recurring:
        income: dict[str, float] = {}
        spend: dict[str, float] = {}
        for item in recurring:
            bucket = income if item["kind"] == "income" else spend
            bucket[item["currency"]] = bucket.get(item["currency"], 0.0) + monthly_equivalent(item)
        lines.append(f"Récurrent / mois : +{_totals(income)} revenus, −{_totals(spend)} charges.")
        for cur in sorted(set(income) | set(spend)):
            balance = income.get(cur, 0.0) - spend.get(cur, 0.0) - totals.get(cur, 0.0)
            lines.append(f"Reste estimé ({cur}) : {'+' if balance >= 0 else '−'}{fmt_money(abs(balance), cur)}.")
    title = f"Bilan de {month} : {_totals(totals)} dépensés" if totals else f"Bilan de {month}"
    return Push(title=title, message="\n".join(lines), tags=["moneybag"], path="#/more/expenses",
                actions=[("Dépenses", "#/more/expenses")], dedupe=f"month:{today:%Y-%m}")


def reminder_window(now: datetime, ahead: timedelta = REMIND_BEFORE) -> tuple[datetime, datetime]:
    """[now - 5 min, now + ahead]: with a schedule every ``ahead`` minutes every due time falls in a window
    (15 min by default; the owner runs it every 3 h with ``ahead=180``)."""
    return now - REMIND_AFTER, now + ahead


async def due_reminders(db: AppDB, now: datetime, ahead: timedelta = REMIND_BEFORE) -> list[dict[str, Any]]:
    """Open timed tasks whose local due datetime is inside :func:`reminder_window`, soonest first."""
    start, end = reminder_window(now, ahead)
    days = sorted({start.date().isoformat(), end.date().isoformat()})
    rows = await db.fetch(
        "SELECT id, title, priority, details, due, due_time FROM app_tasks WHERE done = 0 AND due_time IS NOT NULL"
        f" AND due IN ({', '.join('?' for _ in days)}) ORDER BY due, due_time, created_at",
        days,
    )
    out = []
    for r in rows:
        at = _due_at(r)
        if at is not None and start <= at <= end:
            out.append({**r, "at": at})
    return out


def done_sig(hub_token: str, task_id: str) -> str:
    """Signature of the "Fait ✓" link: HMAC-SHA256(task id) under a key derived from the hub token.

    Deriving (instead of using the token or the ntfy topic) means the URL never exposes the token, and
    knowing the topic is not enough to forge a link for another task.
    """
    key = hmac.new(hub_token.encode("utf-8"), _DONE_KEY_LABEL, hashlib.sha256).digest()
    return hmac.new(key, f"done:{task_id}".encode(), hashlib.sha256).hexdigest()


def compose_reminder(state: HubState, task: dict[str, Any], now: datetime) -> Push:
    at: datetime = task["at"]
    minutes = round((at - now).total_seconds() / 60)
    when = (f"dans {minutes} min" if minutes > 0 else "maintenant" if minutes == 0 else f"il y a {-minutes} min")
    lines = [f"{task['due_time']} ({when})"]
    if task.get("details"):
        lines.append(str(task["details"])[:300])
    http_actions: list[dict[str, Any]] = []
    token = state.hub_token()
    if token:
        base = state.hub.public_base_url.rstrip("/")
        http_actions.append({
            "action": "http", "label": "Fait ✓", "method": "POST", "clear": True,
            "url": f"{base}/notify/done/{task['id']}?sig={done_sig(token, task['id'])}",
        })
    return Push(
        title=f"{'❗ ' if task.get('priority') == 'high' else ''}⏰ {task['title']}"[:250],
        message="\n".join(lines), tags=["alarm_clock"], priority=4, path="#/notes/tasks",
        actions=[("Tâches", "#/notes/tasks")], http_actions=http_actions,
        dedupe=f"reminder:{task['id']}:{task['due']}T{task['due_time']}",
    )


async def run_reminders(state: HubState, db: AppDB, *, preview: bool, force: bool,
                        ahead: timedelta = REMIND_BEFORE) -> dict[str, Any]:
    now = local_now()
    tasks = await due_reminders(db, now, ahead)
    pushes = [compose_reminder(state, t, now) for t in tasks[:MAX_REMINDERS]]
    out: dict[str, Any] = {"kind": "reminders", "sent": False, "skipped": None, "count": 0, "title": None,
                           "message": None, "results": []}
    if not pushes:
        out["skipped"] = "nothing to report"
        return out
    out["title"] = pushes[0].title if len(pushes) == 1 else f"{len(pushes)} rappels"
    out["message"] = "\n".join(f"{p.title} · {p.message.splitlines()[0]}" for p in pushes)
    if preview:
        out["skipped"] = "preview"
        return out
    errors: list[notifier.NotifyError] = []
    for push in pushes:
        try:
            res = await deliver(state, db, "reminders", push, force=force)
        except notifier.NotifyError as exc:  # keep going; the 502 below makes QStash retry (sent ones are deduped)
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


# --------------------------------------------------------------------------- sending


async def get_prefs(db: AppDB) -> dict[str, bool]:
    rows = await db.fetch("SELECT kind, enabled FROM app_notify_prefs", [])
    saved = {r["kind"]: bool(r["enabled"]) for r in rows}
    return {k: saved.get(k, True) for k in KINDS}


def _pwa(state: HubState, path: str) -> str | None:
    base = state.hub.pwa_base_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}" if base and path else (base or None)


async def deliver(state: HubState, db: AppDB, kind: str, push: Push, *, force: bool = False) -> dict[str, Any]:
    """Send ``push`` (claiming its dedupe key first); returns the outcome. Raises :class:`notifier.NotifyError`."""
    cfg = notifier.load_config(state.settings.secret)
    out: dict[str, Any] = {"kind": kind, "sent": False, "skipped": None, "title": push.title, "message": push.message}
    if not cfg.configured:
        out["skipped"] = f"ntfy not configured (vault key {notifier.TOPIC_KEY})"
        return out
    now = now_iso()
    key = push.dedupe if push.dedupe and not force else f"{kind}:{now}:{new_id()}"
    claimed = await db.execute(
        "INSERT INTO app_notify_log (key, kind, title, message, status, sent_at) VALUES (?, ?, ?, ?, 'sending', ?)"
        " ON CONFLICT(key) DO UPDATE SET status = 'sending', title = excluded.title, message = excluded.message,"
        " error = NULL, sent_at = excluded.sent_at WHERE app_notify_log.status = 'failed'",
        [key, kind, push.title, push.message, now],
    )
    if not claimed:
        out["skipped"] = "already sent"
        return out
    actions = [*push.http_actions] + [
        {"action": "view", "label": label, "url": url, "clear": True}
        for label, path in push.actions if (url := _pwa(state, path))
    ]
    try:
        answer = await notifier.publish(
            cfg, push.message, title=push.title, priority=push.priority, tags=push.tags,
            click=_pwa(state, push.path), actions=actions,
        )
    except notifier.NotifyError as exc:
        await db.execute("UPDATE app_notify_log SET status = 'failed', error = ? WHERE key = ?", [str(exc)[:300], key])
        raise
    await db.execute("UPDATE app_notify_log SET status = 'sent', ntfy_id = ? WHERE key = ?",
                     [str(answer.get("id") or "") or None, key])
    await db.execute("DELETE FROM app_notify_log WHERE sent_at < ?",
                     [iso(datetime.now(timezone.utc) - timedelta(days=LOG_KEEP_DAYS))])
    out["sent"] = True
    return out


async def push_inbox_import(state: HubState, db: AppDB, result: dict[str, Any]) -> None:
    """Best-effort push after an email import that created expenses; never raises."""
    created = [e for e in result.get("created") or [] if e.get("id")]
    if not created or result.get("dry_run"):
        return
    try:
        if not notifier.load_config(state.settings.secret).configured:
            return

        async def go() -> None:
            await ensure_schema(db)
            if not (await get_prefs(db))["inbox"]:
                return
            lines = [f"• {e.get('merchant') or 'Inconnu'} {fmt_money(float(e.get('total') or 0), e.get('currency') or 'MAD')}"
                     f" ({e.get('date')})" for e in created[:MAX_TASK_LINES]]
            if len(created) > MAX_TASK_LINES:
                lines.append(f"… et {len(created) - MAX_TASK_LINES} autres")
            push = Push(title=f"Reçus importés : {_plural(len(created), 'nouvelle dépense', 'nouvelles dépenses')}",
                        message="\n".join(lines), tags=["email"], path="#/more/expenses",
                        actions=[("Dépenses", "#/more/expenses")])
            await deliver(state, db, "inbox", push)

        await asyncio.wait_for(go(), INBOX_PUSH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the import already succeeded
        log.warning("inbox push failed: %r", exc)


# --------------------------------------------------------------------------- routes


class PrefsBody(BaseModel):
    prefs: dict[str, bool]


@router.get("/status")
async def notify_status(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    cfg = notifier.load_config(state.settings.secret)
    recent = await db.fetch(
        "SELECT kind, title, status, error, sent_at FROM app_notify_log ORDER BY sent_at DESC LIMIT 20", [], limit=20
    )
    return {
        "configured": cfg.configured,
        "server": cfg.server,
        "topic": cfg.topic,  # secret-ish: only returned to the authenticated PWA, shown behind a reveal toggle
        "subscribe_url": cfg.subscribe_url,
        "token_set": bool(cfg.token),
        "prefs": await get_prefs(db),
        "kinds": list(KINDS),
        "recent": recent,
    }


@router.put("/prefs")
async def set_prefs(body: PrefsBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    unknown = sorted(set(body.prefs) - set(KINDS))
    if unknown:
        raise HTTPException(422, detail=f"unknown kinds: {', '.join(unknown)}")
    for kind, enabled in body.prefs.items():
        await db.execute(
            "INSERT INTO app_notify_prefs (kind, enabled, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(kind) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            [kind, 1 if enabled else 0, now_iso()],
        )
    return {"prefs": await get_prefs(db)}


@router.post("/test")
async def notify_test(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    if not notifier.load_config(state.settings.secret).configured:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"ntfy is not configured: set the vault key {notifier.TOPIC_KEY}")
    push = Push(title="OmniFree : test réussi ✓", message="Les notifications arrivent bien sur ce téléphone. À bientôt !",
                tags=["white_check_mark"], path="#/more/notifications")
    try:
        return await deliver(state, db, "test", push, force=True)
    except notifier.NotifyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/run")
async def notify_run(
    kind: RunKind = Query(...),
    force: bool = Query(False, description="Send even if this kind was already pushed today/this week/month"),
    preview: bool = Query(False, description="Compose the push without sending it"),
    ahead: int = Query(15, ge=5, le=24 * 60, description="reminders: minutes ahead to cover (= the schedule interval)"),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Compose and push one kind (the target of the QStash schedules; the body is ignored)."""
    if not (await get_prefs(db))[kind] and not preview:
        return {"kind": kind, "sent": False, "skipped": "turned off in the app", "title": None, "message": None}
    if kind == "reminders":
        try:
            return await run_reminders(state, db, preview=preview, force=force, ahead=timedelta(minutes=ahead))
        except notifier.NotifyError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    today = local_today()
    budget_alerts: dict[str, str] | None = None
    if kind == "morning":
        push: Push | None = await compose_morning(db, today)
    elif kind == "evening":
        push = await compose_evening(db, today)
    elif kind == "weekly":
        push = await compose_weekly(db, today)
    elif kind == "month":
        push = await compose_month(db, today)
    else:
        try:
            push, budget_alerts = await compose_budget(state, db)
        except TimeoutError as exc:
            raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail="budget report timed out") from exc
    if push is None:
        if budget_alerts is not None and not preview:
            await _state_set(db, "budget_alerts", {"alerts": budget_alerts})  # recovered items can alert again later
        return {"kind": kind, "sent": False, "skipped": "nothing to report", "title": None, "message": None}
    if preview:
        return {"kind": kind, "sent": False, "skipped": "preview", "title": push.title, "message": push.message}
    try:
        out = await deliver(state, db, kind, push, force=force)
    except notifier.NotifyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if budget_alerts is not None and out["sent"]:
        await _state_set(db, "budget_alerts", {"alerts": budget_alerts})
    return out


@public_router.post("/notify/done/{task_id}", include_in_schema=False)
async def notify_done(
    task_id: str,
    sig: str = Query("", max_length=128),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """The "Fait ✓" button of a reminder: marks this one task done. PUBLIC, authenticated by ``sig`` only."""
    token = state.hub_token()
    if not token or len(task_id) > 64 or not hmac.compare_digest(done_sig(token, task_id).encode(), sig.encode()):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
    row = await db.fetch_one("SELECT id, title, done FROM app_tasks WHERE id = ?", [task_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not row.get("done"):
        await db.execute("UPDATE app_tasks SET done = 1, done_at = ? WHERE id = ? AND done = 0", [now_iso(), task_id])
    return {"ok": True, "id": task_id, "title": row["title"], "done": True}
