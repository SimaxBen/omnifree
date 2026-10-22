"""/money/splits: shared expenses between friends (Tricount-like). Members are names, not accounts.

* ``app_split_groups``: a group (trip, flat share...) in one currency, with its members as JSON
  ``[{"id", "name", "me"}]``; exactly one member is the owner (``me``);
* ``app_split_expenses``: what was paid in the group: ``kind = 'expense'`` (paid by one member, split equally between
  some members, by custom amounts or by custom percentages) or ``kind = 'settlement'`` (member A paid member B back).

Balances: a member's net is what they paid minus their shares (positive: the group owes them).

**The owner's spending rule.** Every shared expense the owner has a share of is also recorded in ``app_expenses``
for that share only (``source = 'split'``, ``source_ref`` = the shared expense id), whoever paid: the owner's share
is what the owner consumed, the rest is the others' spending. When the owner paid, the friends' part is money they
owe back; when a friend paid, the owner's share is a debt to that friend. Either way the money that changes hands
later is a settlement, a transfer that is never an expense, so nothing is counted twice. The mirror is kept in sync
when the shared expense changes and removed with it.

**Split <-> Debts.** The group is the single source of truth; the Debts page shows *derived* rows of ``app_debts``
(``origin = 'split'``, ``split_group_id``, ``split_member_id``): one per group and other member, holding the net
between the owner and that member in that group, built only from what links them directly (expenses one of them
paid that the other has a share of, and the settlements between them). "Ayoub owes me 20 for dinner, I owe him 5
for the taxi" is one row "Ayoub owes me 15" with both lines in ``split_detail``. The settlements between the owner
and that member are the row's repayments (``app_debt_payments`` with the settlement's id), so a repayment recorded
on the Debts page is a settlement here (:func:`settle_linked_debt`) and removing it there deletes the settlement
(:func:`delete_linked_payment`). :func:`sync_group_debts` rebuilds a group's rows after every change; it is
idempotent, and each group carries a revision (``rev``, bumped *before* a change) and the revision its rows were
built from (``debts_rev``), so a change interrupted half-way is repaired on the next read (:func:`heal_split_debts`).
An edit or deletion that would leave a member repaid more than they owe (a repaid dinner deleted) is refused with
409: the repayment has to be removed first, it never silently turns into money owed the other way. The settle-up plan starts with these owner pairs
(so "Settle" on either page records the same thing) and pairs the other members among themselves.

Amounts are handled in cents: the amount and every share are rounded once, so the shares always add up to the
amount and the balances always sum to zero; the cents left over by an equal or percentage split go to the payer
when they have a share, else to the last member who has one.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from hub_api.appdata import AppDB, get_appdb, iso, local_today, new_id, now_iso
from hub_api.merchant_rules import normalise_merchant
from hub_api.routers.debts import _load as load_debts
from hub_api.routers.debts import ensure_schema as ensure_debts
from hub_api.routers.debts import same_person
from hub_api.routers.expenses import Category, _Fields, insert_expense
from hub_api.routers.expenses import ensure_schema as ensure_expenses

__all__ = [
    "router", "SCHEMA", "ensure_schema", "equal_shares", "percent_shares", "compute_balances", "settle_up",
    "owner_share", "pair_balances", "pair_debt", "sync_group_debts", "heal_split_debts", "settle_linked_debt",
    "delete_linked_payment", "record_voice_split",
]

router = APIRouter(prefix="/money/splits", tags=["money"])

SplitMode = Literal["equal", "custom", "percent"]
EPS = 0.005
MAX_MEMBERS = 30
MAX_DETAIL_ITEMS = 60
VOICE_DUPLICATE_S = 600

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_split_groups ("
    " id TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'MAD', members TEXT NOT NULL,"
    " archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_split_expenses ("
    " id TEXT PRIMARY KEY, group_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'expense', date TEXT NOT NULL,"
    " description TEXT NOT NULL DEFAULT '', amount REAL NOT NULL, paid_by TEXT NOT NULL, split TEXT NOT NULL DEFAULT 'equal',"
    " shares TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'other', note TEXT, expense_id TEXT,"
    " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_split_expenses_group ON app_split_expenses (group_id, date)",
)
# Migration v2: the revision of a group (bumped before every change) and the one its Debts rows were built from;
# a percentage split keeps its percentages; voice entries keep their recording (duplicates, undo).
GROUP_COLUMNS = (("rev", "INTEGER NOT NULL DEFAULT 0"), ("debts_rev", "INTEGER"))
ENTRY_COLUMNS = (("percents", "TEXT"), ("origin", "TEXT"), ("audio_key", "TEXT"))


async def ensure_schema(db: AppDB) -> AppDB:
    await ensure_expenses(db)
    await ensure_debts(db)
    await db.ensure("splits", SCHEMA)
    await db.add_columns("splits_v2_groups", "app_split_groups", GROUP_COLUMNS)
    await db.add_columns("splits_v2_entries", "app_split_expenses", ENTRY_COLUMNS)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- pure maths (in cents)


def _cents(x: float) -> int:
    return int(round(x * 100))


def _money(x: float) -> float:
    """``x`` rounded to the cent the same way the shares are (one rounding everywhere)."""
    return _cents(x) / 100


def _allocate(total: int, weights: dict[str, int], payer: str | None) -> dict[str, int]:
    """``total`` cents split in proportion to ``weights`` (rounded down); the cents left over go to ``payer`` when
    they have a share, else to the last member with one (``weights`` is in member order)."""
    ids = [m for m, w in weights.items() if w > 0]
    if not ids:
        return {}
    weight = sum(weights[m] for m in ids)
    out = {m: total * weights[m] // weight for m in ids}
    left = total - sum(out.values())
    if left:
        out[payer if payer in out else ids[-1]] += left
    return out


def equal_shares(amount: float, member_ids: list[str], payer: str | None = None) -> dict[str, float]:
    """``amount`` split equally; the leftover cents go to the payer (else the last member)."""
    return {m: c / 100 for m, c in _allocate(_cents(amount), {m: 1 for m in member_ids}, payer).items()}


def _basis_points(pct: float) -> int:
    return int(round(pct * 100))


def percent_shares(amount: float, percents: dict[str, float], payer: str | None = None) -> dict[str, float]:
    """``amount`` split by percentages (to 0.01 %, adding up to 100); leftover cents as :func:`equal_shares`."""
    return {m: c / 100 for m, c in _allocate(_cents(amount), {m: _basis_points(p) for m, p in percents.items()}, payer).items()}


def compute_balances(member_ids: list[str], entries: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Per member: ``paid`` (expenses paid + money paid back), ``share`` (expense shares + money received back) and
    ``net = paid - share`` (positive: the others owe them)."""
    paid = {m: 0 for m in member_ids}
    share = {m: 0 for m in member_ids}
    for e in entries:
        paid[e["paid_by"]] = paid.get(e["paid_by"], 0) + _cents(e["amount"])
        for m, v in e["shares"].items():
            share[m] = share.get(m, 0) + _cents(v)
    return {m: {"paid": paid[m] / 100, "share": share[m] / 100, "net": (paid[m] - share[m]) / 100} for m in paid}


