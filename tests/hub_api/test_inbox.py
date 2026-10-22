"""/inbox/receipts: Gmail (IMAP, app password) -> expenses, with a fake IMAP server and scripted LLMs."""

from __future__ import annotations

import imaplib
import json
import re
import time
from datetime import date
from email.message import EmailMessage
from typing import Any

import pytest

from hub_api.routers import expenses as exp_mod
from hub_api.routers import inbox as inbox_mod
from hub_api.routers.inbox import body_text, header_info, parse_email, pick_attachment
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)
PDF = b"%PDF-1.4\n% not really a pdf, the fake vision model reads it anyway\n"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20_000
LOGO = b"\x89PNG" + b"p" * 500


def _mail(mid: str, subject: str, sender: str, *, html: str | None = None, text: str | None = None,
          attachments: list[tuple[bytes, str, str, str]] = ()) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Date"] = "Sat, 17 Oct 2026 10:00:00 +0100"
    if mid:
        msg["Message-ID"] = f"<{mid}>"
    msg.set_content(text or "See the HTML version.")
    if html:
        msg.add_alternative(html, subtype="html")
    for data, maintype, subtype, name in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class FakeIMAP:
    """imaplib.IMAP4-like server object: LIST/SELECT/UID SEARCH (X-GM-RAW literal)/UID FETCH/LOGOUT."""

    def __init__(self, messages: dict[str, bytes], *, password: str = "abcdefghijklmnop", delay: float = 0.0,
                 log: list[Any] | None = None) -> None:
        self.messages = messages
        self.password = password
        self.delay = delay
        self.log = log if log is not None else []
        self.literal: bytes | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.log.append(("login", user, password))
        if password != self.password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
        return "OK", [b"logged in"]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.log.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        if self.delay:
            time.sleep(self.delay)
        if command == "SEARCH":
            self.log.append(("search", args, (self.literal or b"").decode("utf-8")))
            self.literal = None
            return "OK", [" ".join(sorted(self.messages, key=int)).encode()]
        assert command == "FETCH"
        uids, what = args
        self.log.append(("fetch", uids, what))
        out: list[Any] = []
        for i, uid in enumerate(uids.split(","), start=1):
            raw = self.messages[uid]
            payload = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0] + b"\r\n\r\n" if "HEADER.FIELDS" in what else raw
            out.append((f"{i} (UID {uid} BODY[] {{{len(payload)}}}".encode(), payload))
            out.append(b")")
        return "OK", out

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b""]


MAILS = {
    # uid -> message (processed newest first: 14, 13, 12, 11, 10)
    "10": _mail("pdf-1@shop.ma", "Votre facture Jumia", "Jumia <noreply@jumia.ma>",
                attachments=[(LOGO, "image", "png", "logo.png"), (PDF, "application", "pdf", "facture.pdf")]),
    "11": _mail("img-1@cafe.ma", "Ticket de caisse", "Me <me@example.com>", attachments=[(JPEG, "image", "jpeg", "IMG_1.jpg")]),
    "12": _mail("html-1@careem.com", "Your order confirmation", '"Careem" <receipts@careem.com>',
                html="<html><body><h1>Thanks!</h1><p>Total paid: <b>54.00 MAD</b></p><img src='cid:x'></body></html>"),
    "13": _mail("news-1@promo.com", "Payment options newsletter", "Promo <news@promo.com>", text="Big sale! No purchase here."),
    "14": _mail("", "Reçu de paiement", "Inwi <factures@inwi.ma>", text="Montant: 99 DH. Merci."),
}

PDF_RECEIPT = {"merchant": "Jumia", "date": "2026-10-16", "total": 450, "currency": "MAD", "category": "shopping",
               "items": [{"name": "Casque", "qty": 1, "price": 450}], "confidence": "high"}
