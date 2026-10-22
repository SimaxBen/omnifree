"""Merchant rules: "always file <merchant> under <category>" (and optionally show it under a nicer name).

A rule matches the *normalised* merchant (lower case, accents and punctuation removed, spaces collapsed:
``"MARJANE  Casa-Anfa 123"`` -> ``"marjane casa anfa 123"``) either by ``equals`` or by ``contains``.
The most specific rule wins: ``equals`` before ``contains``, then the longest pattern.

Rules are applied in :func:`hub_api.routers.expenses.insert_expense`, the one place every expense is created
(manual, receipt scan, Gmail import, bank SMS, voice, split share), and on demand to past expenses
(``POST /money/rules/{id}/apply``). A manual expense keeps the category the user picked explicitly; only the
display name is applied to it (see ``keep_category``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from hub_api.appdata import AppDB

__all__ = [
    "RULES_SCHEMA", "MATCH_MODES", "normalise_merchant", "rule_matches", "best_rule", "apply_rule",
    "ensure_rules", "load_rules", "apply_rules", "rule_out",
]

MATCH_MODES = ("contains", "equals")

RULES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_merchant_rules ("
    " id TEXT PRIMARY KEY, pattern TEXT NOT NULL, match TEXT NOT NULL DEFAULT 'contains',"
    " category TEXT, rename TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
    " UNIQUE (pattern, match))",
)

_NON_WORD = re.compile(r"[^0-9a-z؀-ۿ]+")


def normalise_merchant(value: Any) -> str:
    """Lower case, no accents, punctuation as spaces, spaces collapsed ("" for anything not a string)."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _NON_WORD.sub(" ", text).strip()


def rule_matches(rule: dict[str, Any], merchant: str) -> bool:
    """``merchant`` is already normalised."""
    pattern = rule.get("pattern") or ""
    if not pattern or not merchant:
        return False
    if rule.get("match") == "equals":
        return merchant == pattern
    return f" {pattern} " in f" {merchant} "  # whole words: "bim" must not match "bimo"


def best_rule(rules: Iterable[dict[str, Any]], merchant: Any) -> dict[str, Any] | None:
    norm = normalise_merchant(merchant)
    hits = [r for r in rules if rule_matches(r, norm)]
    if not hits:
        return None
    return min(hits, key=lambda r: (r.get("match") != "equals", -len(r.get("pattern") or ""), r.get("created_at") or ""))


def apply_rule(fields: dict[str, Any], rule: dict[str, Any] | None, *, keep_category: bool = False) -> dict[str, Any]:
    """``fields`` with the rule's category and display name applied (a new dict; unchanged without a rule)."""
    if rule is None:
        return fields
    out = dict(fields)
    if rule.get("category") and not keep_category:
        out["category"] = rule["category"]
    if rule.get("rename"):
        out["merchant"] = rule["rename"]
    return out


async def ensure_rules(db: AppDB) -> AppDB:
    await db.ensure("merchant_rules", RULES_SCHEMA)
    return db


async def load_rules(db: AppDB) -> list[dict[str, Any]]:
    await ensure_rules(db)
    return await db.fetch("SELECT * FROM app_merchant_rules ORDER BY pattern")


async def apply_rules(db: AppDB, fields: dict[str, Any], *, keep_category: bool = False) -> dict[str, Any]:
    """The insert-time hook: one small SELECT, then :func:`apply_rule` with the best match."""
    if not fields.get("merchant"):
        return fields
    return apply_rule(fields, best_rule(await load_rules(db), fields["merchant"]), keep_category=keep_category)


def rule_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pattern": row["pattern"],
        "match": row.get("match") or "contains",
        "category": row.get("category"),
        "rename": row.get("rename"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
