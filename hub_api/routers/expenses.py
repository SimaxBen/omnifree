"""/expenses: expense log with receipt scanning (image -> vision LLM -> structured expense).

Receipts are stored in the Drive under ``receipts/<yyyy>/<mm>/<id>.<ext>``; extraction runs on
vision-capable LLM providers only (Capability.VISION, Gemini first). Unreadable fields fall back to
safe defaults (currency MAD, today's date in Africa/Casablanca, category "other").
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from hub_api.appdata import AppDB, get_appdb, local_today, new_id, now_iso, read_upload
from hub_api.merchant_rules import apply_rules
from hub_api.receipts import CATEGORIES, read_receipt_file
from hub_api.state import HubState, get_state

__all__ = [
    "router", "CATEGORIES", "normalise_currency", "parse_receipt", "summarise", "SCHEMA",
    "ensure_schema", "insert_expense", "expense_out", "clean_receipt_text", "search_terms",
]

log = logging.getLogger("omnifree.hub.expenses")

router = APIRouter(prefix="/expenses", tags=["expenses"])

Category = Literal[
    "food", "groceries", "transport", "housing", "bills", "health",
    "shopping", "entertainment", "education", "travel", "other",
]
DEFAULT_CURRENCY = "MAD"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_expenses ("
    " id TEXT PRIMARY KEY, date TEXT NOT NULL, merchant TEXT NOT NULL DEFAULT '',"
    " total REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'MAD',"
    " category TEXT NOT NULL DEFAULT 'other', items TEXT NOT NULL DEFAULT '[]', note TEXT,"
    " receipt_key TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_expenses_date ON app_expenses (date)",
)
# Migration v2: where an expense came from ("email" for the Gmail import) and its reference there
# (the email's Message-ID), used to deduplicate imports. NULL for manual and scanned expenses.
SOURCE_COLUMNS = (("source", "TEXT"), ("source_ref", "TEXT"))
SOURCE_INDEX = ("CREATE INDEX IF NOT EXISTS app_expenses_source_ref ON app_expenses (source_ref)",)
# Migration v3: the receipt's readable text (vision read, email body or PDF text) for GET /expenses/search,
# and the storage backend holding ``receipt_key`` (saves a lookup on every backend before presigning).
RECEIPT_COLUMNS = (("receipt_text", "TEXT"), ("receipt_provider", "TEXT"))
RECEIPT_TEXT_LIMIT = 4000
SEARCH_LIMIT = 50
PRESIGN_TTL_S = 3600
PRESIGN_TIMEOUT_S = 5.0

_IMAGE_TYPES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/heic": "heic", "image/heif": "heif",
}
_CURRENCY_ALIASES = {
    "DH": "MAD", "DHS": "MAD", "MAD": "MAD", "DIRHAM": "MAD", "DIRHAMS": "MAD", "MDH": "MAD", "د.م.": "MAD",
    "درهم": "MAD", "€": "EUR", "EURO": "EUR", "EUROS": "EUR", "$": "USD", "US$": "USD", "£": "GBP",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


async def ensure_schema(db: AppDB) -> AppDB:
    """Create ``app_expenses`` and apply its migrations (idempotent, once per process)."""
    await db.ensure("expenses", SCHEMA)
    await db.add_columns("expenses_v2_source", "app_expenses", SOURCE_COLUMNS)
    await db.ensure("expenses_v2_index", SOURCE_INDEX)
    await db.add_columns("expenses_v3_receipt", "app_expenses", RECEIPT_COLUMNS)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


# --------------------------------------------------------------------------- validation helpers


def normalise_currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().upper()
    if raw in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[raw]
    if raw.lower() in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[raw.lower()]
    return raw if re.fullmatch(r"[A-Z]{3}", raw) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d,.\-]", "", value)
        if cleaned.count(",") == 1 and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        cleaned = cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _valid_date(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()[:10]):
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            it = {"name": it}
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        qty, price = _number(it.get("qty")), _number(it.get("price"))
        out.append({"name": name[:200], "qty": qty, "price": None if price is None else round(price, 2)})
    return out[:200]


def clean_receipt_text(value: Any) -> str | None:
    """The receipt's readable text, whitespace collapsed per line, at most RECEIPT_TEXT_LIMIT characters."""
    if not isinstance(value, str):
        return None
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:RECEIPT_TEXT_LIMIT] or None


