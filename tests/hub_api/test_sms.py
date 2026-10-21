"""Bank SMS -> expenses / incomes / balances: parsers per bank, payload formats, token auth, webhook effects."""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from hub_api.bank_sms import bank_for, mask_text, parse_amount, parse_sms
from hub_api.routers import money as money_mod
from hub_api.routers import sms as sms_mod
from hub_api.routers.sms import read_payload, sms_ref
from tests.hub_api import fakes_apps as fa
from tests.hub_api.conftest import AUTH

TODAY = date(2026, 10, 21)
RECEIVED = datetime(2026, 10, 21, 9, 30)
SMS_TOKEN = "sms-secret-token-123"

# (sender, text, kind, bank, amount, currency, merchant, date, card, balance)
SAMPLES = [
    # CIH Bank
    ("CIH BANK", "CIH BANK: Paiement par carte N° 4567****1234 le 20/10/2026 a 14:32 chez MARJANE CALIFORNIE d'un montant "
     "de 245,50 MAD. Solde disponible: 3 456,78 MAD",
     "expense", "CIH Bank", 245.5, "MAD", "MARJANE CALIFORNIE", "2026-10-20", "1234", 3456.78),
    ("CIH BANK", "Retrait GAB de 500,00 DH effectue le 19/10/2026 a 18:05 avec votre carte ***1234. Solde: 2 956,78 DH",
     "withdrawal", "CIH Bank", 500.0, "MAD", "Retrait GAB", "2026-10-19", "1234", 2956.78),
    ("CIH BANK", "Cher client, votre compte 230 810 4567 8900 0012 a ete credite d'un virement recu de SOCIETE ABC SARL "
     "d'un montant de 12 000,00 MAD le 25/09/2026. Solde: 14 956,78 MAD",
     "income", "CIH Bank", 12000.0, "MAD", "SOCIETE ABC SARL", "2026-09-25", None, 14956.78),
    ("CIH BANK", "CIH BANK: تم سحب 400 درهم من الشباك الأوتوماتيكي بواسطة بطاقتكم ***1234 بتاريخ 20/10/2026",
     "withdrawal", "CIH Bank", 400.0, "MAD", "Retrait GAB", "2026-10-20", "1234", None),
    ("CIH BANK", "CIH BANK: عملية شراء بمبلغ 99,00 درهم لدى JUMIA بتاريخ 20/10/2026",
     "expense", "CIH Bank", 99.0, "MAD", "JUMIA", "2026-10-20", None, None),
    # Attijariwafa bank
    ("Attijari", "Attijariwafa bank: Operation par carte 4***1234 de 120.00 MAD chez CARREFOUR MARKET le 18/10/26 14:20. "
     "Solde: 1 234.56 MAD",
     "expense", "Attijariwafa bank", 120.0, "MAD", "CARREFOUR MARKET", "2026-10-18", "1234", 1234.56),
    ("Attijari", "Attijariwafa bank: Votre compte ****5678 a ete credite de 8 500,00 MAD le 30/09 (VIR RECU DE MOHAMED "
     "ALAOUI). Solde: 9 734,56 MAD",
     "income", "Attijariwafa bank", 8500.0, "MAD", "MOHAMED ALAOUI", "2026-09-30", "5678", 9734.56),
    ("Attijari", "Attijariwafa bank: Retrait DAB de 1 000,00 MAD le 15/10/2026 a 10:12, carte ***1234.",
     "withdrawal", "Attijariwafa bank", 1000.0, "MAD", "Retrait GAB", "2026-10-15", "1234", None),
    # Bank of Africa / BMCE
    ("BMCE", "BANK OF AFRICA: Achat de 30.00 USD chez NETFLIX.COM le 12/10/2026 avec votre carte ****9876. "
     "Solde disponible 4 210,00 MAD",
     "expense", "Bank of Africa", 30.0, "USD", "NETFLIX.COM", "2026-10-12", "9876", 4210.0),
    ("BMCE", "BANK OF AFRICA: Virement recu de la part de JOHN DOE LTD 1 500,00 EUR le 10/10/2026",
     "income", "Bank of Africa", 1500.0, "EUR", "JOHN DOE LTD", "2026-10-10", None, None),
    # Banque Populaire / Chaabi
    ("CHAABI", "Chaabi Bank: Achat de 89,00 DH chez GLOVO le 12/10/2026 avec votre carte ***4321.",
     "expense", "Banque Populaire", 89.0, "MAD", "GLOVO", "2026-10-12", "4321", None),
    ("BCP", "Banque Populaire: Prelevement de 349,00 MAD au profit de MAROC TELECOM le 05/10/2026. Solde: 2 100,00 MAD",
     "expense", "Banque Populaire", 349.0, "MAD", "MAROC TELECOM", "2026-10-05", None, 2100.0),
    ("BCP", "Banque Populaire: Vous avez recu un virement de 3 000,00 DH de AHMED BENNANI le 03/10/2026",
     "income", "Banque Populaire", 3000.0, "MAD", "AHMED BENNANI", "2026-10-03", None, None),
    # Crédit Agricole du Maroc
    ("CAM", "Credit Agricole du Maroc: Retrait de 200 DH au GAB AGENCE AGDAL le 14/10/2026.",
     "withdrawal", "Crédit Agricole du Maroc", 200.0, "MAD", "Retrait GAB", "2026-10-14", None, None),
    ("CAM", "Credit Agricole du Maroc: Paiement TPE 450,00 MAD chez PHARMACIE AL AMAL le 16/10/2026.",
     "expense", "Crédit Agricole du Maroc", 450.0, "MAD", "PHARMACIE AL AMAL", "2026-10-16", None, None),
    # Al Barid Bank
    ("ALBARID", "Al Barid Bank: Votre compte a ete debite de 150,00 DH, paiement chez INWI le 11/10/2026. Solde 820,40 DH",
     "expense", "Al Barid Bank", 150.0, "MAD", "INWI", "2026-10-11", None, 820.4),
    ("ALBARID", "Al Barid Bank: Votre compte a ete credite de 2 500,00 DH suite a un versement le 01/10/2026.",
     "income", "Al Barid Bank", 2500.0, "MAD", "Virement reçu", "2026-10-01", None, None),
    # Crédit du Maroc, Saham (ex Société Générale), CFG
    ("CDM", "CDM: Transaction carte ***2222 de 75,00 MAD chez CAREEM le 17/10/2026 a 23:10.",
     "expense", "Crédit du Maroc", 75.0, "MAD", "CAREEM", "2026-10-17", "2222", None),
    ("SGMA", "Saham Bank: Retrait 300,00 MAD carte ****3333 le 13/10/2026 GAB CASA ANFA",
     "withdrawal", "Saham Bank", 300.0, "MAD", "Retrait GAB", "2026-10-13", "3333", None),
    ("CFG", "CFG Bank: Card purchase of EUR 25.99 at SPOTIFY P1234 on 09/10/2026. Available balance: MAD 10,250.00",
     "expense", "CFG Bank", 25.99, "EUR", "SPOTIFY P1234", "2026-10-09", None, 10250.0),
    ("CFG", "CFG Bank: Incoming transfer: you have received MAD 5,000.00 from ACME FREELANCE on 08/10/2026.",
     "income", "CFG Bank", 5000.0, "MAD", "ACME FREELANCE", "2026-10-08", None, None),
    # Wallets
    ("BARIDCASH", "Barid Cash: Vous avez recu un transfert de 1000 DH de FATIMA Z. Code de retrait: 556677",
     "income", "Barid Cash", 1000.0, "MAD", "FATIMA Z", "2026-10-21", None, None),
    ("Orange Money", "Orange Money: Paiement de 50 DH effectue chez JUMIA. Nouveau solde: 120 DH",
     "expense", "Orange Money", 50.0, "MAD", "JUMIA", "2026-10-21", None, 120.0),
]

