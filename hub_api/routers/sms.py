"""/money/sms: bank SMS alerts forwarded by an Android app become expenses, incomes and balances.

``POST /money/sms`` is a webhook for SMS-forwarder apps ("SMS Forwarder"
bogkonstantin/android_income_sms_gateway_webhook, "SMS to URL Forwarder"...). It authenticates itself:
the normal hub bearer token, or the dedicated vault key ``SMS_WEBHOOK_TOKEN`` sent as the
``X-OmniFree-Token`` header or, for apps that cannot set headers, as ``?token=`` (this route only).
The body is read tolerantly: JSON (``from|sender|phone``, ``text|message|body|content``,
``sentStamp|receivedStamp|timestamp|date``), form data or plain text.

One SMS (see :mod:`hub_api.bank_sms` for the parsing):

* card payment, direct debit, outgoing transfer -> an expense (``source = 'sms'``, ``source_ref`` = hash of
  sender + text + timestamp); skipped when the same purchase (merchant, amount, currency, day) is already
  recorded, e.g. from the email import;
* incoming transfer -> an income (``origin = 'sms'``);
* ATM withdrawal -> not spending: the money moves to the "Cash" account (its balance grows; created when
  missing), so it never counts in the month's spent total or the budgets;
* "solde" in the SMS -> the bank's account balance (account named after the bank, created when missing);
* OTPs, codes and ads -> ``200 {"ignored": reason}`` so the app does not retry; the OTP text is not stored.

Messages from a bank that no regex can read go to the LLM (JSON mode) once. Every SMS is logged in
``app_sms_log`` (masked card numbers) with its status; ``GET /money/sms/log`` lists the last ones.
``?dry_run=1`` parses and answers without writing anything (the PWA's "test an SMS" box).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from core.interfaces import ChatMessage, ChatRequest, ProviderError
from hub_api.appdata import AppDB, extract_json, get_appdb, local_today, local_tz, new_id, now_iso
from hub_api.auth import _bearer, token_matches
from hub_api.bank_sms import WALLETS, ParsedSms, guess_category, looks_like_bank, mask_text, parse_amount, parse_sms
from hub_api.routers import money
from hub_api.routers.expenses import insert_expense, normalise_currency
from hub_api.state import HubState, get_state

__all__ = ["router", "webhook_router", "SCHEMA", "SMS_TOKEN_KEY", "TOKEN_HEADER", "read_payload", "sms_ref"]

log = logging.getLogger("omnifree.hub.sms")

router = APIRouter(prefix="/money/sms", tags=["money"])  # hub token (added by app.py)
webhook_router = APIRouter(prefix="/money", tags=["money"])  # authenticates itself, see require_sms_token

SMS_TOKEN_KEY = "SMS_WEBHOOK_TOKEN"
TOKEN_HEADER = "X-OmniFree-Token"
MAX_BODY = 64 * 1024
LOG_KEEP = 200

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_sms_log ("
    " id TEXT PRIMARY KEY, ref TEXT NOT NULL, sender TEXT, bank TEXT, text TEXT, status TEXT NOT NULL, reason TEXT,"
    " kind TEXT, amount REAL, currency TEXT, parser TEXT, expense_id TEXT, income_id TEXT, account_id TEXT,"
    " received_at TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_sms_log_ref ON app_sms_log (ref)",
    "CREATE INDEX IF NOT EXISTS app_sms_log_created ON app_sms_log (created_at)",
)

_SENDER_KEYS = ("from", "sender", "phone", "phoneNumber", "phone_number", "number", "address", "originatingAddress", "msisdn")
_TEXT_KEYS = ("text", "message", "body", "content", "msg", "sms", "smsText")
_STAMP_KEYS = ("receivedStamp", "sentStamp", "timestamp", "received_at", "receivedAt", "sent_at", "sentAt", "date", "time")
_NESTED_KEYS = ("data", "sms", "payload", "message")


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await money.ensure_schema(db)
    await db.ensure("sms", SCHEMA)
    return db


# --------------------------------------------------------------------------- auth


def _sms_token(state: HubState) -> str | None:
    return state.settings.secret(SMS_TOKEN_KEY) or None


async def require_sms_token(request: Request, state: HubState = Depends(get_state)) -> str:
    """Hub bearer token, or SMS_WEBHOOK_TOKEN as header / ``?token=``; returns which one matched."""
    hub, sms = state.hub_token(), _sms_token(state)
    if not hub and not sms:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="no hub token or SMS_WEBHOOK_TOKEN configured")
    if token_matches(hub, _bearer(request.headers.get("authorization"))):
        return "hub"
    if sms and (
        token_matches(sms, request.headers.get(TOKEN_HEADER)) or token_matches(sms, request.query_params.get("token"))
    ):
        return "sms"
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token", headers={"WWW-Authenticate": "Bearer"}
    )


# --------------------------------------------------------------------------- payload


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "") and not isinstance(v, (dict, list)):
            return v
    return None


def _flatten(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    for k in _NESTED_KEYS:
        inner = data.get(k)
        if isinstance(inner, dict):
            out = {**inner, **{key: v for key, v in out.items() if not isinstance(v, dict)}}
    return out


def _stamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    tz = local_tz()
    if isinstance(value, str) and re.fullmatch(r"\d{9,14}(?:\.\d+)?", value.strip()):
        value = float(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        secs = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, timezone.utc).astimezone(tz)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    return None


_LENIENT_RE = re.compile(r'"(?P<k>[A-Za-z_]+)"\s*:\s*"(?P<v>.*?)"\s*(?=,\s*"[A-Za-z_]+"\s*:|\}\s*$)', re.S)


def _lenient_json(body: str) -> dict[str, Any]:
    """Forwarder templates like {"from":"%pni%","text":"%mb%"} break as JSON when the SMS itself has a quote or
    a line break: read the "key":"value" pairs anyway (the value runs up to the next key or the closing brace)."""
    return {m.group("k"): m.group("v").replace('\\"', '"') for m in _LENIENT_RE.finditer(body)}


def read_payload(raw: bytes, content_type: str, query: dict[str, str]) -> tuple[str, str, Any]:
    """(sender, text, raw timestamp) from a JSON, form or plain-text body (query parameters fill gaps)."""
    body = raw.decode("utf-8", errors="replace").strip()
    ctype = content_type.split(";", 1)[0].strip().lower()
    data: dict[str, Any] = {}
    if "json" in ctype or body.startswith(("{", "[")):
        try:
            parsed = json.loads(body) if body else {}
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if isinstance(parsed, dict):
            data = _flatten(parsed)
        elif isinstance(parsed, str):
            data = {"text": parsed}
        elif parsed is None:
            data = _lenient_json(body) or {"text": body}
    elif ctype == "application/x-www-form-urlencoded":
        data = dict(parse_qsl(body, keep_blank_values=True))
    elif body:
        data = {"text": body}
    merged = {**{k: v for k, v in query.items() if k not in ("token", "dry_run")}, **data}
    sender = str(_pick(merged, _SENDER_KEYS) or "").strip()[:100]
    text = str(_pick(merged, _TEXT_KEYS) or "").strip()[:2000]
    return sender, text, _pick(merged, _STAMP_KEYS)


def sms_ref(sender: str, text: str, stamp: Any) -> str:
    digest = hashlib.sha256(f"{sender.strip().lower()}|{text.strip()}|{stamp or ''}".encode()).hexdigest()
    return f"sms:{digest[:24]}"


# --------------------------------------------------------------------------- LLM fallback


def _llm_prompt(today: date) -> str:
    return (
        "You read one SMS sent by a Moroccan bank or mobile wallet (French, English or Arabic). "
        f"Today is {today.isoformat()}. Answer with ONE JSON object only, no prose:\n"
        '{"is_transaction": true|false, "type": "expense|income|withdrawal", "amount": number, '
        '"currency": "ISO 4217 (DH/DHS/dirham = MAD)", "merchant": "shop, payee or payer", '
        '"date": "YYYY-MM-DD or null", "balance": number or null, "balance_currency": "ISO 4217 or null"}\n'
        "expense = card payment, purchase, direct debit, outgoing transfer; income = money received; "
        "withdrawal = cash taken at an ATM (GAB/DAB). is_transaction is false for codes, OTPs, ads, card or "
        "account notices without a movement of money. Never invent amounts."
    )


async def _llm_parse(state: HubState, sender: str, text: str, today: date, fallback: ParsedSms) -> ParsedSms:
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content=_llm_prompt(today)),
            ChatMessage(role="user", content=f"Sender: {sender or 'unknown'}\nSMS: {text}"),
        ],
        temperature=0.0,
        max_tokens=400,
        response_format={"type": "json_object"},
    )
    resp = await state.router.chat(request)
    data = extract_json(resp.content) or {}
    if data.get("is_transaction") is not True:
        return ParsedSms("ignored", fallback.bank, reason="not a transaction (LLM)", parser="llm")
    kind = str(data.get("type") or "").lower()
    amount = data.get("amount")
    value = float(amount) if isinstance(amount, (int, float)) and not isinstance(amount, bool) else parse_amount(str(amount or ""))
    if kind not in ("expense", "income", "withdrawal") or value is None or value <= 0:
        return ParsedSms("unknown", fallback.bank, reason="unreadable (LLM)", parser="llm")
    merchant = str(data.get("merchant") or "").strip()[:80]
    day = str(data.get("date") or "")
    try:
        day = date.fromisoformat(day[:10]).isoformat() if date.fromisoformat(day[:10]) <= today else today.isoformat()
    except ValueError:
        day = fallback.date or today.isoformat()
    bal = data.get("balance")
    parsed = ParsedSms(
        kind,  # type: ignore[arg-type]
        fallback.bank,
        amount=round(abs(value), 2),
        currency=normalise_currency(data.get("currency")) or "MAD",
        merchant=merchant or ("Retrait GAB" if kind == "withdrawal" else "Virement reçu" if kind == "income" else "Paiement"),
        date=day,
        card=fallback.card,
        balance=float(bal) if isinstance(bal, (int, float)) and not isinstance(bal, bool) else fallback.balance,
        balance_currency=normalise_currency(data.get("balance_currency")) or fallback.balance_currency or "MAD",
        parser="llm",
    )
    parsed.category = "cash" if kind == "withdrawal" else "other" if kind == "income" else guess_category(merchant, text)
    if parsed.balance is None:
        parsed.balance_currency = None
    return parsed


# --------------------------------------------------------------------------- effects


async def _same_expense(db: AppDB, p: ParsedSms) -> dict[str, Any] | None:
    return await db.fetch_one(
        "SELECT id FROM app_expenses WHERE date = ? AND total = ? AND currency = ? AND lower(merchant) = lower(?) LIMIT 1",
        [p.date, p.amount, p.currency, p.merchant],
    )


async def _same_income(db: AppDB, p: ParsedSms) -> dict[str, Any] | None:
    return await db.fetch_one(
        "SELECT id FROM app_incomes WHERE date = ? AND amount = ? AND currency = ? AND lower(source) = lower(?) LIMIT 1",
        [p.date, p.amount, p.currency, p.merchant],
    )


async def _account(db: AppDB, name: str, kind: str, currency: str, balance: float) -> dict[str, Any]:
    """Set the balance of the account called ``name`` (created when missing)."""
    now = now_iso()
    row = await db.fetch_one("SELECT * FROM app_accounts WHERE lower(name) = lower(?)", [name])
    if row is None:
        row = {"id": new_id(), "name": name, "kind": kind, "balance": round(balance, 2), "currency": currency,
               "note": "Created from bank SMS", "updated_at": now, "created_at": now}
        cols = list(row)
        await db.execute(
            f"INSERT INTO app_accounts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
        )
        return money._account_out(row)
    await db.execute(
        "UPDATE app_accounts SET balance = ?, currency = ?, updated_at = ? WHERE id = ?",
        [round(balance, 2), currency, now, row["id"]],
    )
    return money._account_out({**row, "balance": round(balance, 2), "currency": currency, "updated_at": now})


async def _to_cash(db: AppDB, p: ParsedSms) -> dict[str, Any] | None:
    """An ATM withdrawal moves money to the Cash account (created with the withdrawn amount when missing)."""
    row = await db.fetch_one(
        "SELECT * FROM app_accounts WHERE kind = 'cash' ORDER BY (lower(name) = 'cash') DESC, created_at LIMIT 1"
    )
    amount = float(p.amount or 0)
    if row is None:
        return await _account(db, "Cash", "cash", p.currency, amount)
    if (row.get("currency") or "MAD") != p.currency:
        return None  # a MAD withdrawal cannot be added to a EUR cash account without guessing a rate
    return await _account(db, row["name"], "cash", p.currency, float(row.get("balance") or 0) + amount)


def _note(p: ParsedSms) -> str:
    parts = [f"SMS {p.bank}" if p.bank else "SMS"]
    if p.card:
        parts.append(f"card ****{p.card}")
    return ", ".join(parts)


OWNER_NAMES_KEY = "OWNER_NAMES"  # vault: the owner's names as banks write them, comma separated


def _is_owner(state: HubState, name: str) -> bool:
    """Same set of words as one of OWNER_NAMES ("OUHNINE ILYAS" == "Ilyas Ouhnine")."""
    words = set(re.findall(r"[a-z]+", name.lower()))
    for owner in (state.settings.secret(OWNER_NAMES_KEY) or "").split(","):
        if words and words == set(re.findall(r"[a-z]+", owner.lower())):
            return True
    return False


async def _log(db: AppDB, ref: str, sender: str, text: str | None, received: datetime | None, status_: str,
               reason: str | None, p: ParsedSms, ids: dict[str, str | None]) -> None:
    row = {
        "id": new_id(), "ref": ref, "sender": sender[:100] or None, "bank": p.bank, "text": text,
        "status": status_, "reason": reason, "kind": p.kind, "amount": p.amount, "currency": p.currency if p.amount else None,
        "parser": p.parser, "expense_id": ids.get("expense"), "income_id": ids.get("income"),
        "account_id": ids.get("account"), "received_at": received.isoformat() if received else None,
        "created_at": now_iso(),
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_sms_log ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    await db.execute(
        "DELETE FROM app_sms_log WHERE id NOT IN (SELECT id FROM app_sms_log ORDER BY created_at DESC, rowid DESC LIMIT ?)", [LOG_KEEP]
    )


async def handle_sms(
    state: HubState, db: AppDB, sender: str, text: str, stamp: Any, *, dry_run: bool = False
) -> dict[str, Any]:
    today = local_today()
    received = _stamp(stamp)
    parsed = parse_sms(sender, text, today, received)
    out: dict[str, Any] = {"status": "ignored", "dry_run": dry_run, "parsed": None, "expense": None, "income": None,
                           "account": None, "cash_account": None}
    if parsed.kind == "unknown" and looks_like_bank(sender, text) and re.search(r"\d", text):
        try:
            parsed = await _llm_parse(state, sender, text, today, parsed)
        except ProviderError as exc:
            log.warning("bank SMS LLM fallback failed: %s", exc)
            parsed.reason = "could not read it (no LLM available)"
    elif parsed.kind == "unknown" and not looks_like_bank(sender, text):
        parsed.reason = "not a bank message"
    beneficiary = parsed.extra.get("beneficiary")
    if beneficiary and _is_owner(state, beneficiary):
        parsed.kind, parsed.reason = "ignored", "transfer to your own account (not spending)"
    if re.fullmatch(r"\s*%\w+%\s*", text or "") or re.fullmatch(r"\s*%\w+%\s*", sender or ""):
        parsed.kind, parsed.reason = "ignored", "test from the forwarding app (its %placeholders% were not filled in)"
    out["parsed"] = parsed.public()
    ref = sms_ref(sender, text, stamp)
    otp = parsed.kind == "ignored" and parsed.reason == "one-time code"
    # Only bank messages keep their (masked) text: personal SMS forwarded by an "all senders" rule are never stored.
    private = otp or parsed.reason == "not a bank message"
    stored_text = None if private else mask_text(text)

    if parsed.kind in ("ignored", "unknown"):
        reason = parsed.reason or "not a transaction"
        out["ignored"] = reason
        if not dry_run:
            await _log(db, ref, sender, stored_text, received, "ignored", reason, parsed, {})
        return out

    seen = await db.fetch_one("SELECT id FROM app_sms_log WHERE ref = ? AND status = 'created' LIMIT 1", [ref])
    if seen:
        out["ignored"] = "already received"
        return out

    ids: dict[str, str | None] = {}
    reason: str | None = None
    if parsed.kind == "expense":
        same = await _same_expense(db, parsed)
        if same:
            reason = f"same purchase as expense {same['id']}"
        elif not dry_run:
            out["expense"] = await insert_expense(
                db,
                {"date": parsed.date, "merchant": parsed.merchant, "total": parsed.amount, "currency": parsed.currency,
                 "category": parsed.category, "note": _note(parsed), "source": "sms", "source_ref": ref},
            )
            ids["expense"] = out["expense"]["id"]
    elif parsed.kind == "income":
        same = await _same_income(db, parsed)
        if same:
            reason = f"same income as {same['id']}"
        elif not dry_run:
            out["income"] = await money.insert_income(
                db,
                {"date": parsed.date, "amount": parsed.amount, "currency": parsed.currency, "source": parsed.merchant,
                 "category": parsed.category, "note": _note(parsed), "origin": "sms"},
            )
            ids["income"] = out["income"]["id"]
    elif parsed.kind == "withdrawal" and not dry_run:
        out["cash_account"] = await _to_cash(db, parsed)

    if parsed.balance is not None and parsed.bank and not dry_run:
        kind = "other" if parsed.bank in WALLETS else "bank"
        out["account"] = await _account(db, parsed.bank, kind, parsed.balance_currency or "MAD", parsed.balance)
        ids["account"] = out["account"]["id"]

    if reason:
        out["ignored"] = reason
        out["status"] = "ignored"
    else:
        out["status"] = "created"
    if not dry_run:
        await _log(db, ref, sender, stored_text, received, out["status"], reason, parsed, ids)
    return out


# --------------------------------------------------------------------------- routes


@webhook_router.post("/sms")
async def receive_sms(
    request: Request,
    dry_run: bool = Query(False),
    _auth: str = Depends(require_sms_token),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, detail=f"body exceeds {MAX_BODY} bytes")
    query = dict(request.query_params)
    if raw and request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        sender, text, stamp = read_payload(
            json.dumps({k: str(v) for k, v in form.items()}).encode(), "application/json", query
        )
    else:
        sender, text, stamp = read_payload(raw, request.headers.get("content-type", ""), query)
    if not text:
        return {"status": "ignored", "ignored": "empty message", "dry_run": dry_run}
    return await handle_sms(state, db, sender, text, stamp, dry_run=dry_run)


@router.get("/log")
async def sms_log(limit: int = Query(20, ge=1, le=LOG_KEEP), db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_sms_log ORDER BY created_at DESC, rowid DESC LIMIT ?", [limit], limit=limit)
    return {
        "messages": [
            {k: r.get(k) for k in ("id", "sender", "bank", "text", "status", "reason", "kind", "amount", "currency",
                                   "parser", "expense_id", "income_id", "account_id", "received_at", "created_at")}
            for r in rows
        ]
    }


@router.get("/setup")
async def sms_setup(state: HubState = Depends(get_state)) -> dict[str, Any]:
    """What the PWA shows to set up a forwarder app (the SMS token is only revealed to the hub token holder)."""
    token = _sms_token(state)
    return {
        "configured": bool(token),
        "token": token,
        "token_key": SMS_TOKEN_KEY,
        "header": TOKEN_HEADER,
        "query_param": "token",
        "path": "/money/sms",
        "public_base_url": state.hub.public_base_url or None,
    }