def settle_up(nets: dict[str, float]) -> list[dict[str, Any]]:
    """Payments that bring every net to zero: the biggest debtor pays the biggest creditor, repeatedly."""
    debtors = sorted(((m, -_cents(v)) for m, v in nets.items() if _cents(v) < 0), key=lambda kv: (-kv[1], kv[0]))
    creditors = sorted(((m, _cents(v)) for m, v in nets.items() if _cents(v) > 0), key=lambda kv: (-kv[1], kv[0]))
    out: list[dict[str, Any]] = []
    i = j = 0
    debt = [d for _m, d in debtors]
    cred = [c for _m, c in creditors]
    while i < len(debtors) and j < len(creditors):
        pay = min(debt[i], cred[j])
        if pay > 0:
            out.append({"from": debtors[i][0], "to": creditors[j][0], "amount": pay / 100})
        debt[i] -= pay
        cred[j] -= pay
        if debt[i] == 0:
            i += 1
        if cred[j] == 0:
            j += 1
    return out


def owner_share(group: dict[str, Any], entry: dict[str, Any]) -> float:
    """The owner's share of a shared expense, whoever paid it; 0 for a settlement (a transfer, not spending)."""
    me = _me(group)
    if entry.get("kind") != "expense":
        return 0.0
    return round(float(entry["shares"].get(me, 0.0)), 2)


def _settled_to(entry: dict[str, Any]) -> str:
    return next(iter(entry["shares"]), "")