IGNORED = [
    ("CIH BANK", "Votre code de confirmation est 483920. Ne le communiquez a personne. CIH BANK", "one-time code"),
    ("BMCE", "Votre code OTP pour la transaction chez AMAZON est 123456", "one-time code"),
    ("Attijari", "Profitez de notre offre exceptionnelle sur les credits immobiliers! Attijariwafa bank", "advertising"),
    ("CHAABI", "Chaabi Bank: 845512 est votre mot de passe a usage unique. Ne le partagez pas.", "one-time code"),
]


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(sms_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(503)))
    monkeypatch.setenv("SMS_WEBHOOK_TOKEN", SMS_TOKEN)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(("sender", "text", "kind", "bank", "amount", "currency", "merchant", "day", "card", "balance"), SAMPLES)
def test_bank_samples(sender, text, kind, bank, amount, currency, merchant, day, card, balance) -> None:
    p = parse_sms(sender, text, TODAY, RECEIVED)
    assert (p.kind, p.bank, p.amount, p.currency, p.merchant, p.date, p.card, p.balance) == (
        kind, bank, amount, currency, merchant, day, card, balance)
    if balance is not None:
        assert p.balance_currency == "MAD"


@pytest.mark.parametrize(("sender", "text", "reason"), IGNORED)
def test_otp_and_ads_are_ignored(sender, text, reason) -> None:
    p = parse_sms(sender, text, TODAY, RECEIVED)
    assert p.kind == "ignored" and p.reason == reason


