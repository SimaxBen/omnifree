"""/inbox/receipts: import purchase receipts from one or more mailboxes as expenses.

No Google Cloud project and no OAuth app: each mailbox is read over IMAP (``imap.gmail.com:993`` for
Gmail and Google Workspace, any IMAP host otherwise) with an **app password**. The accounts come from
the vault (see docs/GMAIL.md):

* ``INBOX_ACCOUNTS``: JSON list ``[{"name": "perso", "user": "me@gmail.com", "host": "imap.gmail.com",
  "port": 993, "password_key": "INBOX_PW_PERSO"}, ...]``; each password lives in its own vault key
  (``password_key``), never inside the JSON;
* backward compatible single account: ``GMAIL_IMAP_USER`` + ``GMAIL_APP_PASSWORD``.

The blocking :mod:`imaplib` calls run in a worker thread, each with a timeout.

One sync (``POST /inbox/receipts/sync``, also the target of a daily QStash schedule):

1. accounts are visited least recently synced first (an account the previous run did not reach goes
   first), each one isolated: a login failure or timeout on one mailbox does not stop the others;
2. search for receipt-looking messages of the last ``days`` days: Gmail's ``X-GM-RAW`` in "All Mail" on
   Gmail/Workspace hosts, standard IMAP ``SEARCH SINCE ... OR SUBJECT ...`` in INBOX elsewhere;
3. messages already processed (table ``app_inbox_seen``) or imported (``app_expenses.source_ref`` =
   ``<mailbox>#<Message-ID>``) are skipped: imports are deduplicated per (mailbox, Message-ID);
4. for up to ``max`` new messages per call, newest first: a PDF/image attachment goes to the receipt
   vision pipeline (PDFs fall back to their text), otherwise the HTML/text body goes to a text LLM; the
   result is validated like a scanned receipt and stored as an expense (``source = 'email'``), the
   attachment under ``receipts/email/<yyyy>/<mm>/`` in the Drive;
5. the call stops starting new work once ~50 s are used (serverless limit 60 s) and reports what is
   left; the next run continues with the accounts and messages it did not reach.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import email.utils
import imaplib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from hub_api.appdata import AppDB, get_appdb, local_today, new_id, now_iso
from hub_api.rag_extract import ExtractionError, extract, html_to_text
from hub_api.receipts import read_receipt_file, read_receipt_text
from hub_api.routers.expenses import _valid_date, ensure_schema, insert_expense, parse_receipt
from hub_api.state import HubState, get_state

__all__ = [
    "router", "SCHEMA", "Account", "Mailbox", "imap_factory", "load_accounts", "search_query",
    "imap_search_criteria", "parse_email", "pick_attachment", "body_text", "split_source_ref",
]

log = logging.getLogger("omnifree.hub.inbox")

router = APIRouter(prefix="/inbox", tags=["inbox"])

GMAIL_HOST = "imap.gmail.com"
DEFAULT_PORT = 993
ACCOUNTS_KEY = "INBOX_ACCOUNTS"
USER_KEY = "GMAIL_IMAP_USER"  # legacy single account
PASSWORD_KEY = "GMAIL_APP_PASSWORD"

SEARCH_TERMS = (
    '(receipt OR reçu OR facture OR invoice OR "votre commande" OR "order confirmation" '
    "OR ticket OR paiement OR payment) -in:spam"
)
# Standard IMAP SEARCH (non-Gmail hosts): ASCII subject keywords, substring match, case-insensitive.
SUBJECT_KEYWORDS = ("receipt", "facture", "invoice", "commande", "order", "ticket", "paiement", "payment", "recu")
TIME_BUDGET_S = 50.0  # whole call; Vercel kills the function at 60 s
MESSAGE_RESERVE_S = 12.0  # do not start a message with less time than this left
ACCOUNT_RESERVE_S = 15.0  # do not open another mailbox with less time than this left
IMAP_TIMEOUT_S = 15.0  # socket timeout and per-command wait
MAX_SEARCH = 100  # newest candidates considered per mailbox and call
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MIN_IMAGE_BYTES = 10_000  # smaller inline images are logos and tracking pixels
_STATE_KEY = "receipts_sync"
_ACCOUNT_STATE = "account:"

_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/heif": "heif"}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_inbox_seen ("
    " message_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT, expense_id TEXT,"
    " subject TEXT, sender TEXT, processed_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_inbox_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
)


def _default_imap(host: str, port: int, timeout: float) -> Any:
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


# Factory for the IMAP connection: ``(host, port, timeout) -> imaplib.IMAP4``-like object.
# Tests replace it with a fake server object.
imap_factory: Callable[[str, int, float], Any] = _default_imap


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await ensure_schema(db)
    await db.ensure("inbox", SCHEMA)
    return db


# --------------------------------------------------------------------------- accounts


@dataclass(frozen=True)
class Account:
    name: str
    user: str
    host: str
    port: int
    password_key: str
    password: str | None  # None = the password key is not set

    @property
    def gmail(self) -> bool:
        """Gmail and Google Workspace (X-GM-RAW search, "All Mail")."""
        return self.host.lower() in (GMAIL_HOST, "imap.googlemail.com")

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "user": self.user, "host": self.host, "port": self.port,
                "password_key": self.password_key, "configured": self.configured}


def _password(state: HubState, key: str) -> str | None:
    value = state.settings.secret(key) if key else None
    return re.sub(r"\s+", "", value) if value else None  # Google shows app passwords as "abcd efgh ijkl mnop"


def load_accounts(state: HubState) -> tuple[list[Account], list[str]]:
    """(accounts, configuration problems). ``INBOX_ACCOUNTS`` first, then the legacy Gmail pair."""
    accounts: list[Account] = []
    problems: list[str] = []
    raw = state.settings.secret(ACCOUNTS_KEY)
    if raw:
        try:
            entries = json.loads(raw)
        except ValueError as exc:
            entries = []
            problems.append(f"{ACCOUNTS_KEY} is not valid JSON: {exc}")
        if not isinstance(entries, list):
            problems.append(f"{ACCOUNTS_KEY} must be a JSON list of accounts")
            entries = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or not str(entry.get("user") or "").strip():
                problems.append(f"{ACCOUNTS_KEY}[{i}] needs at least a 'user'")
                continue
            if "password" in entry:
                problems.append(f"{ACCOUNTS_KEY}[{i}]: put the password in its own vault key ('password_key'), not in the JSON")
            user = str(entry["user"]).strip()
            name = str(entry.get("name") or user).strip()
            if any(a.name == name for a in accounts):
                problems.append(f"{ACCOUNTS_KEY}[{i}]: duplicate account name {name!r}")
                continue
            key = str(entry.get("password_key") or "").strip()
            try:
                port = int(entry.get("port") or DEFAULT_PORT)
            except (TypeError, ValueError):
                problems.append(f"{ACCOUNTS_KEY}[{i}]: port must be a number")
                continue
            host = str(entry.get("host") or GMAIL_HOST).strip()
            accounts.append(Account(name, user, host, port, key, _password(state, key)))
    legacy_user = (state.settings.secret(USER_KEY) or "").strip()
    if legacy_user and not any(a.user.lower() == legacy_user.lower() for a in accounts):
        name = "gmail" if not any(a.name == "gmail" for a in accounts) else legacy_user
        accounts.append(Account(name, legacy_user, GMAIL_HOST, DEFAULT_PORT, PASSWORD_KEY, _password(state, PASSWORD_KEY)))
    return accounts, problems


def split_source_ref(ref: str | None) -> tuple[str | None, str | None]:
    """``"<mailbox>#<message-id>"`` -> (mailbox, message id); legacy refs have no mailbox."""
    if not ref:
        return None, None
    mailbox, sep, mid = ref.partition("#")
    return (mailbox, mid) if sep and "@" in mailbox else (None, ref)


# --------------------------------------------------------------------------- IMAP (blocking, worker thread)


class MailError(Exception):
    """IMAP said NO/BAD or answered something unexpected."""


_UID_RE = re.compile(rb"UID (\d+)")
_LIST_NAME_RE = re.compile(rb'(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$')
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def search_query(days: int) -> str:
    """Gmail search (X-GM-RAW) for receipt-looking messages."""
    return f"newer_than:{days}d {SEARCH_TERMS}"


def imap_search_criteria(days: int, today: date) -> list[str]:
    """Standard IMAP SEARCH criteria: ``SINCE <date> OR SUBJECT a OR SUBJECT b ... SUBJECT z``."""
    since = today - timedelta(days=days)
    criteria = ["SINCE", f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"]
    for kw in SUBJECT_KEYWORDS[:-1]:
        criteria += ["OR", "SUBJECT", kw]
    criteria += ["SUBJECT", SUBJECT_KEYWORDS[-1]]  # OR is binary and prefix: OR a OR b ... c
    return criteria


class Mailbox:
    """Thin synchronous wrapper over an ``imaplib.IMAP4`` connection, opened read-only."""

    def __init__(self, conn: Any, gmail: bool) -> None:
        self.conn = conn
        self.gmail = gmail

    @classmethod
    def open(cls, account: Account, timeout: float) -> Mailbox:
        conn = imap_factory(account.host, account.port, timeout)
        box = cls(conn, account.gmail)
        try:
            conn.login(account.user, account.password or "")
            typ, _ = conn.select(box._all_mail() if account.gmail else "INBOX", readonly=True)
            if typ != "OK":
                raise MailError("cannot open the mailbox")
        except BaseException:
            box.close()
            raise
        return box

    def _all_mail(self) -> str:
        """The mailbox flagged ``\\All`` (its name depends on the account language), else INBOX."""
        with contextlib.suppress(Exception):
            typ, lines = self.conn.list()
            if typ == "OK":
                for line in lines or []:
                    if isinstance(line, bytes) and b"\\All" in line:
                        m = _LIST_NAME_RE.search(line)
                        if m:
                            name = (m.group(1) or m.group(2)).decode("utf-8", "replace")
                            return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return "INBOX"

    def search(self, days: int, today: date) -> list[str]:
        """UIDs of receipt-looking messages, newest first."""
        if self.gmail:
            self.conn.literal = search_query(days).encode("utf-8")
            typ, data = self.conn.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        else:
            typ, data = self.conn.uid("SEARCH", *imap_search_criteria(days, today))
        if typ != "OK":
            raise MailError(f"search failed: {data!r}")
        raw = data[0] if data and data[0] else b""
        uids = [u.decode() for u in raw.split()]
        return sorted(uids, key=int, reverse=True)

    def _fetch(self, uids: list[str], what: str) -> dict[str, bytes]:
        typ, data = self.conn.uid("FETCH", ",".join(uids), what)
        if typ != "OK":
            raise MailError(f"fetch failed: {data!r}")
        out: dict[str, bytes] = {}
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                m = _UID_RE.search(item[0])
                if m:
                    out[m.group(1).decode()] = item[1]
        return out

    def headers(self, uids: list[str]) -> dict[str, bytes]:
        if not uids:
            return {}
        return self._fetch(uids, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")

    def message(self, uid: str) -> bytes:
        raw = self._fetch([uid], "(UID BODY.PEEK[])").get(uid)
        if raw is None:
            raise MailError(f"message {uid} vanished")
        return raw

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.logout()


# --------------------------------------------------------------------------- message parsing (pure)


def parse_email(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


def header_info(raw_headers: bytes, uid: str) -> dict[str, Any]:
    msg = parse_email(raw_headers)
    message_id = str(msg.get("Message-ID") or "").strip().strip("<>").strip()
    day: str | None = None
    with contextlib.suppress(Exception):
        parsed = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        day = parsed.date().isoformat()
    return {
        "uid": uid,
        "message_id": message_id or f"gmail-uid:{uid}",
        "subject": str(msg.get("Subject") or "").strip()[:300],
        "sender": str(msg.get("From") or "").strip()[:300],
        "date": day,
    }


def _sender_name(sender: str) -> str:
    name, addr = email.utils.parseaddr(sender)
    if name.strip():
        return name.strip()
    domain = addr.rpartition("@")[2]
    return domain.split(".")[-2].capitalize() if domain.count(".") >= 1 else addr


def pick_attachment(msg: EmailMessage) -> tuple[bytes, str, str] | None:
    """The receipt-looking attachment: the first PDF, else the largest real image. ``(data, type, name)``."""
    pdf: tuple[bytes, str, str] | None = None
    images: list[tuple[bytes, str, str]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type().lower()
        name = part.get_filename() or ""
        is_pdf = ctype == "application/pdf" or (ctype == "application/octet-stream" and name.lower().endswith(".pdf"))
        is_image = ctype in _IMAGE_EXT or ctype == "image/jpg"
        if not (is_pdf or is_image):
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            continue
        if is_pdf:
            pdf = pdf or (data, "application/pdf", name or "receipt.pdf")
        elif len(data) >= MIN_IMAGE_BYTES:
            images.append((data, "image/jpeg" if ctype == "image/jpg" else ctype, name or "receipt"))
    if pdf:
        return pdf
    return max(images, key=lambda i: len(i[0])) if images else None


def body_text(msg: EmailMessage) -> str:
    """Readable text of the email: the HTML part with tags stripped, else the plain-text part."""
    for kind in ("html", "plain"):
        part = msg.get_body(preferencelist=(kind,))
        if part is None:
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        if not isinstance(content, str):
            continue
        text = html_to_text(content)[1] if kind == "html" else content
        text = re.sub(r"[ \t ]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if text:
            return text
    return ""


def _usable(data: dict[str, Any] | None) -> bool:
    if not data or data.get("is_receipt") is False:
        return False
    fields, _ = parse_receipt(data, date.today())
    return fields["total"] > 0


# --------------------------------------------------------------------------- models / state


class SyncBody(BaseModel):
    days: int = Field(7, ge=1, le=90)
    max: int = Field(15, ge=1, le=50)
    dry_run: bool = False
    account: str | None = Field(None, description="Sync only this mailbox (account name or address)")


async def _load_state(db: AppDB, key: str) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT value FROM app_inbox_state WHERE key = ?", [key])
    if row is None:
        return None
    try:
        value = json.loads(row["value"])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _save_state(db: AppDB, key: str, value: dict[str, Any]) -> None:
    await db.execute(
        "INSERT INTO app_inbox_state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [key, json.dumps(value, ensure_ascii=False), now_iso()],
    )


async def _known_refs(db: AppDB, refs: list[str]) -> dict[str, str]:
    """ref (``mailbox#message-id``) -> reason, for messages already processed or imported."""
    known: dict[str, str] = {}
    for i in range(0, len(refs), 50):
        chunk = refs[i : i + 50]
        marks = ", ".join("?" for _ in chunk)
        for row in await db.fetch(f"SELECT message_id, status FROM app_inbox_seen WHERE message_id IN ({marks})", chunk):
            known[row["message_id"]] = "already imported" if row["status"] == "imported" else "already processed"
        for row in await db.fetch(
            f"SELECT source_ref FROM app_expenses WHERE source = 'email' AND source_ref IN ({marks})", chunk
        ):
            known[row["source_ref"]] = "already imported"
    return known


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "the mail server did not answer in time"
    detail = str(exc) or type(exc).__name__
    if "AUTHENTICATIONFAILED" in detail.upper() or "invalid credentials" in detail.lower():
        return "login failed: check the address and the app password (IMAP must be enabled)"
    return detail[:300]


# --------------------------------------------------------------------------- one message


class _Clock:
    def __init__(self, budget: float) -> None:
        self.deadline = time.monotonic() + budget

    def left(self) -> float:
        return self.deadline - time.monotonic()

    async def bound(self, coro: Any, cap: float | None = None) -> Any:
        limit = max(self.left() - 1.0, 0.5)
        if cap is not None:
            limit = min(limit, cap)
        return await asyncio.wait_for(coro, limit)


async def _extract(state: HubState, msg: EmailMessage, meta: dict[str, Any], today: date, clock: _Clock) -> tuple[dict[str, Any] | None, tuple[bytes, str, str] | None, str]:
    """(receipt JSON, attachment used or None, how) for one message."""
    context = f"Email from: {meta['sender']}\nSubject: {meta['subject']}\nDate: {meta['date'] or 'unknown'}"
    attachment = pick_attachment(msg)
    data: dict[str, Any] | None = None
    if attachment is not None:
        blob, ctype, _name = attachment
        try:
            data = await clock.bound(read_receipt_file(state, blob, ctype, today, hint=context))
        except TimeoutError:
            raise
        except Exception as exc:  # vision provider refused the file (e.g. a PDF): fall back to its text
            log.info("vision read of %s failed (%r), falling back to text", meta["message_id"], exc)
            data = None
        if _usable(data):
            return data, attachment, "attachment"
        if ctype == "application/pdf":
            try:
                pages = extract("receipt.pdf", blob).pages
                pdf_text = "\n".join(text for _p, text in pages)
            except ExtractionError:
                pdf_text = ""
            if pdf_text.strip():
                data = await clock.bound(read_receipt_text(state, pdf_text, today, context))
                if _usable(data):
                    data.setdefault("text", pdf_text)  # kept as receipt_text for GET /expenses/search
                    return data, attachment, "pdf-text"
    text = body_text(msg)
    if not text:
        return data, None, "empty"
    data = await clock.bound(read_receipt_text(state, text, today, context))
    if isinstance(data, dict):
        data.setdefault("text", text)  # kept as receipt_text for GET /expenses/search
    return data, None, "body"


async def _process(
    state: HubState, db: AppDB, raw: bytes, meta: dict[str, Any], today: date, dry_run: bool, clock: _Clock
) -> tuple[dict[str, Any] | None, str | None]:
    """(expense or None, skip reason or None)."""
    msg = parse_email(raw)
    data, attachment, how = await _extract(state, msg, meta, today, clock)
    if how == "empty":
        return None, "empty message"
    if data is None:
        return None, "unreadable"
    if data.get("is_receipt") is False:
        return None, "not a receipt"
    if _valid_date(data.get("date")) is None and meta.get("date"):
        data["date"] = meta["date"]
    fields, _confidence = parse_receipt(data, today)
    if fields["total"] <= 0:
        return None, "no amount found"
    if fields["merchant"] == "Unknown" and meta["sender"]:
        fields["merchant"] = _sender_name(meta["sender"])[:200] or "Unknown"
    note = f"Email: {meta['subject']}" if meta["subject"] else "Email receipt"
    sender = f"from {meta['sender']}, " if meta["sender"] else ""
    fields["note"] = f"{note} ({sender}mailbox {meta['mailbox']})"[:5000]
    fields["source"] = "email"
    fields["source_ref"] = meta["ref"]
    # One purchase often sends several emails (order, payment and exchange summaries): same merchant,
    # amount, currency and day as an expense already recorded -> treat it as the same purchase.
    same = await db.fetch(
        "SELECT id FROM app_expenses WHERE date = ? AND total = ? AND currency = ? AND lower(merchant) = lower(?) LIMIT 1",
        [fields["date"], fields["total"], fields["currency"], fields["merchant"]],
    )
    if same:
        return None, f"same purchase as expense {same[0]['id']}"
    if dry_run:
        preview = {k: v for k, v in fields.items() if k != "receipt_text"}
        return {**preview, "id": None, "receipt_key": None, "created_at": None}, None
    if attachment is not None:
        blob, ctype, name = attachment
        ext = "pdf" if ctype == "application/pdf" else _IMAGE_EXT.get(ctype, "bin")
        key = f"receipts/email/{today:%Y}/{today:%m}/{new_id()}.{ext}"
        stored = await clock.bound(state.router.storage_put(key, blob, ctype, {"original-filename": name[:200], "source": "email", "mailbox": meta["mailbox"]}))
        fields["receipt_key"] = key
        fields["receipt_provider"] = stored.backend
    return await insert_expense(db, fields), None


# --------------------------------------------------------------------------- sync


async def _thread(fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), max(timeout, 0.5))


