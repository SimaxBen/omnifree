"""/expenses: expense log with receipt scanning (image -> vision LLM -> structured expense).

Receipts are stored in the Drive under ``receipts/<yyyy>/<mm>/<id>.<ext>``; extraction runs on
vision-capable LLM providers only (Capability.VISION, Gemini first). Unreadable fields fall back to
safe defaults (currency MAD, today's date in Africa/Casablanca, category "other").
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, field_validator

from core.interfaces import Capability, ChatMessage, ChatRequest, ChatResponse, ProviderKind
from core.router import TOKEN_METRIC
from hub_api.appdata import AppDB, extract_json, get_appdb, local_today, new_id, now_iso, read_upload
from hub_api.state import HubState, get_state

__all__ = ["router", "CATEGORIES", "normalise_currency", "parse_receipt", "summarise", "SCHEMA"]

log = logging.getLogger("omnifree.hub.expenses")

router = APIRouter(prefix="/expenses", tags=["expenses"])

CATEGORIES = (
    "food", "groceries", "transport", "housing", "bills", "health",
    "shopping", "entertainment", "education", "travel", "other",
)
Category = Literal[
    "food", "groceries", "transport", "housing", "bills", "health",
    "shopping", "entertainment", "education", "travel", "other",
]
DEFAULT_CURRENCY = "MAD"
VISION_PREFERRED = "gemini"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_expenses ("
    " id TEXT PRIMARY KEY, date TEXT NOT NULL, merchant TEXT NOT NULL DEFAULT '',"
    " total REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'MAD',"
    " category TEXT NOT NULL DEFAULT 'other', items TEXT NOT NULL DEFAULT '[]', note TEXT,"
    " receipt_key TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_expenses_date ON app_expenses (date)",
)

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


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await db.ensure("expenses", SCHEMA)
    return db


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
        "created_at": row["created_at"],
    }


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


async def _insert(db: AppDB, fields: dict[str, Any]) -> dict[str, Any]:
    row = {
        "id": new_id(),
        "date": fields["date"],
        "merchant": fields.get("merchant") or "",
        "total": float(fields["total"]),
        "currency": fields.get("currency") or DEFAULT_CURRENCY,
        "category": fields.get("category") or "other",
        "items": json.dumps(fields.get("items") or []),
        "note": fields.get("note"),
        "receipt_key": fields.get("receipt_key"),
        "created_at": now_iso(),
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_expenses ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return _expense_out(row)


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
    return await _insert(db, fields)


_RECEIPT_PROMPT = (
    "You read photos of receipts and invoices (often Moroccan: French/Arabic, amounts in dirhams 'DH'/'MAD').\n"
    "Today is {today}. Answer with ONE JSON object only, no prose, no code fences:\n"
    '{{"merchant": "store name", "date": "YYYY-MM-DD or null", "total": number (the grand total paid), '
    '"currency": "ISO 4217 code (DH/Dhs = MAD) or null", "category": one of {categories}, '
    '"items": [{{"name": "...", "qty": number or null, "price": number or null}}], '
    '"confidence": "high|medium|low"}}\n'
    "Use null for anything unreadable; never invent amounts. confidence is low when the image is blurry, cut or not a receipt."
)


def _vision_candidates(state: HubState) -> list[str]:
    chain = state.registry.chain(ProviderKind.LLM)
    vision = [n for n in chain if Capability.VISION in state.registry.class_meta(n).get("capabilities", [])]
    if VISION_PREFERRED in vision:
        vision.remove(VISION_PREFERRED)
        vision.insert(0, VISION_PREFERRED)
    return vision


async def _vision_chat(state: HubState, request: ChatRequest) -> ChatResponse:
    """Chat on vision-capable providers only (Gemini first), with failover between them."""
    routed = await state.router.execute(
        ProviderKind.LLM,
        lambda a: a.chat(request),
        candidates=_vision_candidates(state),
        capability=Capability.VISION,
        op_name="chat",
    )
    resp: ChatResponse = routed.result
    resp.routed_via = resp.routed_via or routed.provider
    if resp.usage and resp.usage.total_tokens:
        await state.router.record_usage(routed.provider, TOKEN_METRIC, resp.usage.total_tokens)
    return resp


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
    await state.router.storage_put(key, image, content_type, meta)

    data_url = f"data:{content_type};base64,{base64.b64encode(image).decode('ascii')}"
    request = ChatRequest(
        messages=[
            ChatMessage(
                role="system",
                content=_RECEIPT_PROMPT.format(today=today.isoformat(), categories="|".join(CATEGORIES)),
            ),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Extract this receipt as JSON."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            ),
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    resp = await _vision_chat(state, request)
    data = extract_json(resp.content)
    if data is None:
        log.warning("receipt extraction returned no JSON (routed via %s)", resp.routed_via)
    fields, confidence = parse_receipt(data, today)
    fields["note"] = note.strip() if note and note.strip() else None
    fields["receipt_key"] = key
    expense = await _insert(db, fields)
    return {"expense": expense, "confidence": confidence}


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
            value = json.dumps([i.model_dump() for i in value])
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