def test_categories_and_helpers() -> None:
    assert parse_sms("CHAABI", SAMPLES[10][1], TODAY).category == "food"
    assert parse_sms("BCP", SAMPLES[11][1], TODAY).category == "bills"
    assert parse_sms("CIH", SAMPLES[0][1], TODAY).category == "groceries"
    assert parse_sms("CIH", SAMPLES[1][1], TODAY).category == "cash"
    assert parse_sms("x", "Virement recu de ACME: salaire octobre 9 000 MAD", TODAY).category == "salary"
    assert parse_amount("1 234,56") == 1234.56 and parse_amount("1.234,56") == 1234.56
    assert parse_amount("1,234.56") == 1234.56 and parse_amount("12000.00") == 12000.0
    assert parse_amount("245,50") == 245.5 and parse_amount("12,000") == 12000.0 and parse_amount("x") is None
    assert bank_for("AD-CIHBNK", "CIH Bank info") == "CIH Bank"
    assert bank_for("+212600000000", "hello") is None
    assert mask_text("Carte 4567 1234 5678 9012 debitee, code: 483920") == "Carte ****9012 debitee, code: ****"
    # The date in the text wins; without one, the day the SMS arrived; never a future date.
    assert parse_sms("CIH", "Paiement de 10 MAD chez X", TODAY, datetime(2026, 10, 19, 23, 0)).date == "2026-10-19"
    assert parse_sms("CIH", "Paiement de 10 MAD chez X le 30/12", TODAY).date == "2025-12-30"


def test_read_payload_formats() -> None:
    # SMS Forwarder (bogkonstantin) default template: millisecond stamps.
    body = json.dumps({"from": "CIH BANK", "text": "Paiement 10 MAD", "sentStamp": 1761030000000,
                       "receivedStamp": 1761030001000, "sim": "sim1"}).encode()
    assert read_payload(body, "application/json", {}) == ("CIH BANK", "Paiement 10 MAD", 1761030001000)
    # Other apps: sender/message/timestamp, nested objects, form data, plain text with ?from=.
    assert read_payload(b'{"sender":"BCP","message":"Achat 5 DH","timestamp":"2026-10-20T10:00:00Z"}', "application/json", {})[:2] == ("BCP", "Achat 5 DH")
    assert read_payload(b'{"data":{"phone":"AWB","body":"x 1 MAD"}}', "application/json", {})[:2] == ("AWB", "x 1 MAD")
    assert read_payload(b"from=CIH&content=Achat+3+DH", "application/x-www-form-urlencoded", {})[:2] == ("CIH", "Achat 3 DH")
    assert read_payload("Achat de 7 DH chez X".encode(), "text/plain", {"from": "CDM", "token": "t"})[:2] == ("CDM", "Achat de 7 DH chez X")
    assert sms_ref("A", "t", 1) == sms_ref("a", "t ", 1) != sms_ref("A", "t", 2)


