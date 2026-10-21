"""/digest: a weekly or monthly summary shown in the app (spending, tasks, notes, uploads, LLM use).

The numbers come from the apps database (expenses, tasks, notes), a listing of the Drive and the
router's request counters; one LLM call turns them into a short markdown summary, highlights and
suggestions (a deterministic template is used when no LLM answers). The result is cached in D1
per period for an hour; ``?refresh=1`` regenerates it.

Periods (Africa/Casablanca days): ``week`` = the last 7 days including today, ``month`` = the
current calendar month up to today.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from core.interfaces import ChatMessage, ChatRequest, ProviderKind
from hub_api.appdata import AppDB, extract_json, get_appdb, iso, local_today, local_tz, now_iso
from hub_api.routers.expenses import ensure_schema as ensure_expenses
from hub_api.routers.expenses import expense_out, summarise
from hub_api.routers.notes import SCHEMA as NOTES_SCHEMA
from hub_api.state import HubState, get_state

__all__ = ["router", "SCHEMA", "period_range", "fallback_text", "top_merchants"]

log = logging.getLogger("omnifree.hub.digest")

router = APIRouter(tags=["digest"])

Period = Literal["week", "month"]
CACHE_TTL_S = 3600
LLM_TIMEOUT_S = 25.0
DRIVE_TIMEOUT_S = 15.0
COUNTER_TIMEOUT_S = 8.0

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_digest_cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, generated_at TEXT NOT NULL)",
)


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await ensure_expenses(db)
    await db.ensure("notes", NOTES_SCHEMA)
    await db.ensure("digest", SCHEMA)
    return db


# --------------------------------------------------------------------------- pure helpers


def period_range(period: str, today: date) -> tuple[date, date]:
    if period == "month":
        return today.replace(day=1), today
    return today - timedelta(days=6), today


def _utc_start(day: date) -> str:
    """ISO UTC timestamp of local midnight starting ``day`` (timestamps in D1 are UTC)."""
    return iso(datetime.combine(day, time(0), local_tz()))


def top_merchants(expenses: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for e in expenses:
        merchant = (e.get("merchant") or "Unknown").strip() or "Unknown"
        key = (merchant.lower(), e["currency"])
        g = groups.setdefault(key, {"merchant": merchant, "currency": e["currency"], "total": 0.0, "count": 0})
        g["total"] += float(e["total"])
        g["count"] += 1
    rows = sorted(groups.values(), key=lambda g: (-g["total"], g["merchant"]))
    return [{**g, "total": round(g["total"], 2)} for g in rows[:limit]]


def _fmt_money(amount: float, currency: str) -> str:
    return f"{amount:,.2f} {currency}".replace(",", " ")


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fallback_text(period: str, start: date, end: date, stats: dict[str, Any]) -> dict[str, Any]:
    """Deterministic markdown/highlights/suggestions when no LLM is available."""
    label = "This week" if period == "week" else "This month"
    exp, tasks, notes, drive = stats["expenses"], stats["tasks"], stats["notes"], stats["drive"]
    totals = exp["total_by_currency"]
    spent = ", ".join(_fmt_money(v, k) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])) or "nothing"
    highlights: list[str] = []
    if exp["count"]:
        main = max(totals, key=lambda c: totals[c])
        cats = sorted(((c, m.get(main, 0.0)) for c, m in exp["by_category"].items()), key=lambda kv: -kv[1])
        if cats and cats[0][1] > 0:
            highlights.append(f"Most spending went to {cats[0][0]} ({_fmt_money(cats[0][1], main)}).")
        if exp["top_merchants"]:
            t = exp["top_merchants"][0]
            highlights.append(f"Top merchant: {t['merchant']} ({_fmt_money(t['total'], t['currency'])}, {t['count']}x).")
    if tasks["done"]:
        highlights.append(f"{tasks['done']} task{'s' if tasks['done'] != 1 else ''} done.")
    if notes["created"]:
        highlights.append(f"{notes['created']} note{'s' if notes['created'] != 1 else ''} written ({notes['voice']} by voice).")
    if drive["uploaded_files"]:
        highlights.append(f"{drive['uploaded_files']} file{'s' if drive['uploaded_files'] != 1 else ''} uploaded ({_fmt_bytes(drive['uploaded_bytes'])}).")
    if not highlights:
        highlights.append("A quiet period: nothing recorded yet.")
    suggestions: list[str] = []
    if tasks["overdue"]:
        suggestions.append(f"Clear or reschedule the {tasks['overdue']} overdue task{'s' if tasks['overdue'] != 1 else ''}.")
    if tasks["open"] > 15:
        suggestions.append("The task list is long: archive what no longer matters.")
    if not exp["count"]:
        suggestions.append("Scan receipts or import them from Gmail to track spending.")
    elif "other" in (exp.get("by_category") or {}):
        suggestions.append("Give the expenses filed under \"other\" a real category to see where the money goes.")
    if (drive.get("uploaded_files") or 0) >= 5:
        suggestions.append(f"Sort the {drive['uploaded_files']} new Drive files into folders while you still remember them.")
    if not suggestions:
        suggestions.append("Keep it up: capture receipts and notes as they come.")
    markdown = (
        f"## {label}\n\n"
        f"From {start.isoformat()} to {end.isoformat()}: you spent **{spent}** over {exp['count']} expense"
        f"{'s' if exp['count'] != 1 else ''}, finished {tasks['done']} task{'s' if tasks['done'] != 1 else ''} "
        f"({tasks['open']} open, {tasks['overdue']} overdue) and wrote {notes['created']} note"
        f"{'s' if notes['created'] != 1 else ''}."
    )
    return {"markdown": markdown, "highlights": highlights, "suggestions": suggestions}


_PROMPT = (
    "You write a short personal {label} summary for a phone app from usage statistics (JSON below). "
    "Be concrete, friendly and brief; use the numbers, never invent any. Currency amounts keep their ISO codes. "
    "Answer with ONE JSON object only, no code fences: "
    '{{"markdown": "markdown, 2-4 short paragraphs or bullets, may start with a ## heading", '
    '"highlights": ["3-5 one-line facts"], "suggestions": ["2-3 short actionable tips"]}}. '
    "Suggestions are only about the owner's money (spending, categories, merchants, receipts) and their Drive "
    "(organising, naming, cleaning up or grouping files); add one about tasks only when some are overdue. "
    "Never suggest anything about LLM providers, AI requests or quotas: llm stats are for the summary only."
)


def _clean_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:300] for v in value if isinstance(v, (str, int, float)) and str(v).strip()][:limit]


# --------------------------------------------------------------------------- stats


async def _expense_stats(db: AppDB, start: date, end: date) -> dict[str, Any]:
    rows = await db.fetch(
        "SELECT * FROM app_expenses WHERE date >= ? AND date <= ? ORDER BY date DESC", [start.isoformat(), end.isoformat()]
    )
    expenses = [expense_out(r) for r in rows]
    out = summarise(expenses)
    out["top_merchants"] = top_merchants(expenses)
    out["from_email"] = sum(1 for e in expenses if e.get("source") == "email")
    return out


async def _task_stats(db: AppDB, start: date, today: date) -> dict[str, int]:
    since = _utc_start(start)
    done = await db.fetch_one("SELECT COUNT(*) AS n FROM app_tasks WHERE done = 1 AND done_at >= ?", [since])
    open_ = await db.fetch_one("SELECT COUNT(*) AS n FROM app_tasks WHERE done = 0", [])
    overdue = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM app_tasks WHERE done = 0 AND due IS NOT NULL AND due < ?", [today.isoformat()]
    )
    return {
        "done": int((done or {}).get("n") or 0),
        "open": int((open_ or {}).get("n") or 0),
        "overdue": int((overdue or {}).get("n") or 0),
    }


async def _note_stats(db: AppDB, start: date) -> dict[str, int]:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS created, SUM(CASE WHEN source = 'voice' THEN 1 ELSE 0 END) AS voice"
        " FROM app_notes WHERE created_at >= ?",
        [_utc_start(start)],
    )
    row = row or {}
    return {"created": int(row.get("created") or 0), "voice": int(row.get("voice") or 0)}


async def _drive_stats(state: HubState, start: date) -> dict[str, Any]:
    """Objects modified since the period start, across every storage backend (unreachable ones are skipped)."""
    since = datetime.combine(start, time(0), local_tz()).astimezone(timezone.utc)
    try:
        listing = await asyncio.wait_for(state.router.storage_list("", limit=10_000), DRIVE_TIMEOUT_S)
    except Exception as exc:
        log.info("drive listing for the digest failed: %r", exc)
        return {"uploaded_files": None, "uploaded_bytes": None}
    files, size = 0, 0
    for objs in listing.values():
        if isinstance(objs, str):
            continue
        for o in objs:
            modified = o.last_modified
            if modified is None:
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            if modified >= since:
                files += 1
                size += int(o.size or 0)
    return {"uploaded_files": files, "uploaded_bytes": size}


async def _llm_stats(state: HubState, period: str, start: date, end: date) -> dict[str, Any]:
    """Successful LLM requests per provider from the router counters (UTC day/month windows)."""
    quota = state.router.quota
    names = [s.name for s in state.registry.list(ProviderKind.LLM, enabled_only=True)]

    async def one(name: str) -> tuple[str, float]:
        if period == "month":
            now = datetime.combine(end, time(12), timezone.utc)
            return name, await quota.used(name, "requests", "month", now=now)
        days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
        used = await asyncio.gather(
            *(quota.used(name, "requests", "day", now=datetime.combine(d, time(12), timezone.utc)) for d in days)
        )
        return name, float(sum(used))

    try:
        pairs = await asyncio.wait_for(asyncio.gather(*(one(n) for n in names)), COUNTER_TIMEOUT_S)
    except Exception as exc:
        log.info("llm counters for the digest failed: %r", exc)
        pairs = []
    by_provider = {name: int(n) for name, n in pairs if n}
    return {"requests_by_provider": dict(sorted(by_provider.items(), key=lambda kv: -kv[1]))}


async def _write_up(state: HubState, period: str, start: date, end: date, stats: dict[str, Any]) -> dict[str, Any]:
    label = "weekly" if period == "week" else "monthly"
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_PROMPT.format(label=label)),
            ChatMessage(
                role="user",
                content=json.dumps({"period": period, "from": start.isoformat(), "to": end.isoformat(), "stats": stats}),
            ),
        ],
        temperature=0.4,
        max_tokens=900,
        response_format={"type": "json_object"},
    )
    try:
        resp = await asyncio.wait_for(state.router.chat(request), LLM_TIMEOUT_S)
        data = extract_json(resp.content) or {}
        markdown = data.get("markdown")
        highlights = _clean_list(data.get("highlights"), 6)
        suggestions = _clean_list(data.get("suggestions"), 4)
        if isinstance(markdown, str) and markdown.strip() and highlights:
            return {
                "markdown": markdown.strip()[:6000],
                "highlights": highlights,
                "suggestions": suggestions,
                "source": "llm",
                "model": resp.routed_via or resp.model,
            }
        log.info("digest LLM answer unusable, using the template")
    except Exception as exc:
        log.info("digest LLM call failed (%r), using the template", exc)
    return {**fallback_text(period, start, end, stats), "source": "template", "model": None}


async def build_digest(state: HubState, db: AppDB, period: str, today: date) -> dict[str, Any]:
    start, end = period_range(period, today)
    expenses, tasks, notes, drive, llm = await asyncio.gather(
        _expense_stats(db, start, end),
        _task_stats(db, start, today),
        _note_stats(db, start),
        _drive_stats(state, start),
        _llm_stats(state, period, start, end),
    )
    stats = {"expenses": expenses, "tasks": tasks, "notes": notes, "drive": drive, "llm": llm}
    text = await _write_up(state, period, start, end, stats)
    return {
        "period": period,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "generated_at": now_iso(),
        "markdown": text["markdown"],
        "stats": stats,
        "highlights": text["highlights"],
        "suggestions": text["suggestions"],
        "source": text["source"],
        "model": text["model"],
        "cached": False,
    }


def _fresh(generated_at: str, now: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return 0 <= (now - stamp).total_seconds() < CACHE_TTL_S


# --------------------------------------------------------------------------- route


@router.get("/digest")
async def digest(
    period: Period = Query("week"),
    refresh: bool = Query(False),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    today = local_today()
    start, _end = period_range(period, today)
    key = f"{period}:{start.isoformat()}:{today.isoformat()}"
    if not refresh:
        row = await db.fetch_one("SELECT payload, generated_at FROM app_digest_cache WHERE key = ?", [key])
        if row and _fresh(row["generated_at"], datetime.now(timezone.utc)):
            try:
                cached = json.loads(row["payload"])
            except ValueError:
                cached = None
            if isinstance(cached, dict):
                cached["cached"] = True
                return cached
    result = await build_digest(state, db, period, today)
    await db.execute(
        "INSERT INTO app_digest_cache (key, payload, generated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, generated_at = excluded.generated_at",
        [key, json.dumps(result, ensure_ascii=False), result["generated_at"]],
    )
    # Old period keys are useless after a day: keep the table tiny.
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=2))
    await db.execute("DELETE FROM app_digest_cache WHERE generated_at < ?", [cutoff])
    return result
