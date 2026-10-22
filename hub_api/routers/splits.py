"""/money/splits: shared expenses between friends (Tricount-like). Members are names, not accounts.

* ``app_split_groups``: a group (trip, flat share...) in one currency, with its members as JSON
  ``[{"id", "name", "me"}]``; exactly one member is the owner (``me``);
* ``app_split_expenses``: what was paid in the group: ``kind = 'expense'`` (paid by one member, split equally between
  some members or by custom amounts) or ``kind = 'settlement'`` (member A paid member B back).

Balances: a member's net is what they paid minus their shares (positive: the group owes them). Settle-up suggestions
pair the biggest debtor with the biggest creditor until everyone is even (at most n - 1 payments).

**The owner's spending rule.** A shared expense *paid by the owner* is also recorded in ``app_expenses`` for the
owner's share only (``source = 'split'``, ``source_ref`` = the shared expense id): the friends' part is money they owe
back, not the owner's spending, so only the owner's share counts in budgets and the month totals. It is kept in sync
when the shared expense changes and removed with it. A shared expense paid by someone else is not mirrored: the
owner's share is a debt to that member, settled in the group (a settlement is a transfer, never an expense).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator

from hub_api.appdata import AppDB, get_appdb, local_today, new_id, now_iso
from hub_api.merchant_rules import normalise_merchant
from hub_api.routers.expenses import Category, _Fields, insert_expense
from hub_api.routers.expenses import ensure_schema as ensure_expenses

__all__ = [
    "router", "SCHEMA", "ensure_schema", "equal_shares", "compute_balances", "settle_up", "owner_share",
]

router = APIRouter(prefix="/money/splits", tags=["money"])

SplitMode = Literal["equal", "custom"]
EPS = 0.005
MAX_MEMBERS = 30

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


async def ensure_schema(db: AppDB) -> AppDB:
    await ensure_expenses(db)
    await db.ensure("splits", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- pure maths (in cents)


def _cents(x: float) -> int:
    return int(round(x * 100))


def equal_shares(amount: float, member_ids: list[str]) -> dict[str, float]:
    """``amount`` split equally; the leftover cents go to the first members so the shares add up exactly."""
    if not member_ids:
        return {}
    total = _cents(amount)
    base, extra = divmod(total, len(member_ids))
    return {m: (base + (1 if i < extra else 0)) / 100 for i, m in enumerate(member_ids)}


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
    """The owner's share of a shared expense *they paid*; 0 otherwise (settlements included)."""
    me = _me(group)
    if entry.get("kind") != "expense" or entry["paid_by"] != me:
        return 0.0
    return round(float(entry["shares"].get(me, 0.0)), 2)


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