# ------------------------------------------------------------------ auth


def _post(app_hub, body=None, *, headers=None, params=None, auth=False):
    h = dict(headers or {})
    if auth:
        h.update(AUTH)
    return app_hub.client.post("/money/sms", json=body, headers=h, params=params or {})


def test_token_auth(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"from": "CIH BANK", "text": "Votre code de confirmation est 483920"}
    assert _post(app_hub, body).status_code == 401
    assert _post(app_hub, body, headers={"X-OmniFree-Token": "wrong"}).status_code == 401
    assert _post(app_hub, body, params={"token": "wrong"}).status_code == 401
    assert _post(app_hub, body, params={"token": AUTH["Authorization"].split()[1]}).status_code == 401  # hub token not in query
    assert _post(app_hub, body, headers={"X-OmniFree-Token": SMS_TOKEN}).status_code == 200
    assert _post(app_hub, body, params={"token": SMS_TOKEN}).status_code == 200
    assert _post(app_hub, body, auth=True).status_code == 200
    # The log and setup routes need the hub token itself.
    assert app_hub.client.get("/money/sms/log", headers={"X-OmniFree-Token": SMS_TOKEN}).status_code == 401
    setup = app_hub.get("/money/sms/setup").json()
    assert setup["configured"] is True and setup["token"] == SMS_TOKEN and setup["header"] == "X-OmniFree-Token"
    monkeypatch.delenv("SMS_WEBHOOK_TOKEN")
    assert _post(app_hub, body, params={"token": SMS_TOKEN}).status_code == 401
    assert app_hub.get("/money/sms/setup").json()["configured"] is False


# ------------------------------------------------------------------ webhook effects


