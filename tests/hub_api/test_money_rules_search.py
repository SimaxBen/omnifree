"""Merchant rules (/money/rules, applied at expense creation for every source) and receipt search
(GET /expenses/search with receipt_text and presigned receipt URLs). Fake D1, no network."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from hub_api.merchant_rules import apply_rule, best_rule, normalise_merchant, rule_matches
from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.expenses import clean_receipt_text, parse_receipt, search_terms
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    for mod in (exp_mod, money_mod, notes_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(503)))


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _expense(hub, **body):
    r = hub.post("/expenses", json={"date": "2026-10-10", "total": 10, **body})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ pure helpers


def test_normalise_and_match() -> None:
    assert normalise_merchant("  MARJANE  Casa-Anfa 123 ") == "marjane casa anfa 123"
    assert normalise_merchant("Café Bab") == "cafe bab"
    assert normalise_merchant(None) == ""
    contains = {"pattern": "marjane", "match": "contains", "category": "groceries", "rename": "Marjane", "created_at": "1"}
    equals = {"pattern": "marjane casa", "match": "equals", "category": "food", "rename": None, "created_at": "2"}
    longer = {"pattern": "marjane casa", "match": "contains", "category": "shopping", "rename": None, "created_at": "3"}
    assert rule_matches(contains, "marjane casa anfa") and not rule_matches(contains, "marjanex")
    assert not rule_matches(equals, "marjane casa anfa") and rule_matches(equals, "marjane casa")
    assert best_rule([contains, equals, longer], "MARJANE CASA") is equals  # equals wins
    assert best_rule([contains, longer], "Marjane Casa Anfa") is longer  # then the longest pattern
    assert best_rule([contains], "Carrefour") is None
    assert apply_rule({"merchant": "MARJANE 12", "category": "other"}, contains) == {"merchant": "Marjane", "category": "groceries"}
    assert apply_rule({"merchant": "x", "category": "food"}, contains, keep_category=True)["category"] == "food"
    assert apply_rule({"merchant": "x"}, None) == {"merchant": "x"}


def test_search_terms_and_receipt_text() -> None:
    assert search_terms("  Lait  lait, pain ! a ") == ["lait", "pain"]
    assert search_terms("") == []
    assert clean_receipt_text("  MARJANE \n\n  Lait   2x  7,50 \t\n") == "MARJANE\nLait 2x 7,50"
    assert clean_receipt_text(None) is None and clean_receipt_text("   ") is None
    fields, _ = parse_receipt({"merchant": "M", "total": 5, "text": "TICKET\nLait"}, TODAY)
    assert fields["receipt_text"] == "TICKET\nLait"
    assert "receipt_text" not in parse_receipt({"merchant": "M", "total": 5}, TODAY)[0]


# ------------------------------------------------------------------ rules API


def test_rules_crud_apply_and_insert_hook(app_hub) -> None:
    old1 = _expense(app_hub, merchant="MARJANE CASA 0042", category="other")
    old2 = _expense(app_hub, merchant="Marjane Market", category="food")
    _expense(app_hub, merchant="Carrefour", category="other")

    r = app_hub.post("/money/rules", json={"pattern": "Marjane", "category": "groceries", "rename": " Marjane "})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert (rule["pattern"], rule["match"], rule["category"], rule["rename"], rule["pending"]) == (
        "marjane", "contains", "groceries", "Marjane", 2)

    # Same pattern + match again: the rule is updated, not duplicated.
    r = app_hub.post("/money/rules", json={"pattern": "MARJANE", "category": "groceries", "rename": "Marjane"})
    assert r.status_code == 200 and r.json()["id"] == rule["id"]
    assert len(app_hub.get("/money/rules").json()["rules"]) == 1

    for bad in ({"pattern": "x", "category": "food"}, {"pattern": "abc"}, {"pattern": "abc", "category": "pets"},
                {"pattern": "abc", "category": "food", "match": "regex"}):
        assert app_hub.post("/money/rules", json=bad).status_code == 422, bad

    dry = app_hub.post(f"/money/rules/{rule['id']}/apply", params={"dry_run": 1}).json()
    assert (dry["matched"], dry["updated"], dry["dry_run"]) == (2, 0, True)
    done = app_hub.post(f"/money/rules/{rule['id']}/apply").json()
    assert (done["matched"], done["updated"]) == (2, 2)
    for e in (old1, old2):
        got = app_hub.get(f"/expenses/{e['id']}").json()
        assert (got["merchant"], got["category"]) == ("Marjane", "groceries")
    assert app_hub.get("/money/rules").json()["rules"][0]["pending"] == 0

    # New expenses from any source go through the rule.
    auto = _expense(app_hub, merchant="marjane anfa")  # category not sent -> the rule decides
    assert (auto["merchant"], auto["category"]) == ("Marjane", "groceries")
    picked = _expense(app_hub, merchant="Marjane anfa", category="health")  # picked by hand -> kept
    assert (picked["merchant"], picked["category"]) == ("Marjane", "health")

    fa.TRANSCRIPTS["groq"] = "40 dh marjane"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": [
        {"type": "expense", "amount": 40, "currency": "dh", "merchant": "Marjane Bouskoura", "category": "food"}]}))
    out = app_hub.post("/money/voice", files={"file": ("m.ogg", b"a", "audio/ogg")}).json()
    assert (out["expenses"][0]["merchant"], out["expenses"][0]["category"]) == ("Marjane", "groceries")

    # PATCH / conflicts / DELETE
    other = app_hub.post("/money/rules", json={"pattern": "carrefour", "match": "equals", "category": "groceries"}).json()
    assert other["pending"] == 1
    assert app_hub.patch(f"/money/rules/{other['id']}", json={"pattern": "marjane", "match": "contains"}).status_code == 409
    assert app_hub.patch(f"/money/rules/{other['id']}", json={"category": None}).status_code == 422  # no action left
    p = app_hub.patch(f"/money/rules/{other['id']}", json={"rename": "Carrefour Market"})
    assert p.status_code == 200 and p.json()["rename"] == "Carrefour Market"
    assert app_hub.patch("/money/rules/nope", json={"rename": "x"}).status_code == 404
    assert app_hub.delete(f"/money/rules/{other['id']}").status_code == 204
    assert app_hub.delete(f"/money/rules/{other['id']}").status_code == 404
    assert app_hub.post("/money/rules/nope/apply").status_code == 404


def test_rule_applies_to_bank_sms(app_hub) -> None:
    app_hub.post("/money/rules", json={"pattern": "marjane", "category": "shopping", "rename": "Marjane"})
    text = ("CIH BANK: Paiement par carte N° 4567****1234 le 20/10/2026 a 14:32 chez MARJANE CALIFORNIE d'un montant "
            "de 245,50 MAD. Solde disponible: 3 456,78 MAD")
    r = app_hub.post("/money/sms", json={"from": "CIH BANK", "text": text, "receivedStamp": 1792569600000})
    assert r.status_code == 200, r.text
    expense = r.json()["expense"]
    assert (expense["merchant"], expense["category"], expense["source"]) == ("Marjane", "shopping", "sms")


# ------------------------------------------------------------------ search


RECEIPT = {"merchant": "Marjane", "date": "2026-10-19", "total": 87.5, "currency": "MAD", "category": "groceries",
           "items": [{"name": "Lait Centrale", "qty": 2, "price": 7.5}], "confidence": "high",
           "text": "MARJANE BOUSKOURA\nLait Centrale 2x7,50\nCoca Cola 1,5L 14,00\nTOTAL 87,50"}


def test_receipt_text_is_stored_and_searchable(app_hub) -> None:
    fa.CHAT_SCRIPT["gemini"].append(json.dumps(RECEIPT))
    r = app_hub.post("/expenses/receipt", files={"file": ("r.jpg", b"\xff\xd8jpeg", "image/jpeg")})
    assert r.status_code == 201, r.text
    scanned = r.json()["expense"]
    assert scanned["receipt_provider"] == "r2"
    row = fa.sql("SELECT receipt_text, receipt_provider FROM app_expenses WHERE id = ?", [scanned["id"]])[0]
    assert row[0].startswith("MARJANE BOUSKOURA") and row[1] == "r2"
    _expense(app_hub, merchant="Café Bab", note="coca avec Sara", items=[{"name": "Thé"}])
    _expense(app_hub, merchant="Pharmacie", total=50)

    found = app_hub.get("/expenses/search", params={"q": "coca"}).json()
    assert found["terms"] == ["coca"] and found["count"] == 2
    by_merchant = {e["merchant"]: e for e in found["results"]}
    hit = by_merchant["Marjane"]
    assert hit["receipt_kind"] == "image" and hit["receipt_url"] == f"https://signed.example/r2/{scanned['receipt_key']}?exp=3600"
    assert "Coca Cola" in hit["snippet"]
    assert by_merchant["Café Bab"]["receipt_url"] is None and by_merchant["Café Bab"]["receipt_kind"] is None

    # Every word must match (any field, any order); items are searchable with accents.
    assert [e["merchant"] for e in app_hub.get("/expenses/search", params={"q": "lait bouskoura"}).json()["results"]] == ["Marjane"]
    assert app_hub.get("/expenses/search", params={"q": "lait pharmacie"}).json()["count"] == 0
    assert app_hub.get("/expenses/search", params={"q": "thé"}).json()["results"][0]["snippet"] == "Thé"  # item names
    assert app_hub.get("/expenses/search", params={"q": "100%"}).json()["count"] == 0  # % is not a wildcard
    receipts = app_hub.get("/expenses/search", params={"receipts_only": 1}).json()
    assert [e["merchant"] for e in receipts["results"]] == ["Marjane"]
    assert app_hub.get("/expenses/search", params={"q": " a "}).status_code == 422
    assert app_hub.get("/expenses/search").status_code == 422


def test_search_locates_old_receipts_and_survives_presign_failure(app_hub) -> None:
    e = _expense(app_hub, merchant="Old shop")
    fakes.STORES["plain"]["receipts/2025/01/old.pdf"] = (b"%PDF", "application/pdf")
    fa.sql("UPDATE app_expenses SET receipt_key = ? WHERE id = ?", ["receipts/2025/01/old.pdf", e["id"]])
    out = app_hub.get("/expenses/search", params={"q": "old"}).json()
    (hit,) = out["results"]
    assert hit["receipt_kind"] == "pdf" and hit["receipt_provider"] == "plain"
    assert hit["receipt_url"] is None  # the plain backend cannot presign: the PWA downloads it instead
    stored = fa.sql("SELECT receipt_provider FROM app_expenses WHERE id = ?", [e["id"]])[0][0]
    assert stored == "plain"  # remembered for next time

    fa.PRESIGN_FAIL.add("r2")
    e2 = _expense(app_hub, merchant="Old two")
    fa.sql("UPDATE app_expenses SET receipt_key = 'k.jpg', receipt_provider = 'r2' WHERE id = ?", [e2["id"]])
    hit2 = app_hub.get("/expenses/search", params={"q": "two"}).json()["results"][0]
    assert hit2["receipt_url"] is None and hit2["receipt_provider"] == "r2"
