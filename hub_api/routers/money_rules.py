"""/money/rules: merchant recategorisation rules (see :mod:`hub_api.merchant_rules`).

* ``GET /money/rules``: every rule, with how many past expenses it matches;
* ``POST /money/rules``: create, or update the rule with the same normalised pattern and match mode (the PWA's
  "Always do this for <merchant>?" after an edit); on an update, a null ``category`` or ``rename`` keeps the current one;
* ``PATCH`` / ``DELETE /money/rules/{id}``;
* ``POST /money/rules/{id}/apply?dry_run=1``: apply the rule to past expenses (bulk update); ``dry_run`` only counts.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from hub_api.appdata import AppDB, get_appdb, new_id, now_iso
from hub_api.merchant_rules import apply_rule, ensure_rules, normalise_merchant, rule_matches, rule_out
from hub_api.routers.expenses import Category
from hub_api.routers.expenses import ensure_schema as ensure_expenses

__all__ = ["router", "matching_expenses"]

router = APIRouter(prefix="/money/rules", tags=["money"])

MatchMode = Literal["contains", "equals"]
_CHUNK = 80  # ids per UPDATE ... IN (...): D1 allows 100 bound parameters per statement


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await ensure_expenses(db)
    return await ensure_rules(db)


class _RuleFields(BaseModel):
    @field_validator("pattern", check_fields=False)
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v is None:
            return v
        norm = normalise_merchant(v)
        if len(norm) < 2:
            raise ValueError("pattern needs at least 2 letters or digits")
        return norm

    @field_validator("rename", check_fields=False)
    @classmethod
    def _rename(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class RuleCreate(_RuleFields):
    pattern: str = Field(min_length=1, max_length=200, description="Merchant text; stored normalised")
    match: MatchMode = "contains"
    category: Category | None = None
    rename: str | None = Field(None, max_length=200, description="Display name for matching merchants")


class RulePatch(_RuleFields):
    pattern: str | None = Field(None, min_length=1, max_length=200)
    match: MatchMode | None = None
    category: Category | None = None
    rename: str | None = Field(None, max_length=200)


def _check_action(category: str | None, rename: str | None) -> None:
    if not category and not rename:
        raise HTTPException(422, detail="a rule needs a category, a display name or both")


async def _get(db: AppDB, rule_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_merchant_rules WHERE id = ?", [rule_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="rule not found")
    return row


async def _merchants(db: AppDB) -> list[dict[str, Any]]:
    # Normalisation (accents, punctuation) happens in Python, so every merchant is read: a personal log
    # stays in the thousands of rows.
    return await db.fetch("SELECT id, merchant, category FROM app_expenses WHERE merchant != ''")


async def matching_expenses(db: AppDB, rule: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Past expenses the rule matches and would change (id, merchant, category)."""
    rows = await _merchants(db) if rows is None else rows
    out = []
    for r in rows:
        if not rule_matches(rule, normalise_merchant(r["merchant"])):
            continue
        after = apply_rule({"merchant": r["merchant"], "category": r["category"]}, rule)
        if after["merchant"] != r["merchant"] or after["category"] != r["category"]:
            out.append(r)
    return out


@router.get("")
async def list_rules(db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_merchant_rules ORDER BY pattern")
    merchants = await _merchants(db) if rows else []
    return {"rules": [{**rule_out(r), "pending": len(await matching_expenses(db, r, merchants))} for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleCreate, response: Response, db: AppDB = Depends(_db)) -> dict[str, Any]:
    _check_action(body.category, body.rename)
    now = now_iso()
    existing = await db.fetch_one(
        "SELECT * FROM app_merchant_rules WHERE pattern = ? AND match = ?", [body.pattern, body.match]
    )
    if existing:
        # Merge: an action left out (null) keeps the rule's current one, so "Always show it as X" after a rename
        # does not wipe the category the rule already sets (PATCH clears an action explicitly).
        await db.execute(
            "UPDATE app_merchant_rules SET category = ?, rename = ?, updated_at = ? WHERE id = ?",
            [body.category or existing.get("category"), body.rename or existing.get("rename"), now, existing["id"]],
        )
        response.status_code = status.HTTP_200_OK
        row = await _get(db, existing["id"])
    else:
        row = {"id": new_id(), "pattern": body.pattern, "match": body.match, "category": body.category,
               "rename": body.rename, "created_at": now, "updated_at": now}
        cols = list(row)
        await db.execute(
            f"INSERT INTO app_merchant_rules ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
    return {**rule_out(row), "pending": len(await matching_expenses(db, row))}


@router.patch("/{rule_id}")
async def patch_rule(rule_id: str, body: RulePatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    current = await _get(db, rule_id)
    changes = {f: getattr(body, f) for f in body.model_fields_set}
    for field in ("pattern", "match"):
        if field in changes and changes[field] is None:
            raise HTTPException(422, detail=f"{field} cannot be null")
    merged = {**current, **changes}
    _check_action(merged.get("category"), merged.get("rename"))
    clash = await db.fetch_one(
        "SELECT id FROM app_merchant_rules WHERE pattern = ? AND match = ? AND id != ?",
        [merged["pattern"], merged["match"], rule_id],
    )
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="another rule already has this pattern")
    if changes:
        changes["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_merchant_rules SET {sets} WHERE id = ?", [*changes.values(), rule_id])
    row = await _get(db, rule_id)
    return {**rule_out(row), "pending": len(await matching_expenses(db, row))}


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, rule_id)
    await db.execute("DELETE FROM app_merchant_rules WHERE id = ?", [rule_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{rule_id}/apply")
async def apply_to_past(
    rule_id: str,
    dry_run: bool = Query(False, description="Only count the expenses that would change"),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Re-file past expenses matching the rule (category and display name); returns how many changed."""
    rule = await _get(db, rule_id)
    hits = await matching_expenses(db, rule)
    if dry_run or not hits:
        return {"rule": rule_out(rule), "matched": len(hits), "updated": 0, "dry_run": dry_run}
    sets: list[str] = []
    values: list[Any] = []
    if rule.get("category"):
        sets.append("category = ?")
        values.append(rule["category"])
    if rule.get("rename"):
        sets.append("merchant = ?")
        values.append(rule["rename"])
    updated = 0
    for i in range(0, len(hits), _CHUNK):
        ids = [h["id"] for h in hits[i : i + _CHUNK]]
        await db.execute(
            f"UPDATE app_expenses SET {', '.join(sets)} WHERE id IN ({', '.join('?' for _ in ids)})", [*values, *ids]
        )
        updated += len(ids)
    return {"rule": rule_out(rule), "matched": len(hits), "updated": updated, "dry_run": False}