IMG_RECEIPT = {"merchant": "Cafe Clock", "date": None, "total": "32,50", "currency": "DH", "category": "food", "confidence": "medium"}
HTML_RECEIPT = {"is_receipt": True, "merchant": None, "date": "2026-10-18", "total": 54, "currency": "MAD", "category": "transport"}
NOT_RECEIPT = {"is_receipt": False}
INWI = {"is_receipt": True, "merchant": "Inwi", "date": "2026-10-15", "total": 99, "currency": "DH", "category": "bills"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fa.reset()
    monkeypatch.setattr(exp_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(inbox_mod, "local_today", lambda: TODAY)
    monkeypatch.setenv("GMAIL_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    log: list[Any] = []
    servers: list[FakeIMAP] = []

    def factory(host: str, port: int, timeout: float) -> FakeIMAP:
        log.append(("connect", host, port, timeout))
        server = FakeIMAP(dict(MAILS), log=log)
        servers.append(server)
        return server

    monkeypatch.setattr(inbox_mod, "imap_factory", factory)
    return {"log": log, "servers": servers}


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _script_all() -> None:
    # Vision (gemini): uid 11 (image) is processed before uid 10 (pdf).
    fa.CHAT_SCRIPT["gemini"].extend([json.dumps(IMG_RECEIPT), json.dumps(PDF_RECEIPT)])
    # Text (groq leads the chain): uid 14, 13, 12.
    fa.CHAT_SCRIPT["groq"].extend([json.dumps(INWI), json.dumps(NOT_RECEIPT), "```json\n" + json.dumps(HTML_RECEIPT) + "\n```"])


# ------------------------------------------------------------------ pure helpers


def test_pick_attachment_and_body_text() -> None:
    msg = parse_email(MAILS["10"])
    data, ctype, name = pick_attachment(msg)
    assert (data, ctype, name) == (PDF, "application/pdf", "facture.pdf")  # the PDF wins over the logo
    assert pick_attachment(parse_email(MAILS["11"]))[1] == "image/jpeg"
    small = _mail("x@y", "s", "a@b", attachments=[(LOGO, "image", "png", "logo.png")])
    assert pick_attachment(parse_email(small)) is None  # tiny images are logos
    text = body_text(parse_email(MAILS["12"]))
    assert "Total paid: 54.00 MAD" in text and "<b>" not in text
    assert body_text(parse_email(MAILS["13"])) == "Big sale! No purchase here."
    meta = header_info(MAILS["14"], "14")
    assert meta["message_id"] == "gmail-uid:14" and meta["subject"] == "Reçu de paiement" and meta["date"] == "2026-10-17"


# ------------------------------------------------------------------ sync


def test_not_configured(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    st = app_hub.get("/inbox/receipts/status").json()
    assert st["configured"] is False and st["last_sync_at"] is None and st["last_result"] is None
    assert st["accounts"][0]["name"] == "gmail" and st["accounts"][0]["configured"] is False
    r = app_hub.post("/inbox/receipts/sync", json={})
    assert r.status_code == 409 and "GMAIL_APP_PASSWORD" in r.json()["detail"]
    monkeypatch.delenv("GMAIL_IMAP_USER")
    assert app_hub.get("/inbox/receipts/status").json()["accounts"] == []


def test_sync_creates_expenses_and_dedupes(app_hub, _env) -> None:
    _script_all()
    r = app_hub.post("/inbox/receipts/sync")  # no body at all: what a QStash schedule sends
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["scanned"] == 5 and out["matched"] == 4 and out["errors"] == [] and out["remaining"] == 0
    created = {e["source_ref"].removeprefix("me@gmail.com#"): e for e in out["created"]}
    assert set(created) == {"gmail-uid:14", "html-1@careem.com", "img-1@cafe.ma", "pdf-1@shop.ma"}
    assert out["skipped"] == [{"account": "gmail", "message_id": "news-1@promo.com", "reason": "not a receipt",
                               "subject": "Payment options newsletter"}]
    assert out["accounts"][0] == {"name": "gmail", "user": "me@gmail.com", "scanned": 5, "created": 4,
                                       "skipped": 1, "remaining": 0, "error": None, "complete": True}

    pdf = created["pdf-1@shop.ma"]
    assert (pdf["merchant"], pdf["total"], pdf["currency"], pdf["category"], pdf["source"]) == (
        "Jumia", 450.0, "MAD", "shopping", "email")
    assert pdf["receipt_key"].startswith("receipts/email/2026/10/") and pdf["receipt_key"].endswith(".pdf")
    assert fakes.STORES["r2"][pdf["receipt_key"]] == (PDF, "application/pdf")
    assert pdf["note"] == "Email: Votre facture Jumia (from Jumia <noreply@jumia.ma>, mailbox me@gmail.com)"
    img = created["img-1@cafe.ma"]
    assert img["total"] == 32.5 and img["date"] == "2026-10-17"  # unreadable date -> the email's date
    assert img["receipt_key"].endswith(".jpg")
    html = created["html-1@careem.com"]
    assert html["merchant"] == "Careem" and html["receipt_key"] is None  # body only: nothing stored
    assert created["gmail-uid:14"]["currency"] == "MAD"

    # The vision request carried the PDF itself; text requests the stripped HTML.
    vision = [req for name, req in fa.CHAT_LOG if name == "gemini"]
    assert vision[1].messages[-1].content[1]["image_url"]["url"].startswith("data:application/pdf;base64,")
    text_reqs = [req for name, req in fa.CHAT_LOG if name == "groq"]
    assert "Total paid: 54.00 MAD" in text_reqs[2].messages[-1].content and "Subject: Your order confirmation" in text_reqs[2].messages[-1].content

    log = _env["log"]
    assert ("connect", "imap.gmail.com", 993, inbox_mod.IMAP_TIMEOUT_S) in log
    assert ("login", "me@gmail.com", "abcdefghijklmnop") in log  # spaces of the app password removed
    assert ("select", '"[Gmail]/All Mail"', True) in log
    search = next(e for e in log if e[0] == "search")
    assert search[1] == ("CHARSET", "UTF-8", "X-GM-RAW")
    assert search[2].startswith("newer_than:7d (receipt OR reçu OR facture") and search[2].endswith("-in:spam")
    assert _env["servers"] and all(s.logged_out for s in _env["servers"])

    listed = app_hub.get("/expenses", params={"month": "2026-10"}).json()
    assert listed["summary"]["count"] == 4
    status = app_hub.get("/inbox/receipts/status").json()
    assert status["configured"] is True and status["last_sync_at"]
    assert status["accounts"][0]["last_sync_at"] and status["accounts"][0]["last_error"] is None
    assert status["last_result"]["matched"] == 4 and len(status["last_result"]["created"]) == 4

    # Second run: everything is known, no LLM call, no duplicate expense.
    calls = len(fa.CHAT_LOG)
    again = app_hub.post("/inbox/receipts/sync", json={"days": 3}).json()
    assert again["created"] == [] and again["matched"] == 0 and len(fa.CHAT_LOG) == calls
    reasons = {s["message_id"]: s["reason"] for s in again["skipped"]}
    assert {s["account"] for s in again["skipped"]} == {"gmail"}
    assert reasons == {"gmail-uid:14": "already imported", "html-1@careem.com": "already imported",
                       "img-1@cafe.ma": "already imported", "pdf-1@shop.ma": "already imported",
                       "news-1@promo.com": "already processed"}
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["count"] == 4


def test_several_emails_of_one_purchase_make_one_expense(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    oncf = {uid: _mail(f"oncf-{uid}@oncf.ma", subject, "ONCF <oncf-voyages@oncf.ma>", text="Total 100 MAD")
            for uid, subject in (("1", "Purchase summary"), ("2", "Payment summary"), ("3", "Confirmation of exchange"))}
    monkeypatch.setattr(inbox_mod, "imap_factory", lambda host, port, timeout: FakeIMAP(dict(oncf)))
    ticket = {"is_receipt": True, "merchant": "ONCF", "date": "2026-10-13", "total": 100, "currency": "MAD", "category": "transport"}
    fa.CHAT_SCRIPT["groq"].extend([json.dumps(ticket), json.dumps({**ticket, "merchant": "oncf"}), json.dumps(ticket)])
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert out["matched"] == 1 and len(out["created"]) == 1
    first = out["created"][0]["id"]
    assert [s["reason"] for s in out["skipped"]] == [f"same purchase as expense {first}"] * 2
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["count"] == 1


def test_dry_run_writes_nothing(app_hub) -> None:
    _script_all()
    out = app_hub.post("/inbox/receipts/sync", json={"dry_run": True}).json()
    assert out["dry_run"] is True and out["matched"] == 4
    assert all(e["id"] is None and e["receipt_key"] is None for e in out["created"])
    assert not fakes.STORES.get("r2")
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["count"] == 0
    assert fa.sql("SELECT COUNT(*) FROM app_inbox_seen") == [(0,)]


def test_max_and_time_budget(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    fa.CHAT_SCRIPT["groq"].append(json.dumps(INWI))
    out = app_hub.post("/inbox/receipts/sync", json={"max": 1}).json()
    assert out["matched"] == 1 and out["remaining"] == 4

    monkeypatch.setattr(inbox_mod, "TIME_BUDGET_S", 5.0)  # less than the per-mailbox reserve: not even opened
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert out["created"] == [] and out["scanned"] == 0
    assert out["accounts"][0]["error"] == "not reached (time budget)"

    monkeypatch.setattr(inbox_mod, "ACCOUNT_RESERVE_S", 0.0)  # opened, but no time for any message
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert out["created"] == [] and out["remaining"] == 4 and out["errors"] == []
    assert out["accounts"][0]["complete"] is False


def test_imap_timeout_and_bad_password(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox_mod, "IMAP_TIMEOUT_S", 0.5)
    monkeypatch.setattr(inbox_mod, "imap_factory", lambda h, p, t: FakeIMAP(dict(MAILS), delay=1.5))
    started = time.monotonic()
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert time.monotonic() - started < 4
    assert out["errors"] == [{"account": "gmail", "message_id": None, "error": "the mail server did not answer in time"}]
    assert out["scanned"] == 0

    monkeypatch.setattr(inbox_mod, "imap_factory", lambda h, p, t: FakeIMAP(dict(MAILS), password="other"))
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert "login failed" in out["errors"][0]["error"]
    st = app_hub.get("/inbox/receipts/status").json()
    assert st["last_result"]["errors"] == out["errors"]
    assert "login failed" in st["accounts"][0]["last_error"] and st["accounts"][0]["last_sync_at"] is None


def test_failed_message_is_retried_next_run(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("llm exploded")

    monkeypatch.setattr(inbox_mod, "read_receipt_text", boom)
    out = app_hub.post("/inbox/receipts/sync", json={"max": 1}).json()
    assert out["errors"] == [{"account": "gmail", "message_id": "gmail-uid:14", "error": "llm exploded"}]
    assert fa.sql("SELECT COUNT(*) FROM app_inbox_seen") == [(0,)]  # claim released

    monkeypatch.undo()
    monkeypatch.setattr(inbox_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(exp_mod, "local_today", lambda: TODAY)
    monkeypatch.setenv("GMAIL_IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setattr(inbox_mod, "imap_factory", lambda h, p, t: FakeIMAP(dict(MAILS)))
    fa.CHAT_SCRIPT["groq"].append(json.dumps(INWI))
    out = app_hub.post("/inbox/receipts/sync", json={"max": 1}).json()
    assert [e["source_ref"] for e in out["created"]] == ["me@gmail.com#gmail-uid:14"]


def test_pdf_vision_failure_falls_back_to_body(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.interfaces import ProviderError

    monkeypatch.setattr(inbox_mod, "imap_factory", lambda h, p, t: FakeIMAP({"10": MAILS["10"]}))
    fa.CHAT_SCRIPT["gemini"].append(ProviderError("unsupported mime type", provider="gemini", status_code=400))
    fa.CHAT_SCRIPT["vision_b"].append(ProviderError("no pdf", provider="vision_b", status_code=400))
    fa.CHAT_SCRIPT["groq"].append(json.dumps({**INWI, "merchant": "Jumia", "total": 450}))
    out = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert out["errors"] == [] and out["matched"] == 1
    assert out["created"][0]["merchant"] == "Jumia" and out["created"][0]["receipt_key"] is None


def test_expenses_migration_adds_source_columns(app_hub) -> None:
    # A table created before the migration (no source columns) gets them on first use.
    fa.sql(exp_mod.SCHEMA[0])
    fa.sql("INSERT INTO app_expenses (id, date, merchant, total, created_at) VALUES ('old', '2026-10-01', 'Old', 5, 'x')")
    listed = app_hub.get("/expenses", params={"month": "2026-10"}).json()
    assert listed["expenses"][0]["source"] is None and listed["expenses"][0]["source_ref"] is None
    manual = app_hub.post("/expenses", json={"merchant": "Kiosk", "total": 3}).json()
    assert manual["source"] is None
    cols = [r[1] for r in fa.sql("PRAGMA table_info(app_expenses)")]
    assert cols.count("source") == 1 and "source_ref" in cols


# ------------------------------------------------------------------ several mailboxes

ACCOUNTS = [
    {"name": "perso", "user": "simaxhah@gmail.com", "password_key": "INBOX_PW_SIMAXHAH"},
    {"name": "ilyas", "user": "ilyasouhnine@gmail.com", "host": "imap.gmail.com", "port": 993, "password_key": "INBOX_PW_ILYAS"},
    {"name": "work", "user": "iouhnine@bidtndr.com", "host": "imap.zoho.eu", "port": 993, "password_key": "INBOX_PW_WORK"},
]


@pytest.fixture
def multi(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.delenv("GMAIL_IMAP_USER")
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    monkeypatch.setenv("INBOX_ACCOUNTS", json.dumps(ACCOUNTS))
    monkeypatch.setenv("INBOX_PW_SIMAXHAH", "pw perso")
    monkeypatch.setenv("INBOX_PW_ILYAS", "wrong")
    monkeypatch.setenv("INBOX_PW_WORK", "pwwork")
    log: list[Any] = []
    inwi = MAILS["14"].replace(b"Subject:", b"Message-ID: <same@inwi.ma>\nSubject:", 1)
    boxes = {"imap.gmail.com": {"14": inwi}, "imap.zoho.eu": {"3": inwi}}
    passwords = {"simaxhah@gmail.com": "pwperso", "ilyasouhnine@gmail.com": "right", "iouhnine@bidtndr.com": "pwwork"}

    class PerUser(FakeIMAP):
        def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
            self.password = passwords[user]
            return super().login(user, password)

    def factory(host: str, port: int, timeout: float) -> FakeIMAP:
        log.append(("connect", host, port))
        return PerUser(dict(boxes[host]), log=log)

    monkeypatch.setattr(inbox_mod, "imap_factory", factory)
    return log


def test_multi_account_sync_isolates_failures(app_hub, multi) -> None:
    fa.CHAT_SCRIPT["groq"].extend([json.dumps(INWI), json.dumps(INWI)])
    r = app_hub.post("/inbox/receipts/sync")
    assert r.status_code == 200, r.text
    out = r.json()
    by_name = {a["name"]: a for a in out["accounts"]}
    assert set(by_name) == {"perso", "ilyas", "work"}
    assert "login failed" in by_name["ilyas"]["error"] and by_name["ilyas"]["created"] == 0
    # The same receipt reaching two mailboxes is read in both, but recorded once (same purchase).
    assert by_name["perso"]["created"] + by_name["work"]["created"] == 1
    assert len(out["created"]) == 1 and out["created"][0]["source_ref"].endswith("#same@inwi.ma")
    assert [s["reason"] for s in out["skipped"]] == [f"same purchase as expense {out['created'][0]['id']}"]
    assert out["errors"] == [{"account": "ilyas", "message_id": None, "error": by_name["ilyas"]["error"]}]

    # Gmail hosts search with X-GM-RAW in All Mail; the custom host with standard IMAP SEARCH in INBOX.
    searches = [e for e in multi if e[0] == "search"]
    assert sum(1 for e in searches if e[1] == ("CHARSET", "UTF-8", "X-GM-RAW")) == 1  # ilyas never got there
    std = next(e[1] for e in searches if e[1][0] == "SINCE")
    assert std[:6] == ("SINCE", "13-Oct-2026", "OR", "SUBJECT", "receipt", "OR")
    assert std[-2:] == ("SUBJECT", "recu") and std.count("OR") == len(inbox_mod.SUBJECT_KEYWORDS) - 1
    assert ("select", "INBOX", True) in multi and ("select", '"[Gmail]/All Mail"', True) in multi
    assert ("login", "simaxhah@gmail.com", "pwperso") in multi  # spaces removed

    st = app_hub.get("/inbox/receipts/status").json()
    accts = {a["name"]: a for a in st["accounts"]}
    assert st["configured"] is True and st["problems"] == []
    assert accts["work"]["host"] == "imap.zoho.eu" and accts["work"]["last_error"] is None and accts["work"]["last_sync_at"]
    assert "login failed" in accts["ilyas"]["last_error"] and accts["ilyas"]["last_sync_at"] is None
    assert "pwperso" not in json.dumps(st) and "pwwork" not in json.dumps(st)  # secrets never echoed

    # Re-run: the failed mailbox goes first, imported messages are not read again.
    multi.clear()
    again = app_hub.post("/inbox/receipts/sync", json={}).json()
    assert [a["name"] for a in again["accounts"]][0] == "ilyas"
    assert again["created"] == [] and {s["reason"] for s in again["skipped"]} <= {"already imported", "already processed"}


def test_sync_one_account_and_config_problems(app_hub, multi, monkeypatch: pytest.MonkeyPatch) -> None:
    fa.CHAT_SCRIPT["groq"].append(json.dumps(INWI))
    out = app_hub.post("/inbox/receipts/sync", json={"account": "iouhnine@bidtndr.com"}).json()
    assert [a["name"] for a in out["accounts"]] == ["work"] and len(out["created"]) == 1
    assert [e[1] for e in multi if e[0] == "connect"] == ["imap.zoho.eu"]
    assert app_hub.post("/inbox/receipts/sync", json={"account": "nope"}).status_code == 404

    bad = [*ACCOUNTS, {"name": "leaky", "user": "x@y.com", "password": "oops"}, {"host": "no-user"}]
    monkeypatch.setenv("INBOX_ACCOUNTS", json.dumps(bad))
    problems = app_hub.get("/inbox/receipts/status").json()["problems"]
    assert any("own vault key" in p for p in problems) and any("needs at least a 'user'" in p for p in problems)
    monkeypatch.setenv("INBOX_ACCOUNTS", "{not json")
    r = app_hub.post("/inbox/receipts/sync", json={})
    assert r.status_code == 409 and "not valid JSON" in r.json()["detail"]


def test_split_source_ref() -> None:
    from hub_api.routers.inbox import split_source_ref

    assert split_source_ref("me@gmail.com#abc@x.com") == ("me@gmail.com", "abc@x.com")
    assert split_source_ref("abc@x.com") == (None, "abc@x.com")
    assert split_source_ref(None) == (None, None)


def test_sync_pushes_a_notification_best_effort(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from hub_api import notifier

    pushed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pushed.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": "m1"})

    monkeypatch.setenv("NTFY_TOPIC", "secret-topic")
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    _script_all()
    assert len(app_hub.post("/inbox/receipts/sync").json()["created"]) == 4
    assert len(pushed) == 1 and pushed[0]["title"] == "Reçus importés : 4 nouvelles dépenses"
    assert pushed[0]["topic"] == "secret-topic" and pushed[0]["click"].endswith("#/more/expenses")

    # ntfy down: the import still succeeds; nothing new imported: no push at all.
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda r: httpx.Response(500)))
    r = app_hub.post("/inbox/receipts/sync")
    assert r.status_code == 200 and r.json()["created"] == [] and len(pushed) == 1


def test_email_text_is_kept_for_receipt_search(app_hub) -> None:
    _script_all()
    out = app_hub.post("/inbox/receipts/sync").json()
    created = {e["source_ref"].removeprefix("me@gmail.com#"): e for e in out["created"]}
    html_id, pdf = created["html-1@careem.com"]["id"], created["pdf-1@shop.ma"]
    text, provider = fa.sql("SELECT receipt_text, receipt_provider FROM app_expenses WHERE id = ?", [html_id])[0]
    assert "Total paid: 54.00 MAD" in text and provider is None  # the email body, no attachment stored
    assert fa.sql("SELECT receipt_provider FROM app_expenses WHERE id = ?", [pdf["id"]])[0][0] == "r2"
    found = app_hub.get("/expenses/search", params={"q": "total paid"}).json()
    assert [e["id"] for e in found["results"]] == [html_id]
