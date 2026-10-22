"""/money/debts: money lent to and borrowed from people (names only), with partial repayments.

* ``app_debts``: one loan; ``direction`` is ``lent`` (they owe me) or ``borrowed`` (I owe them); ``settled`` is set
  automatically once the repayments cover the amount (and can be set by hand: "forget it");
* ``app_debt_payments``: the repayments of a loan (same currency as the loan).

Routes: ``GET/POST /money/debts``, ``GET/PATCH/DELETE /money/debts/{id}``, ``POST /money/debts/{id}/repay`` and
``DELETE /money/debts/{id}/payments/{payment_id}``. Totals are in MAD (rates of :func:`hub_api.routers.money.get_rates`).

Voice (``POST /money/voice``): the money parser also returns ``lend`` / ``borrow`` entries ("j'ai prêté 200 dh à
Ayoub", "j'ai emprunté 500 à Karim"), recorded as new loans, and ``repaid`` / ``repay`` entries ("Ayoub m'a rendu
100", "j'ai rendu 200 à Karim"), applied to that person's open loans oldest first (:func:`record_voice_debt`).
The same loan or repayment heard again from another recording within :data:`VOICE_DUPLICATE_S` (the memo sent
twice after a timeout) is reported as ``duplicate`` and not stored again; every outcome lists what it stored
(``payment_ids``) so the PWA can undo it.

Reminders (``POST /notify/run?kind=debts``, daily): one push per open loan whose due date is today or was in the last
:data:`DUE_GRACE_DAYS` days (once per loan and due date) and, on Mondays, one push listing the money lent more than
:data:`LENT_STALE_DAYS` days ago and still not returned (:func:`run_debts`).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import Field, field_validator

from hub_api.appdata import AppDB, get_appdb, iso, local_today, new_id, now_iso
from hub_api.merchant_rules import normalise_merchant
from hub_api.routers.expenses import _Fields
from hub_api.state import HubState

__all__ = [
    "router", "SCHEMA", "ensure_schema", "debt_out", "insert_debt", "add_payment", "find_open_debts",
    "record_voice_debt", "compose_debts", "run_debts", "DEBT_ENTRY_TYPES",
]

log = logging.getLogger("omnifree.hub.debts")

router = APIRouter(prefix="/money/debts", tags=["money"])

Direction = Literal["lent", "borrowed"]
# Voice entry types: a new loan, or a repayment of an open one.
DEBT_ENTRY_TYPES = ("lend", "borrow", "repaid", "repay")
LENT_STALE_DAYS = 14
DUE_GRACE_DAYS = 3
MAX_LINES = 8
EPS = 0.005
MAX_AMOUNT = 1e9
VOICE_DUPLICATE_S = 600  # the same voice loan/repayment from another recording within 10 min is the memo sent twice

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_debts ("
    " id TEXT PRIMARY KEY, person TEXT NOT NULL, direction TEXT NOT NULL, amount REAL NOT NULL,"
    " currency TEXT NOT NULL DEFAULT 'MAD', date TEXT NOT NULL, due_date TEXT, note TEXT,"
    " settled INTEGER NOT NULL DEFAULT 0, settled_at TEXT, origin TEXT, audio_key TEXT,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_debts_open ON app_debts (settled, date)",
    "CREATE TABLE IF NOT EXISTS app_debt_payments ("
    " id TEXT PRIMARY KEY, debt_id TEXT NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL, note TEXT,"
    " origin TEXT, audio_key TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_debt_payments_debt ON app_debt_payments (debt_id)",
)


async def ensure_schema(db: AppDB) -> AppDB:
    from hub_api.routers.money import ensure_schema as ensure_money  # money imports this module lazily

    await ensure_money(db)
    await db.ensure("debts", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- rows


def debt_out(row: dict[str, Any], payments: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    today = today or local_today()
    amount = round(float(row.get("amount") or 0), 2)
    paid = round(sum(float(p.get("amount") or 0) for p in payments), 2)
    settled = bool(row.get("settled"))
    due = row.get("due_date")
    try:
        age = (today - date.fromisoformat(str(row["date"])[:10])).days
    except ValueError:
        age = 0
    return {
        "id": row["id"],
        "person": row["person"],
        "direction": row["direction"],
        "amount": amount,
        "currency": row.get("currency") or "MAD",
        "date": row["date"],
        "due_date": due,
        "note": row.get("note"),
        "settled": settled,
        "settled_at": row.get("settled_at"),
        "origin": row.get("origin"),
        "paid": paid,
        "remaining": 0.0 if settled else max(round(amount - paid, 2), 0.0),
        "overdue": bool(due and not settled and due < today.isoformat()),
        "age_days": age,
        "payments": [
            {"id": p["id"], "amount": round(float(p["amount"]), 2), "date": p["date"], "note": p.get("note"),
             "origin": p.get("origin"), "created_at": p["created_at"]}
            for p in sorted(payments, key=lambda p: (p["date"], p["created_at"]))
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _payments_for(db: AppDB, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for i in range(0, len(ids), 80):
        chunk = ids[i : i + 80]
        rows = await db.fetch(
            f"SELECT * FROM app_debt_payments WHERE debt_id IN ({', '.join('?' for _ in chunk)})", chunk
        )
        for r in rows:
            out.setdefault(r["debt_id"], []).append(r)
    return out


async def _load(db: AppDB, where: str = "1 = 1", params: list[Any] | None = None) -> list[dict[str, Any]]:
    rows = await db.fetch(f"SELECT * FROM app_debts WHERE {where} ORDER BY date DESC, created_at DESC", params or [])
    pays = await _payments_for(db, [r["id"] for r in rows])
    today = local_today()
    return [debt_out(r, pays.get(r["id"], []), today) for r in rows]


async def _get(db: AppDB, debt_id: str) -> dict[str, Any]:
    found = await _load(db, "id = ?", [debt_id])
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="debt not found")
    return found[0]


async def insert_debt(db: AppDB, fields: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    row = {
        "id": new_id(),
        "person": str(fields["person"]).strip()[:100],
        "direction": fields["direction"],
        "amount": round(float(fields["amount"]), 2),
        "currency": fields.get("currency") or "MAD",
        "date": fields.get("date") or local_today().isoformat(),
        "due_date": fields.get("due_date"),
        "note": fields.get("note"),
        "settled": 0,
        "settled_at": None,
        "origin": fields.get("origin") or "manual",
        "audio_key": fields.get("audio_key"),
        "created_at": now,
        "updated_at": now,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_debts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return debt_out(row, [])


async def add_payment(
    db: AppDB, debt: dict[str, Any], amount: float, day: str, note: str | None, origin: str, *, audio_key: str | None = None
) -> dict[str, Any]:
    """Record a repayment (``amount`` <= what is left) and settle the loan when it is fully repaid; returns the loan
    with ``payment_id`` set to the new repayment."""
    payment_id = new_id()
    await db.execute(
        "INSERT INTO app_debt_payments (id, debt_id, amount, date, note, origin, audio_key, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [payment_id, debt["id"], round(amount, 2), day, note, origin, audio_key, now_iso()],
    )
    if debt["remaining"] - amount <= EPS:
        await db.execute(
            "UPDATE app_debts SET settled = 1, settled_at = ?, updated_at = ? WHERE id = ?",
            [now_iso(), now_iso(), debt["id"]],
        )
    else:
        await db.execute("UPDATE app_debts SET updated_at = ? WHERE id = ?", [now_iso(), debt["id"]])
    return {**await _get(db, debt["id"]), "payment_id": payment_id}


def same_person(a: str, b: str) -> bool:
    """"Ayoub" matches "ayoub", "Ayoub B." and "Ayoub Benali" (first word); accents and case ignored."""
    x, y = normalise_merchant(a), normalise_merchant(b)
    if not x or not y:
        return False
    return x == y or x.split(" ")[0] == y.split(" ")[0]


async def find_open_debts(db: AppDB, person: str, direction: Direction) -> list[dict[str, Any]]:
    """Open loans with ``person`` in ``direction``, oldest first."""
    rows = await _load(db, "settled = 0 AND direction = ?", [direction])
    exact = [d for d in rows if normalise_merchant(d["person"]) == normalise_merchant(person)]
    hits = exact or [d for d in rows if same_person(d["person"], person)]
    return sorted(hits, key=lambda d: (d["date"], d["created_at"]))


# --------------------------------------------------------------------------- voice


def _recent_cutoff() -> str:
    return iso(datetime.now(timezone.utc) - timedelta(seconds=VOICE_DUPLICATE_S))


async def _voice_duplicate_loan(db: AppDB, entry: dict[str, Any], person: str, audio_key: str | None) -> dict[str, Any] | None:
    """The loan a recent *other* recording already created for this very entry (same person, direction, amount,
    currency and day), else None."""
    direction = "lent" if entry["type"] == "lend" else "borrowed"
    rows = await db.fetch(
        "SELECT id, person, audio_key FROM app_debts WHERE origin = 'voice' AND direction = ? AND amount = ?"
        " AND currency = ? AND date = ? AND created_at >= ?",
        [direction, round(float(entry["amount"]), 2), entry["currency"], entry["date"], _recent_cutoff()],
    )
    for r in rows:
        if r.get("audio_key") != audio_key and normalise_merchant(r["person"]) == normalise_merchant(person):
            return await _get(db, r["id"])
    return None


async def _voice_duplicate_repayment(
    db: AppDB, entry: dict[str, Any], person: str, direction: Direction, audio_key: str | None
) -> list[dict[str, Any]] | None:
    """The loans a recent *other* recording already repaid with this very entry (same person, direction, total
    amount and day), else None."""
    rows = await db.fetch(
        "SELECT p.debt_id, p.amount, p.audio_key, d.person FROM app_debt_payments p JOIN app_debts d ON d.id = p.debt_id"
        " WHERE p.origin = 'voice' AND p.date = ? AND p.created_at >= ? AND d.direction = ? AND d.currency = ?",
        [entry["date"], _recent_cutoff(), direction, entry["currency"]],
    )
    by_audio: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("audio_key") and r["audio_key"] != audio_key and same_person(r["person"], person):
            by_audio.setdefault(r["audio_key"], []).append(r)
    for group in by_audio.values():
        if abs(sum(float(r["amount"]) for r in group) - float(entry["amount"])) <= EPS:
            return [await _get(db, i) for i in dict.fromkeys(r["debt_id"] for r in group)]
    return None


async def record_voice_debt(db: AppDB, entry: dict[str, Any], *, audio_key: str | None) -> dict[str, Any]:
    """One parsed voice entry of type lend/borrow/repaid/repay -> {"action", "person", "amount", "currency", "debts",
    "payment_ids", "applied", "unapplied"}. A repayment without a matching open loan is reported as ``unmatched``
    (nothing stored); the same entry already stored by another recording a moment ago as ``duplicate``."""
    await ensure_schema(db)
    typ = entry["type"]
    person = entry.get("person") or entry.get("merchant") or ""
    base = {"type": typ, "person": person, "amount": entry["amount"], "currency": entry["currency"], "payment_ids": []}
    if typ in ("lend", "borrow"):
        same = await _voice_duplicate_loan(db, entry, person, audio_key)
        if same is not None:
            return {**base, "action": "duplicate", "debts": [same], "applied": 0.0, "unapplied": 0.0}
        debt = await insert_debt(
            db,
            {"person": person, "direction": "lent" if typ == "lend" else "borrowed", "amount": entry["amount"],
             "currency": entry["currency"], "date": entry["date"], "note": entry.get("note"), "origin": "voice",
             "audio_key": audio_key},
        )
        return {**base, "action": "created", "debts": [debt], "applied": 0.0, "unapplied": 0.0}
    direction: Direction = "lent" if typ == "repaid" else "borrowed"
    repaid_before = await _voice_duplicate_repayment(db, entry, person, direction, audio_key)
    if repaid_before is not None:
        return {**base, "action": "duplicate", "debts": repaid_before, "applied": 0.0, "unapplied": 0.0}
    left = float(entry["amount"])
    touched: list[dict[str, Any]] = []
    payment_ids: list[str] = []
    for debt in await find_open_debts(db, person, direction):
        if left <= EPS:
            break
        if debt["currency"] != entry["currency"]:
            continue
        part = min(left, debt["remaining"])
        if part <= EPS:
            continue
        paid = await add_payment(db, debt, part, entry["date"], entry.get("note"), "voice", audio_key=audio_key)
        payment_ids.append(paid.pop("payment_id"))
        touched.append(paid)
        left -= part
    applied = round(float(entry["amount"]) - left, 2)
    action = "repaid" if touched else "unmatched"
    return {**base, "action": action, "debts": touched, "payment_ids": payment_ids, "applied": applied,
            "unapplied": round(max(left, 0.0), 2)}


# --------------------------------------------------------------------------- models


class _DebtFields(_Fields):
    @field_validator("person", check_fields=False)
    @classmethod
    def _person(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("person cannot be empty")
        return v

    @field_validator("due_date", check_fields=False)
    @classmethod
    def _due(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError as exc:
            raise ValueError("due_date must be YYYY-MM-DD") from exc


class DebtCreate(_DebtFields):
    person: str = Field(min_length=1, max_length=100)
    direction: Direction
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    currency: str = "MAD"
    date: str | None = None  # default: today (Africa/Casablanca)
    due_date: str | None = None
    note: str | None = Field(None, max_length=2000)


class DebtPatch(_DebtFields):
    person: str | None = Field(None, min_length=1, max_length=100)
    direction: Direction | None = None
    amount: float | None = Field(None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    currency: str | None = None
    date: str | None = None
    due_date: str | None = None
    note: str | None = Field(None, max_length=2000)
    settled: bool | None = None


class RepayBody(_Fields):
    amount: float | None = Field(None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False, description="Default: everything that is left")
    date: str | None = None
    note: str | None = Field(None, max_length=2000)


# --------------------------------------------------------------------------- routes


async def summarise_debts(db: AppDB, debts: list[dict[str, Any]]) -> dict[str, Any]:
    """Open totals in MAD: what people owe me, what I owe, and the net per person."""
    from hub_api.routers.money import get_rates  # money imports this module lazily

    rates = await get_rates(db)
    lent = borrowed = 0.0
    people: dict[str, dict[str, Any]] = {}
    unconverted: set[str] = set()
    for d in debts:
        if d["settled"] or d["remaining"] <= 0:
            continue
        mad = rates.convert(d["remaining"], d["currency"])
        if mad is None:
            unconverted.add(d["currency"])
            continue
        key = normalise_merchant(d["person"])
        p = people.setdefault(key, {"person": d["person"], "owes_me": 0.0, "i_owe": 0.0, "net": 0.0, "count": 0})
        p["count"] += 1
        if d["direction"] == "lent":
            lent += mad
            p["owes_me"] += mad
        else:
            borrowed += mad
            p["i_owe"] += mad
    by_person = []
    for p in people.values():
        p.update(owes_me=round(p["owes_me"], 2), i_owe=round(p["i_owe"], 2), net=round(p["owes_me"] - p["i_owe"], 2))
        by_person.append(p)
    by_person.sort(key=lambda p: (-abs(p["net"]), p["person"].lower()))
    return {"currency": "MAD", "owed_to_me": round(lent, 2), "i_owe": round(borrowed, 2),
            "net": round(lent - borrowed, 2), "by_person": by_person, "unconverted": sorted(unconverted),
            "fx": rates.public()}


@router.get("")
async def list_debts(
    status_: Literal["open", "settled", "all"] = Query("open", alias="status"),
    person: str | None = Query(None, max_length=100),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    where = {"open": "settled = 0", "settled": "settled = 1", "all": "1 = 1"}[status_]
    debts = await _load(db, where)
    if person:
        debts = [d for d in debts if same_person(d["person"], person)]
    open_debts = debts if status_ == "open" else await _load(db, "settled = 0")  # the totals are always the open loans
    return {"debts": debts, "summary": await summarise_debts(db, open_debts)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_debt(body: DebtCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    fields = body.model_dump()
    fields["origin"] = "manual"
    if fields["due_date"] and fields["date"] and fields["due_date"] < fields["date"]:
        raise HTTPException(422, detail="due_date must not be before the loan date")
    return await insert_debt(db, fields)


@router.get("/{debt_id}")
async def get_debt(debt_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return await _get(db, debt_id)


@router.patch("/{debt_id}")
async def patch_debt(debt_id: str, body: DebtPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    current = await _get(db, debt_id)
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in ("due_date", "note"):
            raise HTTPException(422, detail=f"{field} cannot be null")
        changes[field] = value
    if "amount" in changes and changes["amount"] + EPS < current["paid"]:
        raise HTTPException(422, detail=f"amount cannot be less than what was already repaid ({current['paid']})")
    amount = changes.get("amount", current["amount"])
    # Settled by its repayments (as opposed to "forget the rest", which stays settled whatever is edited).
    repaid_in_full = current["paid"] > 0 and current["amount"] - current["paid"] <= EPS
    if "settled" in changes:
        if changes["settled"] == current["settled"]:
            changes.pop("settled")  # the PWA sends the whole loan back: nothing to change
        elif not changes["settled"] and amount - current["paid"] <= EPS:
            raise HTTPException(422, detail="this loan is fully repaid: remove a repayment to open it again")
        else:
            changes["settled_at"] = now_iso() if changes["settled"] else None
            changes["settled"] = 1 if changes["settled"] else 0
    if "settled" not in changes and "amount" in changes:
        if not current["settled"] and amount - current["paid"] <= EPS:
            changes.update(settled=1, settled_at=now_iso())  # lowered to what was repaid: settled
        elif current["settled"] and repaid_in_full and amount - current["paid"] > EPS:
            changes.update(settled=0, settled_at=None)  # raised above what was repaid: open again
    day, due = changes.get("date", current["date"]), changes.get("due_date", current["due_date"])
    if due and day and due < day:
        raise HTTPException(422, detail="due_date must not be before the loan date")
    if changes:
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_debts SET {sets} WHERE id = ?", [*changes.values(), debt_id])
    return await _get(db, debt_id)


@router.delete("/{debt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_debt(debt_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, debt_id)
    await db.execute("DELETE FROM app_debt_payments WHERE debt_id = ?", [debt_id])
    await db.execute("DELETE FROM app_debts WHERE id = ?", [debt_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{debt_id}/repay", status_code=status.HTTP_201_CREATED)
async def repay_debt(debt_id: str, body: RepayBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """A (partial) repayment; the loan is settled once nothing is left."""
    debt = await _get(db, debt_id)
    if debt["settled"]:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="this debt is already settled")
    amount = body.amount if body.amount is not None else debt["remaining"]
    if debt["remaining"] <= EPS:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="nothing is left to repay on this debt")
    if amount - debt["remaining"] > EPS:
        raise HTTPException(422, detail=f"only {debt['remaining']} {debt['currency']} is left to repay")
    return await add_payment(db, debt, amount, body.date or local_today().isoformat(), body.note, "manual")


@router.delete("/{debt_id}/payments/{payment_id}")
async def delete_payment(debt_id: str, payment_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Remove a repayment (a mistake); a loan settled by its repayments opens again (one settled by hand, "forget the
    rest", stays settled)."""
    before = await _get(db, debt_id)
    repaid_in_full = before["amount"] - before["paid"] <= EPS
    gone = await db.execute("DELETE FROM app_debt_payments WHERE id = ? AND debt_id = ?", [payment_id, debt_id])
    if not gone:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="payment not found")
    debt = await _get(db, debt_id)
    if debt["settled"] and repaid_in_full and debt["amount"] - debt["paid"] > EPS:
        await db.execute("UPDATE app_debts SET settled = 0, settled_at = NULL, updated_at = ? WHERE id = ?",
                         [now_iso(), debt_id])
        debt = await _get(db, debt_id)
    return debt