def parse_receipt(data: dict[str, Any] | None, today: date) -> tuple[dict[str, Any], Literal["high", "medium", "low"]]:
    """Validated expense fields + confidence from the vision model's JSON (``None`` = unreadable)."""
    data = data or {}
    defaulted = 0
    total = _number(data.get("total"))
    if total is None or total < 0:
        total, defaulted = 0.0, defaulted + 2
    currency = normalise_currency(data.get("currency"))
    if currency is None:
        currency, defaulted = DEFAULT_CURRENCY, defaulted + 1
    day = _valid_date(data.get("date"))
    if day is None or day > today.isoformat():
        day, defaulted = today.isoformat(), defaulted + 1
    category = str(data.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        category = "other"
    merchant = data.get("merchant")
    merchant = merchant.strip()[:200] if isinstance(merchant, str) and merchant.strip() else ""
    if not merchant:
        defaulted += 1
    fields = {
        "date": day,
        "merchant": merchant or "Unknown",
        "total": round(total, 2),
        "currency": currency,
        "category": category,
        "items": _clean_items(data.get("items")),
    }
    text = clean_receipt_text(data.get("text"))
    if text:
        fields["receipt_text"] = text
    reported = str(data.get("confidence") or "").strip().lower()
    confidence: Literal["high", "medium", "low"]
    if not data or defaulted >= 2:
        confidence = "low"
    elif defaulted == 1:
        confidence = "low" if reported == "low" else "medium"
    else:
        confidence = reported if reported in ("high", "medium", "low") else "medium"  # type: ignore[assignment]
    return fields, confidence


def _expense_out(row: dict[str, Any]) -> dict[str, Any]:
    try:
        items = json.loads(row.get("items") or "[]")
    except ValueError:
        items = []
    return {
        "id": row["id"],
        "date": row["date"],
        "merchant": row.get("merchant") or "",
        "total": float(row.get("total") or 0),
        "currency": row.get("currency") or DEFAULT_CURRENCY,
        "category": row.get("category") or "other",
        "items": items if isinstance(items, list) else [],
        "note": row.get("note"),
        "receipt_key": row.get("receipt_key"),
        "receipt_provider": row.get("receipt_provider"),
        "source": row.get("source"),
        "source_ref": row.get("source_ref"),
        "created_at": row["created_at"],
    }


expense_out = _expense_out


def summarise(expenses: list[dict[str, Any]]) -> dict[str, Any]:
    by_currency: dict[str, float] = {}
    by_category: dict[str, dict[str, float]] = {}
    for e in expenses:
        cur, cat, amount = e["currency"], e["category"], float(e["total"])
        by_currency[cur] = by_currency.get(cur, 0.0) + amount
        bucket = by_category.setdefault(cat, {})
        bucket[cur] = bucket.get(cur, 0.0) + amount
    return {
        "total_by_currency": {k: round(v, 2) for k, v in by_currency.items()},
        "by_category": {c: {k: round(v, 2) for k, v in m.items()} for c, m in by_category.items()},
        "count": len(expenses),
    }


async def _insert(db: AppDB, fields: dict[str, Any], *, keep_category: bool = False) -> dict[str, Any]:
    """Every expense goes through here (manual, receipt, email, SMS, voice, split share): merchant rules
    (:mod:`hub_api.merchant_rules`) are applied first; ``keep_category`` keeps a category the user chose."""
    await ensure_schema(db)
    fields = await apply_rules(db, fields, keep_category=keep_category)
    row = {
        "id": new_id(),
        "date": fields["date"],
        "merchant": fields.get("merchant") or "",
        "total": float(fields["total"]),
        "currency": fields.get("currency") or DEFAULT_CURRENCY,
        "category": fields.get("category") or "other",
        "items": json.dumps(fields.get("items") or [], ensure_ascii=False),
        "note": fields.get("note"),
        "receipt_key": fields.get("receipt_key"),
        "receipt_text": clean_receipt_text(fields.get("receipt_text")),
        "receipt_provider": fields.get("receipt_provider"),
        "source": fields.get("source"),
        "source_ref": fields.get("source_ref"),
        "created_at": now_iso(),
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_expenses ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return _expense_out(row)


insert_expense = _insert


async def _get(db: AppDB, expense_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM app_expenses WHERE id = ?", [expense_id])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="expense not found")
    return row


# --------------------------------------------------------------------------- models


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: float | None = None
    price: float | None = None


class _Fields(BaseModel):
    @field_validator("currency", check_fields=False)
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is None:
            return v
        cur = normalise_currency(v)
        if cur is None:
            raise ValueError("currency must be an ISO 4217 code (e.g. MAD, EUR)")
        return cur

    @field_validator("date", check_fields=False)
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        d = _valid_date(v)
        if d is None or len(v.strip()) != 10:
            raise ValueError("date must be YYYY-MM-DD")
        return d


class ExpenseCreate(_Fields):
    date: str | None = None  # default: today (Africa/Casablanca)
    merchant: str = Field("", max_length=200)
    total: float = Field(ge=0)
    currency: str = DEFAULT_CURRENCY
    category: Category = "other"
    items: list[Item] = Field(default_factory=list)
    note: str | None = Field(None, max_length=5000)
    receipt_key: str | None = None


class ExpensePatch(_Fields):
    date: str | None = None
    merchant: str | None = Field(None, max_length=200)
    total: float | None = Field(None, ge=0)
    currency: str | None = None
    category: Category | None = None
    items: list[Item] | None = None
    note: str | None = Field(None, max_length=5000)
    receipt_key: str | None = None


# --------------------------------------------------------------------------- routes


@router.get("")
async def list_expenses(
    month: str | None = Query(None, pattern=_MONTH_RE),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    month = month or local_today().strftime("%Y-%m")
    rows = await db.fetch(
        "SELECT * FROM app_expenses WHERE date >= ? AND date <= ? ORDER BY date DESC, created_at DESC",
        [f"{month}-01", f"{month}-31"],
    )
    expenses = [_expense_out(r) for r in rows]
    return {"expenses": expenses, "summary": summarise(expenses)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_expense(body: ExpenseCreate, db: AppDB = Depends(_db)) -> dict[str, Any]:
    fields = body.model_dump()
    fields["date"] = fields["date"] or local_today().isoformat()
    # A category picked by hand wins over a merchant rule; "other" (or none sent) lets the rule decide.
    keep = "category" in body.model_fields_set and body.category != "other"
    return await _insert(db, fields, keep_category=keep)


def _image_type(filename: str | None, content_type: str | None) -> tuple[str, str]:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype not in _IMAGE_TYPES:
        guessed = mimetypes.guess_type(filename or "")[0] or ""
        if not guessed and filename and filename.lower().endswith((".heic", ".heif")):
            guessed = "image/heic"
        ctype = guessed.lower()
    if ctype not in _IMAGE_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="receipt must be a JPEG, PNG, WebP or HEIC image")
    return ("image/jpeg" if ctype == "image/jpg" else ctype), _IMAGE_TYPES[ctype]


@router.post("/receipt", status_code=status.HTTP_201_CREATED)
async def scan_receipt(
    file: UploadFile = File(...),
    note: str | None = Form(None),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    content_type, ext = _image_type(file.filename, file.content_type)
    image = await read_upload(file, state.hub.max_upload_bytes)
    today = local_today()
    key = f"receipts/{today:%Y}/{today:%m}/{new_id()}.{ext}"
    meta = {"original-filename": file.filename} if file.filename else None
    stored = await state.router.storage_put(key, image, content_type, meta)

    data = await read_receipt_file(state, image, content_type, today)
    fields, confidence = parse_receipt(data, today)
    fields["note"] = note.strip() if note and note.strip() else None
    fields["receipt_key"] = key
    fields["receipt_provider"] = stored.backend
    expense = await _insert(db, fields)
    return {"expense": expense, "confidence": confidence}


def search_terms(q: str) -> list[str]:
    """Up to 6 distinct words of at least 2 characters (LIKE wildcards are escaped by _like)."""
    out: list[str] = []
    for word in re.split(r"\s+", q.strip().lower()):
        word = word.strip(".,;:!?\"'()")
        if len(word) >= 2 and word not in out:
            out.append(word)
    return out[:6]


def _like(term: str) -> str:
    return "%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def _snippet(row: dict[str, Any], terms: list[str], width: int = 80) -> str | None:
    """A short excerpt of the receipt text (or the items) around the first matching word."""
    try:
        items = json.loads(row.get("items") or "[]")
    except ValueError:
        items = []
    names = ", ".join(str(i.get("name")) for i in items if isinstance(i, dict) and i.get("name")) if isinstance(items, list) else ""
    for source in (row.get("receipt_text"), names):
        text = re.sub(r"\s+", " ", str(source or ""))
        low = text.lower()
        for term in terms:
            at = low.find(term)
            if at >= 0:
                start = max(0, at - width // 2)
                piece = text[start : start + width].strip()
                return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")
    return None


def receipt_kind(key: str | None) -> Literal["image", "pdf"] | None:
    if not key:
        return None
    return "pdf" if key.lower().endswith(".pdf") else "image"


async def _receipt_url(state: HubState, db: AppDB, expense: dict[str, Any]) -> tuple[str | None, str | None]:
    """(presigned GET URL, backend) for the expense's receipt; (None, backend) when it cannot be presigned."""
    key = expense.get("receipt_key")
    if not key:
        return None, None
    provider = expense.get("receipt_provider")
    try:
        if not provider:
            obj = await asyncio.wait_for(state.router.storage_locate(key), PRESIGN_TIMEOUT_S)
            if obj is None:
                return None, None
            provider = obj.backend
            await db.execute("UPDATE app_expenses SET receipt_provider = ? WHERE id = ?", [provider, expense["id"]])
        url = await asyncio.wait_for(state.router.storage_presign(key, provider, PRESIGN_TTL_S), PRESIGN_TIMEOUT_S)
        return url, provider
    except Exception as exc:  # noqa: BLE001 - the result still lists the expense; the PWA falls back to a download
        log.info("receipt %s not presigned: %r", key, exc)
        return None, provider


@router.get("/search")
async def search_expenses(
    q: str = Query("", max_length=200),
    receipts_only: bool = Query(False, description="Only expenses with a receipt image or PDF"),
    limit: int = Query(30, ge=1, le=SEARCH_LIMIT),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Every word of ``q`` must appear in the merchant, the note, the items or the receipt's text (any order,
    case-insensitive); newest first. Results carry a presigned ``receipt_url`` (1 h) when there is a receipt."""
    terms = search_terms(q)
    if not terms and not receipts_only:
        raise HTTPException(422, detail="type at least one word of 2 characters")
    where: list[str] = []
    params: list[Any] = []
    for term in terms:
        where.append(
            "(lower(merchant) LIKE ? ESCAPE '!' OR lower(coalesce(note, '')) LIKE ? ESCAPE '!'"
            " OR lower(items) LIKE ? ESCAPE '!' OR lower(coalesce(receipt_text, '')) LIKE ? ESCAPE '!')"
        )
        params += [_like(term)] * 4
    if receipts_only:
        where.append("receipt_key IS NOT NULL AND receipt_key != ''")
    rows = await db.fetch(
        f"SELECT * FROM app_expenses WHERE {' AND '.join(where)} ORDER BY date DESC, created_at DESC LIMIT ?",
        [*params, limit],
        limit=limit,
    )
    expenses = [_expense_out(r) for r in rows]
    urls = await asyncio.gather(*(_receipt_url(state, db, e) for e in expenses))
    results = [
        {**e, "receipt_url": url, "receipt_provider": provider or e["receipt_provider"],
         "receipt_kind": receipt_kind(e["receipt_key"]), "snippet": _snippet(row, terms)}
        for e, row, (url, provider) in zip(expenses, rows, urls, strict=True)
    ]
    return {"q": q, "terms": terms, "results": results, "count": len(results)}


@router.get("/{expense_id}")
async def get_expense(expense_id: str, db: AppDB = Depends(_db)) -> dict[str, Any]:
    return _expense_out(await _get(db, expense_id))


@router.patch("/{expense_id}")
async def patch_expense(expense_id: str, body: ExpensePatch, db: AppDB = Depends(_db)) -> dict[str, Any]:
    await _get(db, expense_id)
    changes: dict[str, Any] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in ("note", "receipt_key"):
            raise HTTPException(422, detail=f"{field} cannot be null")
        if field == "items":
            value = json.dumps([i.model_dump() for i in value], ensure_ascii=False)
        changes[field] = value
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db.execute(f"UPDATE app_expenses SET {sets} WHERE id = ?", [*changes.values(), expense_id])
    return _expense_out(await _get(db, expense_id))


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(expense_id: str, db: AppDB = Depends(_db)) -> Response:
    await _get(db, expense_id)
    await db.execute("DELETE FROM app_expenses WHERE id = ?", [expense_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