class _Run:
    """Shared state of one sync call across accounts."""

    def __init__(self, opts: SyncBody, today: date) -> None:
        self.opts = opts
        self.today = today
        self.clock = _Clock(TIME_BUDGET_S)
        self.processed = 0  # new messages read in this call (``max`` is per call)
        self.result: dict[str, Any] = {
            "scanned": 0, "matched": 0, "created": [], "skipped": [], "errors": [], "remaining": 0,
            "dry_run": opts.dry_run, "days": opts.days, "accounts": [],
        }


async def _sync_account(state: HubState, db: AppDB, account: Account, run: _Run) -> dict[str, Any]:
    """Sync one mailbox; never raises (its failure is reported, the other accounts carry on)."""
    opts, clock, result = run.opts, run.clock, run.result
    summary: dict[str, Any] = {"name": account.name, "user": account.user, "scanned": 0, "created": 0,
                               "skipped": 0, "remaining": 0, "error": None, "complete": False}
    if not account.configured:
        summary["error"] = f"password not set (vault key {account.password_key or '?'})"
        result["errors"].append({"account": account.name, "message_id": None, "error": summary["error"]})
        return summary
    box: Mailbox | None = None
    try:
        box = await _thread(Mailbox.open, account, IMAP_TIMEOUT_S, timeout=min(IMAP_TIMEOUT_S, clock.left() - 1))
        uids = (await _thread(box.search, opts.days, run.today, timeout=min(IMAP_TIMEOUT_S, clock.left() - 1)))[:MAX_SEARCH]
        summary["scanned"] = len(uids)
        result["scanned"] += len(uids)
        raw_headers = await _thread(box.headers, uids, timeout=min(IMAP_TIMEOUT_S, clock.left() - 1))
        metas = []
        for uid in uids:
            meta = header_info(raw_headers.get(uid, b""), uid)
            meta["mailbox"] = account.user
            meta["ref"] = f"{account.user}#{meta['message_id']}"
            metas.append(meta)
        known = await _known_refs(db, [m["ref"] for m in metas])
        for meta in metas:
            mid, ref = meta["message_id"], meta["ref"]
            if ref in known:
                result["skipped"].append({"account": account.name, "message_id": mid, "reason": known[ref]})
                summary["skipped"] += 1
                continue
            if run.processed >= opts.max or clock.left() < MESSAGE_RESERVE_S:
                summary["remaining"] += 1
                continue
            known[ref] = "duplicate in this mailbox"
            if not opts.dry_run:
                claimed = await db.execute(
                    "INSERT OR IGNORE INTO app_inbox_seen (message_id, status, subject, sender, processed_at)"
                    " VALUES (?, 'processing', ?, ?, ?)",
                    [ref, meta["subject"], meta["sender"], now_iso()],
                )
                if not claimed:
                    result["skipped"].append({"account": account.name, "message_id": mid, "reason": "already processed"})
                    summary["skipped"] += 1
                    continue
            run.processed += 1
            try:
                raw = await _thread(box.message, meta["uid"], timeout=min(IMAP_TIMEOUT_S, clock.left() - 1))
                expense, reason = await _process(state, db, raw, meta, run.today, opts.dry_run, clock)
            except Exception as exc:
                if not opts.dry_run:  # release the claim: the next run retries this message
                    await db.execute("DELETE FROM app_inbox_seen WHERE message_id = ?", [ref])
                text = "timed out" if isinstance(exc, TimeoutError) else (str(exc) or type(exc).__name__)
                log.warning("email receipt %s failed: %r", ref, exc)
                result["errors"].append({"account": account.name, "message_id": mid, "error": text[:300]})
                continue
            if expense is not None:
                result["matched"] += 1
                result["created"].append(expense)
                summary["created"] += 1
            else:
                result["skipped"].append({"account": account.name, "message_id": mid, "reason": reason, "subject": meta["subject"]})
                summary["skipped"] += 1
            if not opts.dry_run:
                await db.execute(
                    "UPDATE app_inbox_seen SET status = ?, reason = ?, expense_id = ?, processed_at = ? WHERE message_id = ?",
                    ["imported" if expense else "skipped", reason, expense["id"] if expense else None, now_iso(), ref],
                )
        summary["complete"] = summary["remaining"] == 0
    except Exception as exc:  # login, network, timeout: isolate this mailbox
        log.warning("mailbox %s failed: %r", account.name, exc)
        summary["error"] = _error_text(exc)
        result["errors"].append({"account": account.name, "message_id": None, "error": summary["error"]})
    finally:
        if box is not None:
            with contextlib.suppress(Exception):
                await _thread(box.close, timeout=5.0)
    result["remaining"] += summary["remaining"]
    return summary