# --------------------------------------------------------------------------- reminders (notify kind "debts")


def _weeks(days: int) -> str:
    return f"{days} j" if days < 14 else f"{days // 7} sem."


async def compose_debts(db: AppDB, today: date) -> list[Any]:
    """The pushes for today: due-date reminders (any day) and, on Mondays, the stale money lent."""
    from hub_api.routers.notify import Push, fmt_money  # notify imports this module lazily

    await ensure_schema(db)
    open_debts = await _load(db, "settled = 0")
    pushes: list[Any] = []
    first_due = (today - timedelta(days=DUE_GRACE_DAYS)).isoformat()
    for d in sorted(open_debts, key=lambda d: (d["due_date"] or "", d["person"])):
        if not d["due_date"] or not (first_due <= d["due_date"] <= today.isoformat()) or d["remaining"] <= 0:
            continue
        left = fmt_money(d["remaining"], d["currency"])
        line = ("Échéance aujourd'hui." if d["due_date"] == today.isoformat()
                else f"En retard : c'était prévu le {d['due_date'][8:10]}/{d['due_date'][5:7]}.")
        if d["direction"] == "lent":
            title = f"💸 {d['person']} doit te rendre {left}"
        else:
            title = f"💸 Tu dois rendre {left} à {d['person']}"
        if d.get("note"):
            line += f"\n{str(d['note'])[:200]}"
        pushes.append(Push(title=title[:250], message=line, tags=["money_with_wings"], priority=4,
                           path="#/more/money-debts", actions=[("Dettes", "#/more/money-debts")],
                           dedupe=f"debt-due:{d['id']}:{d['due_date']}"))
    if today.weekday() == 0:
        cutoff = (today - timedelta(days=LENT_STALE_DAYS)).isoformat()
        stale = [d for d in open_debts if d["direction"] == "lent" and d["date"] <= cutoff and d["remaining"] > 0]
        stale.sort(key=lambda d: d["date"])
        if stale:
            totals: dict[str, float] = {}
            for d in stale:
                totals[d["currency"]] = totals.get(d["currency"], 0.0) + d["remaining"]
            lines = [f"• {d['person']} : {fmt_money(d['remaining'], d['currency'])} (depuis {_weeks(d['age_days'])})"
                     for d in stale[:MAX_LINES]]
            if len(stale) > MAX_LINES:
                lines.append(f"… et {len(stale) - MAX_LINES} autres")
            total = ", ".join(fmt_money(v, k) for k, v in sorted(totals.items(), key=lambda kv: -kv[1]))
            pushes.append(Push(title=f"On te doit encore {total}", message="\n".join(lines), tags=["handshake"],
                               path="#/more/money-debts", actions=[("Dettes", "#/more/money-debts")],
                               dedupe=f"debts-week:{today.isoformat()}"))
    return pushes


async def run_debts(state: HubState, db: AppDB, *, preview: bool, force: bool, today: date | None = None) -> dict[str, Any]:
    """What ``POST /notify/run?kind=debts`` does (same result shape as the reminders run)."""
    from hub_api import notifier
    from hub_api.routers.notify import deliver

    pushes = await compose_debts(db, today or local_today())
    out: dict[str, Any] = {"kind": "debts", "sent": False, "skipped": None, "count": 0, "title": None,
                           "message": None, "results": []}
    if not pushes:
        out["skipped"] = "nothing to report"
        return out
    out["title"] = pushes[0].title if len(pushes) == 1 else f"{len(pushes)} rappels de dettes"
    out["message"] = "\n".join(f"{p.title} · {p.message.splitlines()[0]}" for p in pushes)
    if preview:
        out["skipped"] = "preview"
        return out
    errors: list[Exception] = []
    for push in pushes:
        try:
            res = await deliver(state, db, "debts", push, force=force)
        except notifier.NotifyError as exc:  # keep going; the caller answers 502 so QStash retries (sent ones dedupe)
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