def _sms(app_hub, text, sender="CIH BANK", stamp=1792569600000, **params):
    r = _post(app_hub, {"from": sender, "text": text, "receivedStamp": stamp}, params={"token": SMS_TOKEN, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_payment_creates_expense_and_updates_balance(app_hub) -> None:
    out = _sms(app_hub, SAMPLES[0][1])
    assert out["status"] == "created"
    e = out["expense"]
    assert (e["merchant"], e["total"], e["currency"], e["category"], e["date"], e["source"]) == (
        "MARJANE CALIFORNIE", 245.5, "MAD", "groceries", "2026-10-20", "sms")
    assert e["source_ref"].startswith("sms:") and e["note"] == "SMS CIH Bank, card ****1234"
    acc = out["account"]
    assert (acc["name"], acc["kind"], acc["balance"], acc["currency"]) == ("CIH Bank", "bank", 3456.78, "MAD")

    again = _sms(app_hub, SAMPLES[0][1])
    assert again["ignored"] == "already received" and again["expense"] is None
    assert len(app_hub.get("/expenses", params={"month": "2026-10"}).json()["expenses"]) == 1

    # The next SMS updates the same account; its own balance wins.
    _sms(app_hub, SAMPLES[1][1], stamp=1792570100000)
    accounts = {a["name"]: a for a in app_hub.get("/money/accounts").json()["accounts"]}
    assert accounts["CIH Bank"]["balance"] == 2956.78


def test_same_purchase_from_email_is_not_duplicated(app_hub) -> None:
    app_hub.post("/expenses", json={"total": 89, "merchant": "Glovo", "category": "food", "date": "2026-10-12",
                                    "currency": "MAD"})
    out = _sms(app_hub, SAMPLES[10][1], sender="CHAABI")
    assert out["status"] == "ignored" and out["ignored"].startswith("same purchase as expense")
    assert len(app_hub.get("/expenses", params={"month": "2026-10"}).json()["expenses"]) == 1
    log = app_hub.get("/money/sms/log").json()["messages"]
    assert log[0]["status"] == "ignored" and log[0]["reason"].startswith("same purchase")


def test_income_and_withdrawal_to_cash(app_hub) -> None:
    inc = _sms(app_hub, SAMPLES[2][1])["income"]
    assert (inc["source"], inc["amount"], inc["origin"], inc["date"]) == ("SOCIETE ABC SARL", 12000.0, "sms", "2026-09-25")

    first = _sms(app_hub, SAMPLES[7][1], sender="Attijari", stamp=1)
    assert first["status"] == "created" and first["expense"] is None
    assert (first["cash_account"]["name"], first["cash_account"]["kind"], first["cash_account"]["balance"]) == ("Cash", "cash", 1000.0)
    second = _sms(app_hub, SAMPLES[1][1], stamp=2)
    assert second["cash_account"]["balance"] == 1500.0
    # A withdrawal is a move to cash: never counted as spent.
    ov = app_hub.get("/money/overview", params={"month": "2026-10"}).json()
    assert ov["spent"]["total"] == 0 and ov["accounts"]["total"] == pytest.approx(1500.0 + 2956.78)


def test_otp_is_ignored_and_not_stored(app_hub) -> None:
    out = _sms(app_hub, IGNORED[0][1])
    assert out == {**out, "status": "ignored", "ignored": "one-time code"}
    ad = _sms(app_hub, IGNORED[2][1], sender="Attijari")
    assert ad["ignored"] == "advertising"
    log = app_hub.get("/money/sms/log").json()["messages"]
    assert [m["reason"] for m in log] == ["advertising", "one-time code"]
    assert log[1]["text"] is None and "offre" in log[0]["text"]
    assert not fa.CHAT_LOG  # no LLM call for these


def test_dry_run_writes_nothing(app_hub) -> None:
    r = _post(app_hub, {"text": SAMPLES[5][1], "from": "Attijari"}, params={"dry_run": "true"}, auth=True)
    out = r.json()
    assert out["dry_run"] is True and out["status"] == "created"
    assert out["parsed"]["merchant"] == "CARREFOUR MARKET" and out["expense"] is None and out["account"] is None
    assert app_hub.get("/money/sms/log").json()["messages"] == []
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["expenses"] == []
    assert app_hub.get("/money/accounts").json()["accounts"] == []


def test_llm_fallback_only_for_unreadable_bank_sms(app_hub) -> None:
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "is_transaction": True, "type": "expense", "amount": 64.5, "currency": "dh", "merchant": "Bolt",
        "date": "2026-10-20", "balance": 900, "balance_currency": "MAD"}))
    out = _sms(app_hub, "CIH BANK: Mouvement sur votre compte: -64.5 en faveur BOLT.EU le 20/10", stamp=5)
    assert out["status"] == "created" and out["parsed"]["parser"] == "llm"
    assert (out["expense"]["merchant"], out["expense"]["total"], out["expense"]["category"]) == ("Bolt", 64.5, "transport")
    assert out["account"]["balance"] == 900
    assert len(fa.CHAT_LOG) == 1

    fa.CHAT_SCRIPT["groq"].append(json.dumps({"is_transaction": False}))
    out = _sms(app_hub, "CIH BANK: votre carte ***1234 sera renouvelee le 01/11", stamp=6)
    assert out["ignored"] == "not a transaction (LLM)"

    # Not a bank: no LLM call at all.
    out = _sms(app_hub, "Salut, rdv a 18h30 devant le cafe?", sender="+212600000000", stamp=7)
    assert out["ignored"] == "not a bank message" and len(fa.CHAT_LOG) == 2


def test_plain_text_and_empty_bodies(app_hub) -> None:
    r = app_hub.client.post("/money/sms", content=SAMPLES[11][1].encode(), headers={"Content-Type": "text/plain"},
                            params={"token": SMS_TOKEN, "from": "BCP"})
    assert r.status_code == 200 and r.json()["expense"]["merchant"] == "MAROC TELECOM"
    r = app_hub.client.post("/money/sms", content=b"", params={"token": SMS_TOKEN})
    assert r.json() == {"status": "ignored", "ignored": "empty message", "dry_run": False}