def _pick_accounts(accounts: list[Account], wanted: str | None) -> list[Account]:
    if not wanted:
        return accounts
    w = wanted.strip().lower()
    chosen = [a for a in accounts if a.name.lower() == w or a.user.lower() == w]
    if not chosen:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"unknown mailbox {wanted!r}")
    return chosen


# --------------------------------------------------------------------------- routes


@router.post("/receipts/sync")
async def sync_receipts(
    body: SyncBody | None = Body(None),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> dict[str, Any]:
    """Import receipts from every mailbox (or ``account``). The body is optional so QStash can POST nothing."""
    opts = body or SyncBody()
    accounts, problems = load_accounts(state)
    if not any(a.configured for a in accounts):
        detail = "; ".join(problems) or (
            f"no mailbox configured: store {ACCOUNTS_KEY} (+ one password key per account) or "
            f"{USER_KEY}/{PASSWORD_KEY} in the vault (see docs/GMAIL.md)"
        )
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)
    selected = _pick_accounts(accounts, opts.account)
    previous = {a.name: (await _load_state(db, _ACCOUNT_STATE + a.name) or {}) for a in selected}
    # Least recently synced first: a mailbox the last run did not reach or finish goes first.
    order = sorted(
        selected,
        key=lambda a: (bool(previous[a.name].get("complete")), previous[a.name].get("last_sync_at") or ""),
    )
    run = _Run(opts, local_today())
    for account in order:
        if run.clock.left() < ACCOUNT_RESERVE_S:
            run.result["accounts"].append({"name": account.name, "user": account.user, "scanned": 0, "created": 0,
                                           "skipped": 0, "remaining": None, "error": "not reached (time budget)",
                                           "complete": False})
            continue
        summary = await _sync_account(state, db, account, run)
        run.result["accounts"].append(summary)
        await _save_state(db, _ACCOUNT_STATE + account.name, {
            "last_sync_at": now_iso() if summary["error"] is None else previous[account.name].get("last_sync_at"),
            "last_attempt_at": now_iso(),
            "last_error": summary["error"],
            "complete": summary["complete"],
            "last_result": {k: v for k, v in summary.items() if k not in ("name", "user")},
        })
    result = run.result
    result["elapsed_s"] = round(TIME_BUDGET_S - run.clock.left(), 1)
    saved = {k: v for k, v in result.items() if k not in ("created", "skipped")}
    saved["created"] = [{"id": e.get("id"), "merchant": e.get("merchant"), "total": e.get("total"),
                         "currency": e.get("currency"), "date": e.get("date"), "source_ref": e.get("source_ref")}
                        for e in result["created"]]
    saved["skipped"] = result["skipped"][:50]
    await _save_state(db, _STATE_KEY, {"last_sync_at": now_iso(), "last_result": saved})
    if result["created"] and not opts.dry_run:
        from hub_api.routers.notify import push_inbox_import  # best effort, never fails the import

        await push_inbox_import(state, db, result)
    return result


@router.get("/receipts/status")
async def receipts_status(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    accounts, problems = load_accounts(state)
    saved = await _load_state(db, _STATE_KEY) or {}
    per_account = []
    for a in accounts:
        st = await _load_state(db, _ACCOUNT_STATE + a.name) or {}
        per_account.append({
            **a.public(),
            "last_sync_at": st.get("last_sync_at"),
            "last_attempt_at": st.get("last_attempt_at"),
            "last_error": st.get("last_error"),
            "last_result": st.get("last_result"),
        })
    return {
        "configured": any(a.configured for a in accounts),
        "accounts": per_account,
        "problems": problems,
        "last_sync_at": saved.get("last_sync_at"),
        "last_result": saved.get("last_result"),
    }