def pair_balances(group: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Between the owner and each other member, from what links them directly (in cents): ``owes_me`` (their shares
    of what I paid), ``i_owe`` (my shares of what they paid), ``paid_me`` / ``i_paid`` (settlements between us),
    ``base = owes_me - i_owe`` and ``net = base - paid_me + i_paid`` (positive: they owe me), with the lines."""
    me = _me(group)
    out: dict[str, dict[str, Any]] = {
        m["id"]: {"member": m["id"], "name": m["name"], "owes_me": 0, "i_owe": 0, "paid_me": 0, "i_paid": 0, "items": []}
        for m in group["members"] if not m.get("me")
    }
    for e in sorted(entries, key=lambda e: (e["date"], e["created_at"], e["id"])):
        line = {"entry_id": e["id"], "date": e["date"], "kind": e["kind"], "description": e["description"],
                "total": e["amount"]}
        if e["kind"] == "expense":
            if e["paid_by"] == me:
                for m, v in e["shares"].items():
                    if m in out and _cents(v) > 0:
                        out[m]["owes_me"] += _cents(v)
                        out[m]["items"].append({**line, "effect": _money(v)})
            elif e["paid_by"] in out and _cents(e["shares"].get(me, 0)) > 0:
                v = e["shares"][me]
                out[e["paid_by"]]["i_owe"] += _cents(v)
                out[e["paid_by"]]["items"].append({**line, "effect": -_money(v)})
        else:
            to = _settled_to(e)
            if e["paid_by"] == me and to in out:
                out[to]["i_paid"] += _cents(e["amount"])
                out[to]["items"].append({**line, "effect": e["amount"]})
            elif to == me and e["paid_by"] in out:
                out[e["paid_by"]]["paid_me"] += _cents(e["amount"])
                out[e["paid_by"]]["items"].append({**line, "effect": -e["amount"]})
    for p in out.values():
        p["base"] = p["owes_me"] - p["i_owe"]
        p["net"] = p["base"] - p["paid_me"] + p["i_paid"]
    return out


def pair_debt(p: dict[str, Any]) -> dict[str, Any] | None:
    """The Debts row of one owner pair: direction (by the net, else by what was owed), amount (what is still owed plus
    what was paid back) and paid, in cents; None when nothing links them."""
    base, net = p["base"], p["net"]
    if net > 0 or (net == 0 and base >= 0):
        direction, paid, amount = "lent", p["paid_me"], net + p["paid_me"]
    else:
        direction, paid, amount = "borrowed", p["i_paid"], -net + p["i_paid"]
    if amount <= 0:
        return None
    return {"direction": direction, "amount": amount, "paid": paid}


def _pair_out(p: dict[str, Any]) -> dict[str, Any]:
    d = pair_debt(p)
    return {
        "member": p["member"], "name": p["name"], "net": p["net"] / 100,
        "owes_me": p["owes_me"] / 100, "i_owe": p["i_owe"] / 100, "paid_me": p["paid_me"] / 100, "i_paid": p["i_paid"] / 100,
        "direction": "settled" if p["net"] == 0 else ("owes_me" if p["net"] > 0 else "i_owe"),
        "linked": d is not None, "items": p["items"][-MAX_DETAIL_ITEMS:],
    }


def settle_plan(group: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who pays whom: first what is open between the owner and each member (exactly the Debts rows), then the
    other members among themselves on what is left (the owner's part is then zero)."""
    me = _me(group)
    ids = [m["id"] for m in group["members"]]
    bal = compute_balances(ids, entries)
    pairs = pair_balances(group, entries)
    mine = []
    residual = {m: _cents(bal[m]["net"]) for m in ids if m != me}
    for m, p in pairs.items():
        if p["net"] > 0:
            mine.append({"from": m, "to": me, "amount": p["net"] / 100})
        elif p["net"] < 0:
            mine.append({"from": me, "to": m, "amount": -p["net"] / 100})
        residual[m] = residual.get(m, 0) + p["net"]
    mine.sort(key=lambda s: -s["amount"])
    return mine + settle_up({m: c / 100 for m, c in residual.items()})


# --------------------------------------------------------------------------- rows


def _me(group: dict[str, Any]) -> str:
    return next((m["id"] for m in group["members"] if m.get("me")), "")


def _group_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        members = json.loads(row.get("members") or "[]")
    except ValueError:
        members = []
    return {
        "id": row["id"],
        "name": row["name"],
        "currency": row.get("currency") or "MAD",
        "members": [m for m in members if isinstance(m, dict) and m.get("id")],
        "archived": bool(row.get("archived")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _json_dict(raw: Any) -> dict[str, float]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return {str(k): round(float(v), 2) for k, v in value.items()} if isinstance(value, dict) else {}


def _entry_row(row: dict[str, Any]) -> dict[str, Any]:
    percents = _json_dict(row.get("percents")) if row.get("percents") else None
    return {
        "id": row["id"],
        "group_id": row["group_id"],
        "kind": row.get("kind") or "expense",
        "date": row["date"],
        "description": row.get("description") or "",
        "amount": round(float(row.get("amount") or 0), 2),
        "paid_by": row["paid_by"],
        "split": row.get("split") or "equal",
        "shares": _json_dict(row.get("shares")),
        "percents": percents,
        "category": row.get("category") or "other",
        "note": row.get("note"),
        "expense_id": row.get("expense_id"),
        "origin": row.get("origin"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _group(db: AppDB, group_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_split_groups WHERE id = ?", [group_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="group not found")
    return _group_row(row)


async def _entries(db: AppDB, group_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT * FROM app_split_expenses WHERE group_id = ? ORDER BY date DESC, created_at DESC", [group_id]
    )
    return [_entry_row(r) for r in rows]


async def _entry(db: AppDB, group_id: str, entry_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_split_expenses WHERE id = ? AND group_id = ?", [entry_id, group_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="shared expense not found")
    return _entry_row(row)


def _detail(group: dict[str, Any], entries: list[dict[str, Any]], debt_ids: dict[str, str] | None = None) -> dict[str, Any]:
    ids = [m["id"] for m in group["members"]]
    names = {m["id"]: m["name"] for m in group["members"]}
    bal = compute_balances(ids, entries)
    me = _me(group)
    spent = round(sum(e["amount"] for e in entries if e["kind"] == "expense"), 2)
    my_share = round(sum(e["shares"].get(me, 0.0) for e in entries if e["kind"] == "expense"), 2)
    pairs = [{**_pair_out(p), "debt_id": (debt_ids or {}).get(m)} for m, p in pair_balances(group, entries).items()]
    return {
        **group,
        "me": me,
        "entries": entries,
        "balances": [{"member": m, "name": names[m], **bal[m]} for m in ids],
        "pairs": pairs,
        "settle": [{**s, "from_name": names.get(s["from"], "?"), "to_name": names.get(s["to"], "?")}
                   for s in settle_plan(group, entries)],
        "totals": {"spent": spent, "my_share": my_share, "my_net": bal.get(me, {"net": 0.0})["net"],
                   "count": sum(1 for e in entries if e["kind"] == "expense")},
    }


async def _full(db: AppDB, group_id: str) -> dict[str, Any]:
    """The group as the PWA shows it, with the ids of its Debts rows."""
    group = await _group(db, group_id)
    rows = await db.fetch(
        "SELECT id, split_member_id FROM app_debts WHERE origin = 'split' AND split_group_id = ?", [group_id]
    )
    return _detail(group, await _entries(db, group_id), {r["split_member_id"]: r["id"] for r in rows})


# --------------------------------------------------------------------------- the owner's share in app_expenses


async def _sync_mirror(db: AppDB, group: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """(Re)create the owner's-share expense of ``entry``; returns its id (None when nothing is mirrored)."""
    if entry.get("expense_id"):
        await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [entry["expense_id"]])
    share = owner_share(group, entry)
    if share <= 0:
        return None
    payer = next((m["name"] for m in group["members"] if m["id"] == entry["paid_by"]), "?")
    paid = "" if entry["paid_by"] == _me(group) else f", paid by {payer}"
    note = f"Your share of {entry['amount']:.2f} {group['currency']}{paid} (split: {group['name']})"
    if entry.get("note"):
        note += f". {entry['note']}"
    expense = await insert_expense(
        db,
        {"date": entry["date"], "merchant": entry["description"] or group["name"], "total": share,
         "currency": group["currency"], "category": entry["category"], "note": note[:5000], "source": "split",
         "source_ref": entry["id"]},
        keep_category=True,
    )
    return str(expense["id"])


# --------------------------------------------------------------------------- the Debts rows (derived)


async def _touch(db: AppDB, group_id: str) -> None:
    """Bump the group's revision *before* changing it: its Debts rows are stale until :func:`sync_group_debts`."""
    await db.execute("UPDATE app_split_groups SET updated_at = ?, rev = COALESCE(rev, 0) + 1 WHERE id = ?",
                     [now_iso(), group_id])


async def _drop_debts(db: AppDB, debt_ids: list[str]) -> None:
    for debt_id in debt_ids:
        await db.execute("DELETE FROM app_debt_payments WHERE debt_id = ?", [debt_id])
        await db.execute("DELETE FROM app_debts WHERE id = ? AND origin = 'split'", [debt_id])


async def sync_group_debts(db: AppDB, group_id: str) -> None:
    """Make the group's Debts rows (and their repayments) match the group; idempotent. A group that no longer
    exists loses its rows."""
    row = await db.fetch_one("SELECT * FROM app_split_groups WHERE id = ?", [group_id])
    existing = await db.fetch(
        "SELECT * FROM app_debts WHERE origin = 'split' AND split_group_id = ? ORDER BY created_at, id", [group_id]
    )
    if row is None:
        await _drop_debts(db, [r["id"] for r in existing])
        return
    rev = int(row.get("rev") or 0)
    group = _group_row(row)
    entries = await _entries(db, group_id)
    by_member: dict[str, dict[str, Any]] = {}
    extra: list[str] = []
    for r in existing:
        if r.get("split_member_id") in by_member:
            extra.append(r["id"])  # a duplicate (two syncs at once): keep the first
        else:
            by_member[r["split_member_id"]] = r
    me = _me(group)
    settlements = {e["id"]: e for e in entries if e["kind"] == "settlement"}
    now = now_iso()
    for member, p in pair_balances(group, entries).items():
        cur = by_member.pop(member, None)
        want = pair_debt(p)
        if want is None:
            if cur is not None:
                await _drop_debts(db, [cur["id"]])
            continue
        settled = want["amount"] - want["paid"] <= 0
        detail = json.dumps({"group_name": group["name"], "member_name": p["name"],
                             "items": p["items"][-MAX_DETAIL_ITEMS:]}, ensure_ascii=False)
        fields = {
            "person": p["name"][:100], "direction": want["direction"], "amount": want["amount"] / 100,
            "currency": group["currency"], "date": p["items"][0]["date"] if p["items"] else local_today().isoformat(),
            "settled": 1 if settled else 0, "split_detail": detail,
        }
        if cur is None:
            debt_id = new_id()
            full = {"id": debt_id, **fields, "due_date": None, "note": None, "settled_at": now if settled else None,
                    "origin": "split", "audio_key": None, "split_group_id": group_id, "split_member_id": member,
                    "created_at": now, "updated_at": now}
            cols = list(full)
            await db.execute(f"INSERT INTO app_debts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                             [full[c] for c in cols])
        else:
            debt_id = cur["id"]
            changes = {k: v for k, v in fields.items()
                       if (round(float(cur.get(k) or 0), 2) != round(float(v), 2) if k in ("amount", "settled")
                           else cur.get(k) != v)}
            if "settled" in changes:
                changes["settled_at"] = now if settled else None
            if changes:
                changes["updated_at"] = now
                sets = ", ".join(f"{k} = ?" for k in changes)
                await db.execute(f"UPDATE app_debts SET {sets} WHERE id = ?", [*changes.values(), debt_id])
        # Its repayments: the settlements towards the owner (they owe me) or from the owner (I owe them).
        frm, to = (member, me) if want["direction"] == "lent" else (me, member)
        wanted = {sid: s for sid, s in settlements.items() if s["paid_by"] == frm and _settled_to(s) == to}
        have = {r["id"]: r for r in await db.fetch("SELECT * FROM app_debt_payments WHERE debt_id = ?", [debt_id])}
        for pid, pay in have.items():
            s = wanted.get(pid)
            if s is None:
                await db.execute("DELETE FROM app_debt_payments WHERE id = ?", [pid])
            elif round(float(pay["amount"]), 2) != s["amount"] or pay["date"] != s["date"]:
                await db.execute("UPDATE app_debt_payments SET amount = ?, date = ? WHERE id = ?", [s["amount"], s["date"], pid])
        for sid, s in wanted.items():
            if sid not in have:
                await db.execute("DELETE FROM app_debt_payments WHERE id = ?", [sid])  # was under another row
                await db.execute(
                    "INSERT INTO app_debt_payments (id, debt_id, amount, date, note, origin, audio_key, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [sid, debt_id, s["amount"], s["date"], s.get("note"), s.get("origin") or "split",
                     s.get("audio_key"), s["created_at"]],
                )
    await _drop_debts(db, extra + [r["id"] for r in by_member.values()])
    await db.execute("UPDATE app_split_groups SET debts_rev = ? WHERE id = ?", [rev, group_id])


async def heal_split_debts(db: AppDB) -> int:
    """Rebuild the Debts rows of every group changed since they were built (a change interrupted half-way, a group
    from before this feature) and drop the rows of deleted groups; returns how many groups were synced."""
    await ensure_schema(db)
    rows = await db.fetch(
        "SELECT id FROM app_split_groups WHERE debts_rev IS NULL OR debts_rev != rev"
        " UNION SELECT DISTINCT split_group_id AS id FROM app_debts WHERE origin = 'split'"
        " AND split_group_id NOT IN (SELECT id FROM app_split_groups)"
    )
    for r in rows:
        if r.get("id"):
            await sync_group_debts(db, r["id"])
    return len(rows)


def _check_not_overpaid(group: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    """409 when an edit or a deletion leaves a member repaid more than they owe: the Debts row had repayments and its
    net would cross zero (the repayments would silently turn into money owed the other way)."""
    old, new = pair_balances(group, before), pair_balances(group, after)
    cur = group["currency"]
    for m, p in new.items():
        was = pair_debt(old[m]) if m in old else None
        if was is None or was["paid"] <= 0:
            continue
        lent = was["direction"] == "lent"
        if (lent and p["net"] < 0) or (not lent and p["net"] > 0):
            paid, over = was["paid"] / 100, abs(p["net"]) / 100
            if lent:
                said = f"{p['name']} already paid you back {paid:.2f} {cur} in this group"
                after_ = f"you would owe {p['name']} {over:.2f} {cur}"
            else:
                said = f"You already paid {p['name']} back {paid:.2f} {cur} in this group"
                after_ = f"{p['name']} would owe you {over:.2f} {cur}"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"{said}: after this change {after_}. Remove that repayment first (on the Split or the "
                       "Debts page), then make the change.",
            )


# --------------------------------------------------------------------------- models


class MemberIn(BaseModel):
    id: str | None = Field(None, max_length=40)
    name: str = Field(min_length=1, max_length=60)


class GroupCreate(_Fields):
    name: str = Field(min_length=1, max_length=100)
    currency: str = "MAD"
    members: list[str] = Field(min_length=1, max_length=MAX_MEMBERS - 1, description="The other members' names")
    me_name: str = Field("Me", min_length=1, max_length=60)


class GroupPatch(_Fields):
    name: str | None = Field(None, min_length=1, max_length=100)
    currency: str | None = None
    members: list[MemberIn] | None = Field(None, max_length=MAX_MEMBERS, description="The full member list")
    archived: bool | None = None


class EntryFields(_Fields):
    @field_validator("description", check_fields=False)
    @classmethod
    def _desc(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


MAX_AMOUNT = 1e9  # anything larger is a typo (and keeps the cents arithmetic exact)


class EntryCreate(EntryFields):
    description: str = Field("", max_length=200)
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    paid_by: str = Field(min_length=1, max_length=40)
    date: str | None = None
    split: SplitMode = "equal"
    participants: list[str] | None = Field(
        None, max_length=MAX_MEMBERS, description="equal split: who shares it (default: everyone)"
    )
    shares: dict[str, float] | None = Field(None, max_length=MAX_MEMBERS, description="custom split: amount per member id")
    percents: dict[str, float] | None = Field(
        None, max_length=MAX_MEMBERS, description="percent split: percentage per member id, adding up to 100"
    )
    category: Category = "other"
    note: str | None = Field(None, max_length=2000)


class EntryPatch(EntryFields):
    description: str | None = Field(None, max_length=200)
    amount: float | None = Field(None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    paid_by: str | None = Field(None, min_length=1, max_length=40)
    date: str | None = None
    split: SplitMode | None = None
    participants: list[str] | None = Field(None, max_length=MAX_MEMBERS)
    shares: dict[str, float] | None = Field(None, max_length=MAX_MEMBERS)
    percents: dict[str, float] | None = Field(None, max_length=MAX_MEMBERS)
    category: Category | None = None
    note: str | None = Field(None, max_length=2000)


class SettleBody(_Fields):
    from_member: str = Field(alias="from", min_length=1, max_length=40)
    to_member: str = Field(alias="to", min_length=1, max_length=40)
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    date: str | None = None
    note: str | None = Field(None, max_length=2000)


def _unique_names(names: list[str]) -> None:
    seen: set[str] = set()
    for n in names:
        key = normalise_merchant(n) or n.strip().lower()
        if key in seen:
            raise HTTPException(422, detail=f"two members are called {n.strip()!r}")
        seen.add(key)


def _known(group: dict[str, Any], ids: Any) -> None:
    unknown = sorted(set(ids) - {m["id"] for m in group["members"]})
    if unknown:
        raise HTTPException(422, detail=f"unknown members: {', '.join(unknown)}")


def _shares_for(group: dict[str, Any], amount: float, split: str, participants: list[str] | None,
                shares: dict[str, float] | None, percents: dict[str, float] | None,
                payer: str) -> tuple[dict[str, float], dict[str, float] | None]:
    """The shares (amounts) of an expense, and its percentages for a percent split."""
    ids = [m["id"] for m in group["members"]]
    if split == "custom":
        if not shares:
            raise HTTPException(422, detail="a custom split needs shares")
        _known(group, shares)
        if any(not math.isfinite(v) or v < 0 or v > MAX_AMOUNT for v in shares.values()):
            raise HTTPException(422, detail="shares must be amounts of 0 or more")
        clean = {m: _money(shares[m]) for m in ids if m in shares and _cents(shares[m]) > 0}
        if not clean:
            raise HTTPException(422, detail="at least one member needs a share")
        if sum(_cents(v) for v in clean.values()) != _cents(amount):
            raise HTTPException(422, detail=f"the shares add up to {sum(clean.values()):.2f}, not {amount:.2f}")
        return clean, None
    if split == "percent":
        if not percents:
            raise HTTPException(422, detail="a percent split needs percentages")
        _known(group, percents)
        if any(not math.isfinite(v) or v < 0 or v > 100 for v in percents.values()):
            raise HTTPException(422, detail="percentages must be between 0 and 100")
        clean_pct = {m: _basis_points(percents[m]) / 100 for m in ids if m in percents and _basis_points(percents[m]) > 0}
        total = sum(_basis_points(v) for v in clean_pct.values())
        if total != 10000:
            raise HTTPException(422, detail=f"the percentages add up to {total / 100:.2f} %, not 100 %")
        return percent_shares(amount, clean_pct, payer), clean_pct
    who = participants if participants else ids
    _known(group, who)
    ordered = [m for m in ids if m in set(who)]  # member order: the leftover cents are deterministic
    return equal_shares(amount, ordered, payer), None


def _positive(amount: float) -> float:
    """The amount in cents; 422 when it rounds to nothing (0.001)."""
    value = _money(amount)
    if value <= 0:
        raise HTTPException(422, detail="the amount must be at least 0.01")
    return value


def _check_member(group: dict[str, Any], member: str, what: str) -> None:
    if member not in {m["id"] for m in group["members"]}:
        raise HTTPException(422, detail=f"{what} is not a member of this group")


# --------------------------------------------------------------------------- routes: groups


@router.get("")
async def list_groups(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_split_groups ORDER BY archived, updated_at DESC")
    groups = []
    for r in rows:
        g = _group_row(r)
        d = _detail(g, await _entries(db, g["id"]))
        groups.append({**g, "me": d["me"], "totals": d["totals"], "settled": not d["settle"]})
    return {"groups": groups}


async def _create_group(db: AppDB, name: str, currency: str, me_name: str, others: list[str]) -> dict[str, Any]:
    _unique_names([me_name, *others])
    members = [{"id": new_id(), "name": me_name.strip(), "me": True}]
    members += [{"id": new_id(), "name": n, "me": False} for n in others]
    now = now_iso()
    row = {"id": new_id(), "name": name.strip(), "currency": currency, "members": json.dumps(members, ensure_ascii=False),
           "archived": 0, "created_at": now, "updated_at": now, "rev": 0, "debts_rev": 0}
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_split_groups ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _group_row(row)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    others = [n.strip() for n in body.members if n.strip()]
    if not others:
        raise HTTPException(422, detail="add at least one other member")
    group = await _create_group(db, body.name, body.currency, body.me_name, others)
    return _detail(group, [])


@router.get("/{group_id}")
async def get_group(group_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    row = await db.fetch_one("SELECT rev, debts_rev FROM app_split_groups WHERE id = ?", [group_id])
    if row is not None and row.get("debts_rev") != row.get("rev"):
        await sync_group_debts(db, group_id)  # an interrupted change: repair the Debts rows first
    return await _full(db, group_id)


@router.patch("/{group_id}")
async def patch_group(group_id: str, body: GroupPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    entries = await _entries(db, group_id)
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        if getattr(body, field) is None:
            raise HTTPException(422, detail=f"{field} cannot be null")
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.archived is not None:
        changes["archived"] = 1 if body.archived else 0
    if body.currency is not None and body.currency != group["currency"]:
        if entries:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="the currency cannot change once expenses are recorded")
        changes["currency"] = body.currency
    if body.members is not None:
        current = {m["id"]: m for m in group["members"]}
        used = {e["paid_by"] for e in entries} | {m for e in entries for m in e["shares"]}
        new_members = []
        for m in body.members:
            if m.id and m.id not in current:
                raise HTTPException(422, detail=f"unknown member id {m.id}")
            new_members.append({"id": m.id or new_id(), "name": m.name.strip(),
                                "me": bool(m.id and current[m.id].get("me"))})
        kept = {m["id"] for m in new_members}
        me = _me(group)
        if me not in kept:
            raise HTTPException(422, detail="you cannot remove yourself from the group")
        removed = [current[i]["name"] for i in current if i not in kept and i in used]
        if removed:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                detail=f"{', '.join(removed)} already has expenses in this group: delete them first")
        if len(new_members) < 2:
            raise HTTPException(422, detail="a group needs at least one other member")
        _unique_names([m["name"] for m in new_members])
        changes["members"] = json.dumps(new_members, ensure_ascii=False)
    if changes:
        await _touch(db, group_id)
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_split_groups SET {sets} WHERE id = ?", [*changes.values(), group_id])
    group = await _group(db, group_id)
    if "name" in changes:  # the owner's-share notes name the group
        for e in entries:
            if e.get("expense_id"):
                new_id_ = await _sync_mirror(db, group, e)
                await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [new_id_, e["id"]])
    if changes:
        await sync_group_debts(db, group_id)  # names and the group name show on the Debts page
    return await _full(db, group_id)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: str, db: AppDB = Depends(_db)) -> Response:
    """Deletes the group, its shared expenses, the owner's-share expenses they created and its Debts rows."""
    await _group(db, group_id)
    await _touch(db, group_id)
    for e in await _entries(db, group_id):
        if e.get("expense_id"):
            await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [e["expense_id"]])
    await db.execute("DELETE FROM app_split_expenses WHERE group_id = ?", [group_id])
    await db.execute("DELETE FROM app_split_groups WHERE id = ?", [group_id])
    await sync_group_debts(db, group_id)  # gone: drops its Debts rows and their repayments
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routes: shared expenses


async def _store_entry(db: AppDB, row: dict[str, Any]) -> None:
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_split_expenses ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )


async def _add_expense(db: AppDB, group: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Store a validated shared expense, its owner's-share mirror and the Debts rows; returns the entry."""
    now = now_iso()
    row = {
        "id": new_id(), "group_id": group["id"], "kind": "expense", "date": fields.get("date") or local_today().isoformat(),
        "description": fields.get("description") or "", "amount": fields["amount"], "paid_by": fields["paid_by"],
        "split": fields["split"], "shares": json.dumps(fields["shares"]),
        "percents": json.dumps(fields["percents"]) if fields.get("percents") else None,
        "category": fields.get("category") or "other", "note": fields.get("note"), "expense_id": None,
        "origin": fields.get("origin"), "audio_key": fields.get("audio_key"), "created_at": now, "updated_at": now,
    }
    await _touch(db, group["id"])
    await _store_entry(db, row)
    entry = _entry_row(row)
    mirror = await _sync_mirror(db, group, entry)
    if mirror:
        await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [mirror, entry["id"]])
    await sync_group_debts(db, group["id"])
    return await _entry(db, group["id"], entry["id"])


@router.post("/{group_id}/expenses", status_code=status.HTTP_201_CREATED)
async def add_entry(group_id: str, body: EntryCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    _check_member(group, body.paid_by, "paid_by")
    amount = _positive(body.amount)
    shares, percents = _shares_for(group, amount, body.split, body.participants, body.shares, body.percents, body.paid_by)
    entry = await _add_expense(db, group, {
        "date": body.date, "description": body.description, "amount": amount, "paid_by": body.paid_by,
        "split": body.split, "shares": shares, "percents": percents, "category": body.category, "note": body.note,
    })
    return {"entry": entry, "group": await _full(db, group_id)}


@router.patch("/{group_id}/expenses/{entry_id}")
async def patch_entry(group_id: str, entry_id: str, body: EntryPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    entry = await _entry(db, group_id, entry_id)
    if entry["kind"] != "expense":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="a settlement cannot be edited: delete it and record it again")
    fields = body.model_fields_set
    for f in fields - {"note", "participants", "shares", "percents"}:
        if getattr(body, f) is None:
            raise HTTPException(422, detail=f"{f} cannot be null")
    paid_by = body.paid_by if "paid_by" in fields else entry["paid_by"]
    _check_member(group, paid_by, "paid_by")
    amount = _positive(body.amount) if body.amount is not None else entry["amount"]
    split = body.split or entry["split"]
    shares, percents = entry["shares"], entry["percents"]
    if fields & {"amount", "split", "participants", "shares", "percents", "paid_by"}:
        participants = body.participants if "participants" in fields else (list(entry["shares"]) if split == "equal" else None)
        custom = body.shares if "shares" in fields else (entry["shares"] if split == "custom" else None)
        pct = body.percents if "percents" in fields else (entry["percents"] if split == "percent" else None)
        shares, percents = _shares_for(group, amount, split, participants, custom, pct, paid_by)
    changes = {
        "description": body.description if "description" in fields else entry["description"],
        "amount": amount, "paid_by": paid_by, "split": split, "shares": json.dumps(shares),
        "percents": json.dumps(percents) if percents else None,
        "date": body.date if "date" in fields else entry["date"],
        "category": body.category if "category" in fields else entry["category"],
        "note": body.note if "note" in fields else entry["note"],
        "updated_at": now_iso(),
    }
    entries = await _entries(db, group_id)
    after = [e if e["id"] != entry_id else {**e, **changes, "shares": shares} for e in entries]
    _check_not_overpaid(group, entries, after)
    await _touch(db, group_id)
    sets = ", ".join(f"{k} = ?" for k in changes)
    await db.execute(f"UPDATE app_split_expenses SET {sets} WHERE id = ?", [*changes.values(), entry_id])
    entry = await _entry(db, group_id, entry_id)
    mirror = await _sync_mirror(db, group, entry)
    await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [mirror, entry_id])
    await sync_group_debts(db, group_id)
    return {"entry": await _entry(db, group_id, entry_id), "group": await _full(db, group_id)}


async def _delete_entry(db: AppDB, group: dict[str, Any], entry: dict[str, Any]) -> None:
    entries = await _entries(db, group["id"])
    _check_not_overpaid(group, entries, [e for e in entries if e["id"] != entry["id"]])
    await _touch(db, group["id"])
    if entry.get("expense_id"):
        await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [entry["expense_id"]])
    await db.execute("DELETE FROM app_split_expenses WHERE id = ?", [entry["id"]])
    await sync_group_debts(db, group["id"])


@router.delete("/{group_id}/expenses/{entry_id}")
async def delete_entry(group_id: str, entry_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    await _delete_entry(db, group, await _entry(db, group_id, entry_id))
    return await _full(db, group_id)


async def _settle(db: AppDB, group: dict[str, Any], frm: str, to: str, amount: float, day: str | None,
                  note: str | None, origin: str | None = None, audio_key: str | None = None) -> str:
    """Record that ``frm`` paid ``to`` back; between the owner and a member it cannot exceed what is owed that way
    (the same rule as a repayment on the Debts page). Returns the settlement id."""
    _check_member(group, frm, "from")
    _check_member(group, to, "to")
    if frm == to:
        raise HTTPException(422, detail="from and to must be different members")
    names = {m["id"]: m["name"] for m in group["members"]}
    amount = _positive(amount)
    me = _me(group)
    if me in (frm, to):
        other = to if frm == me else frm
        p = pair_balances(group, await _entries(db, group["id"]))[other]
        owed = p["net"] if to == me else -p["net"]  # cents owed in this direction
        cur = group["currency"]
        if owed <= 0:
            who = f"{names[other]} owes you nothing" if to == me else f"You owe {names[other]} nothing"
            raise HTTPException(422, detail=f"{who} in this group")
        if _cents(amount) > owed:
            who = f"{names[other]} owes you" if to == me else f"You owe {names[other]}"
            raise HTTPException(422, detail=f"{who} only {owed / 100:.2f} {cur} in this group")
    now = now_iso()
    row = {
        "id": new_id(), "group_id": group["id"], "kind": "settlement", "date": day or local_today().isoformat(),
        "description": f"{names[frm]} → {names[to]}", "amount": amount, "paid_by": frm, "split": "custom",
        "shares": json.dumps({to: amount}), "percents": None, "category": "other", "note": note, "expense_id": None,
        "origin": origin, "audio_key": audio_key, "created_at": now, "updated_at": now,
    }
    await _touch(db, group["id"])
    await _store_entry(db, row)
    await sync_group_debts(db, group["id"])
    return row["id"]


@router.post("/{group_id}/settle", status_code=status.HTTP_201_CREATED)
async def settle(group_id: str, body: SettleBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Record that ``from`` paid ``to`` back (a settle-up suggestion, or any amount up to what is owed)."""
    group = await _group(db, group_id)
    await _settle(db, group, body.from_member, body.to_member, body.amount, body.date, body.note, "manual")
    return await _full(db, group_id)


# --------------------------------------------------------------------------- called from the Debts page


async def settle_linked_debt(db: AppDB, debt: dict[str, Any], amount: float, day: str, note: str | None,
                             origin: str, audio_key: str | None = None) -> str:
    """A repayment of a split-linked debt = the matching settlement in its group; returns its id (the payment id)."""
    await ensure_schema(db)
    group = await _group(db, debt["split_group_id"])
    me, member = _me(group), debt["split_member_id"]
    frm, to = (member, me) if debt["direction"] == "lent" else (me, member)
    return await _settle(db, group, frm, to, amount, day, note, origin, audio_key)


async def delete_linked_payment(db: AppDB, debt: dict[str, Any], payment_id: str) -> bool:
    """Removing a repayment of a split-linked debt deletes its settlement; False when there is no such settlement."""
    await ensure_schema(db)
    group = await _group(db, debt["split_group_id"])
    row = await db.fetch_one(
        "SELECT * FROM app_split_expenses WHERE id = ? AND group_id = ? AND kind = 'settlement'", [payment_id, group["id"]]
    )
    if row is None:
        return False
    await _delete_entry(db, group, _entry_row(row))
    return True


# --------------------------------------------------------------------------- voice ("j'ai payé 300 avec Ayoub et Sara")


def _match_member(group: dict[str, Any], person: str) -> str | None:
    others = [m for m in group["members"] if not m.get("me")]
    exact = [m for m in others if normalise_merchant(m["name"]) == normalise_merchant(person)]
    loose = exact or [m for m in others if same_person(m["name"], person)]
    return loose[0]["id"] if len(loose) == 1 else None


async def record_voice_split(db: AppDB, entry: dict[str, Any], *, audio_key: str | None) -> dict[str, Any]:
    """A voice "I paid X for me, A and B, equal shares": an equal shared expense paid by the owner in the most recent
    open group (same currency) that has all those people, else in a new group named after it. The outcome has the
    shape of a voice debt outcome (``type = 'split'``) with ``split`` = where it went (for Undo)."""
    await ensure_schema(db)
    people = [p for p in dict.fromkeys(entry.get("people") or []) if p.strip()]
    base = {"type": "split", "person": ", ".join(people), "amount": entry["amount"], "currency": entry["currency"],
            "payment_ids": [], "applied": 0.0, "unapplied": 0.0}
    group: dict[str, Any] | None = None
    ids: list[str] = []
    rows = await db.fetch(
        "SELECT * FROM app_split_groups WHERE archived = 0 AND currency = ? ORDER BY updated_at DESC", [entry["currency"]]
    )
    for r in rows:
        g = _group_row(r)
        found = [_match_member(g, p) for p in people]
        if all(found) and len(set(found)) == len(found):
            group, ids = g, [f for f in found if f]
            break
    created = group is None
    if group is None:
        name = (entry.get("merchant") or "").strip() or "Shared"
        group = await _create_group(db, name[:100], entry["currency"], "Me", [p.strip()[:60] for p in people])
        ids = [m["id"] for m in group["members"] if not m.get("me")]
    me = _me(group)
    amount = _positive(float(entry["amount"]))
    cutoff = iso(datetime.now(timezone.utc) - timedelta(seconds=VOICE_DUPLICATE_S))
    if not created:
        dup = await db.fetch_one(
            "SELECT id FROM app_split_expenses WHERE group_id = ? AND origin = 'voice' AND amount = ? AND date = ?"
            " AND paid_by = ? AND created_at >= ? AND COALESCE(audio_key, '') != COALESCE(?, '')",
            [group["id"], amount, entry["date"], me, cutoff, audio_key],
        )
        if dup is not None:
            return {**base, "action": "duplicate", "debts": [],
                    "split": {"group_id": group["id"], "group_name": group["name"], "entry_id": dup["id"],
                              "group_created": False, "my_share": 0.0}}
    order = [m["id"] for m in group["members"] if m["id"] in {me, *ids}]
    shares = equal_shares(amount, order, me)
    saved = await _add_expense(db, group, {
        "date": entry["date"], "description": (entry.get("merchant") or "")[:200], "amount": amount, "paid_by": me,
        "split": "equal", "shares": shares, "category": entry.get("category") or "other", "note": entry.get("note"),
        "origin": "voice", "audio_key": audio_key,
    })
    debts = await load_debts(db, "origin = 'split' AND split_group_id = ?", [group["id"]])
    return {**base, "action": "created", "debts": [d for d in debts if d.get("split_member_id") in set(ids)],
            "split": {"group_id": group["id"], "group_name": group["name"], "entry_id": saved["id"],
                      "group_created": created, "my_share": shares.get(me, 0.0)}}