def _entry_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        shares = json.loads(row.get("shares") or "{}")
    except ValueError:
        shares = {}
    return {
        "id": row["id"],
        "group_id": row["group_id"],
        "kind": row.get("kind") or "expense",
        "date": row["date"],
        "description": row.get("description") or "",
        "amount": round(float(row.get("amount") or 0), 2),
        "paid_by": row["paid_by"],
        "split": row.get("split") or "equal",
        "shares": {str(k): round(float(v), 2) for k, v in (shares.items() if isinstance(shares, dict) else [])},
        "category": row.get("category") or "other",
        "note": row.get("note"),
        "expense_id": row.get("expense_id"),
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


def _detail(group: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [m["id"] for m in group["members"]]
    names = {m["id"]: m["name"] for m in group["members"]}
    bal = compute_balances(ids, entries)
    me = _me(group)
    spent = round(sum(e["amount"] for e in entries if e["kind"] == "expense"), 2)
    my_share = round(sum(e["shares"].get(me, 0.0) for e in entries if e["kind"] == "expense"), 2)
    return {
        **group,
        "me": me,
        "entries": entries,
        "balances": [{"member": m, "name": names[m], **bal[m]} for m in ids],
        "settle": [{**s, "from_name": names.get(s["from"], "?"), "to_name": names.get(s["to"], "?")}
                   for s in settle_up({m: bal[m]["net"] for m in ids})],
        "totals": {"spent": spent, "my_share": my_share, "my_net": bal.get(me, {"net": 0.0})["net"],
                   "count": sum(1 for e in entries if e["kind"] == "expense")},
    }


# --------------------------------------------------------------------------- the owner's share in app_expenses


async def _sync_mirror(db: AppDB, group: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """(Re)create the owner's-share expense of ``entry``; returns its id (None when nothing is mirrored)."""
    if entry.get("expense_id"):
        await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [entry["expense_id"]])
    share = owner_share(group, entry)
    if share <= 0:
        return None
    note = f"Your share of {entry['amount']:.2f} {group['currency']} (split: {group['name']})"
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


class EntryCreate(EntryFields):
    description: str = Field("", max_length=200)
    amount: float = Field(gt=0)
    paid_by: str = Field(min_length=1, max_length=40)
    date: str | None = None
    split: SplitMode = "equal"
    participants: list[str] | None = Field(None, description="equal split: who shares it (default: everyone)")
    shares: dict[str, float] | None = Field(None, description="custom split: amount per member id")
    category: Category = "other"
    note: str | None = Field(None, max_length=2000)


class EntryPatch(EntryFields):
    description: str | None = Field(None, max_length=200)
    amount: float | None = Field(None, gt=0)
    paid_by: str | None = Field(None, min_length=1, max_length=40)
    date: str | None = None
    split: SplitMode | None = None
    participants: list[str] | None = None
    shares: dict[str, float] | None = None
    category: Category | None = None
    note: str | None = Field(None, max_length=2000)


class SettleBody(_Fields):
    from_member: str = Field(alias="from", min_length=1, max_length=40)
    to_member: str = Field(alias="to", min_length=1, max_length=40)
    amount: float = Field(gt=0)
    date: str | None = None
    note: str | None = Field(None, max_length=2000)


def _unique_names(names: list[str]) -> None:
    seen: set[str] = set()
    for n in names:
        key = normalise_merchant(n) or n.strip().lower()
        if key in seen:
            raise HTTPException(422, detail=f"two members are called {n.strip()!r}")
        seen.add(key)


def _shares_for(group: dict[str, Any], amount: float, split: str, participants: list[str] | None,
                shares: dict[str, float] | None) -> dict[str, float]:
    ids = [m["id"] for m in group["members"]]
    if split == "custom":
        if not shares:
            raise HTTPException(422, detail="a custom split needs shares")
        unknown = sorted(set(shares) - set(ids))
        if unknown:
            raise HTTPException(422, detail=f"unknown members: {', '.join(unknown)}")
        if any(v < 0 for v in shares.values()):
            raise HTTPException(422, detail="shares cannot be negative")
        clean = {m: round(float(v), 2) for m, v in shares.items() if _cents(v) > 0}
        if not clean:
            raise HTTPException(422, detail="at least one member needs a share")
        if sum(_cents(v) for v in clean.values()) != _cents(amount):
            raise HTTPException(422, detail=f"the shares add up to {sum(clean.values()):.2f}, not {amount:.2f}")
        return clean
    who = participants if participants else ids
    unknown = sorted(set(who) - set(ids))
    if unknown:
        raise HTTPException(422, detail=f"unknown members: {', '.join(unknown)}")
    ordered = [m for m in ids if m in set(who)]  # member order: the leftover cents are deterministic
    return equal_shares(amount, ordered)


def _check_member(group: dict[str, Any], member: str, what: str) -> None:
    if member not in {m["id"] for m in group["members"]}:
        raise HTTPException(422, detail=f"{what} is not a member of this group")


async def _touch(db: AppDB, group_id: str) -> None:
    await db.execute("UPDATE app_split_groups SET updated_at = ? WHERE id = ?", [now_iso(), group_id])


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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    others = [n.strip() for n in body.members if n.strip()]
    if not others:
        raise HTTPException(422, detail="add at least one other member")
    _unique_names([body.me_name, *others])
    members = [{"id": new_id(), "name": body.me_name.strip(), "me": True}]
    members += [{"id": new_id(), "name": n, "me": False} for n in others]
    now = now_iso()
    row = {"id": new_id(), "name": body.name.strip(), "currency": body.currency, "members": json.dumps(members, ensure_ascii=False),
           "archived": 0, "created_at": now, "updated_at": now}
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_split_groups ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _detail(_group_row(row), [])


@router.get("/{group_id}")
async def get_group(group_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _detail(await _group(db, group_id), await _entries(db, group_id))


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
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_split_groups SET {sets} WHERE id = ?", [*changes.values(), group_id])
    group = await _group(db, group_id)
    if "name" in changes:  # the owner's-share notes name the group
        for e in entries:
            if e.get("expense_id"):
                new_id_ = await _sync_mirror(db, group, e)
                await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [new_id_, e["id"]])
        entries = await _entries(db, group_id)
    return _detail(group, entries)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: str, db: AppDB = Depends(_db)) -> Response:
    """Deletes the group, its shared expenses and the owner's-share expenses they created."""
    await _group(db, group_id)
    for e in await _entries(db, group_id):
        if e.get("expense_id"):
            await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [e["expense_id"]])
    await db.execute("DELETE FROM app_split_expenses WHERE group_id = ?", [group_id])
    await db.execute("DELETE FROM app_split_groups WHERE id = ?", [group_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- routes: shared expenses


async def _store_entry(db: AppDB, group: dict[str, Any], row: dict[str, Any]) -> None:
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_split_expenses ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    await _touch(db, group["id"])


@router.post("/{group_id}/expenses", status_code=status.HTTP_201_CREATED)
async def add_entry(group_id: str, body: EntryCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    _check_member(group, body.paid_by, "paid_by")
    shares = _shares_for(group, body.amount, body.split, body.participants, body.shares)
    now = now_iso()
    row = {
        "id": new_id(), "group_id": group_id, "kind": "expense", "date": body.date or local_today().isoformat(),
        "description": body.description, "amount": round(body.amount, 2), "paid_by": body.paid_by, "split": body.split,
        "shares": json.dumps(shares), "category": body.category, "note": body.note, "expense_id": None,
        "created_at": now, "updated_at": now,
    }
    await _store_entry(db, group, row)
    entry = _entry_row(row)
    mirror = await _sync_mirror(db, group, entry)
    if mirror:
        await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [mirror, entry["id"]])
    return {"entry": await _entry(db, group_id, entry["id"]), "group": _detail(group, await _entries(db, group_id))}


@router.patch("/{group_id}/expenses/{entry_id}")
async def patch_entry(group_id: str, entry_id: str, body: EntryPatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    entry = await _entry(db, group_id, entry_id)
    if entry["kind"] != "expense":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="a settlement cannot be edited: delete it and record it again")
    fields = body.model_fields_set
    for f in fields - {"note", "participants", "shares"}:
        if getattr(body, f) is None:
            raise HTTPException(422, detail=f"{f} cannot be null")
    paid_by = body.paid_by if "paid_by" in fields else entry["paid_by"]
    _check_member(group, paid_by, "paid_by")
    amount = round(body.amount, 2) if body.amount is not None else entry["amount"]
    split = body.split or entry["split"]
    shares = entry["shares"]
    if fields & {"amount", "split", "participants", "shares"}:
        participants = body.participants if "participants" in fields else (list(entry["shares"]) if split == "equal" else None)
        custom = body.shares if "shares" in fields else (entry["shares"] if split == "custom" else None)
        shares = _shares_for(group, amount, split, participants, custom)
    changes = {
        "description": body.description if "description" in fields else entry["description"],
        "amount": amount, "paid_by": paid_by, "split": split, "shares": json.dumps(shares),
        "date": body.date if "date" in fields else entry["date"],
        "category": body.category if "category" in fields else entry["category"],
        "note": body.note if "note" in fields else entry["note"],
        "updated_at": now_iso(),
    }
    sets = ", ".join(f"{k} = ?" for k in changes)
    await db.execute(f"UPDATE app_split_expenses SET {sets} WHERE id = ?", [*changes.values(), entry_id])
    entry = await _entry(db, group_id, entry_id)
    mirror = await _sync_mirror(db, group, entry)
    await db.execute("UPDATE app_split_expenses SET expense_id = ? WHERE id = ?", [mirror, entry_id])
    await _touch(db, group_id)
    return {"entry": await _entry(db, group_id, entry_id), "group": _detail(group, await _entries(db, group_id))}


@router.delete("/{group_id}/expenses/{entry_id}")
async def delete_entry(group_id: str, entry_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    group = await _group(db, group_id)
    entry = await _entry(db, group_id, entry_id)
    if entry.get("expense_id"):
        await db.execute("DELETE FROM app_expenses WHERE id = ? AND source = 'split'", [entry["expense_id"]])
    await db.execute("DELETE FROM app_split_expenses WHERE id = ?", [entry_id])
    await _touch(db, group_id)
    return _detail(group, await _entries(db, group_id))


@router.post("/{group_id}/settle", status_code=status.HTTP_201_CREATED)
async def settle(group_id: str, body: SettleBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    """Record that ``from`` paid ``to`` back (a settle-up suggestion, or any amount)."""
    group = await _group(db, group_id)
    _check_member(group, body.from_member, "from")
    _check_member(group, body.to_member, "to")
    if body.from_member == body.to_member:
        raise HTTPException(422, detail="from and to must be different members")
    names = {m["id"]: m["name"] for m in group["members"]}
    now = now_iso()
    row = {
        "id": new_id(), "group_id": group_id, "kind": "settlement", "date": body.date or local_today().isoformat(),
        "description": f"{names[body.from_member]} → {names[body.to_member]}", "amount": round(body.amount, 2),
        "paid_by": body.from_member, "split": "custom", "shares": json.dumps({body.to_member: round(body.amount, 2)}),
        "category": "other", "note": body.note, "expense_id": None, "created_at": now, "updated_at": now,
    }
    await _store_entry(db, group, row)
    return _detail(group, await _entries(db, group_id))
