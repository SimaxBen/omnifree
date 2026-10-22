"""/expenses and the receipt vision pipeline (fake D1, scripted vision LLMs, fake storage)."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from core.interfaces import QuotaExhausted
from hub_api.routers import expenses as exp_mod
from hub_api.routers.expenses import normalise_currency, parse_receipt, summarise
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

RECEIPT = {
    "merchant": "Marjane Californie",
    "date": "2026-10-18",
    "total": "245,50",
    "currency": "DH",
    "category": "groceries",
    "items": [{"name": "Lait", "qty": 2, "price": 7.5}, {"name": "Pain", "qty": None, "price": "3.00"}, {"qty": 1}],
    "confidence": "high",
}


@pytest.fixture(autouse=True)
def _reset_app_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    monkeypatch.setattr(exp_mod, "local_today", lambda: TODAY)


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _scan(app_hub, content: bytes = JPEG, ctype: str = "image/jpeg", name: str = "r.jpg", **form):
    return app_hub.post("/expenses/receipt", files={"file": (name, content, ctype)}, data=form)


# ------------------------------------------------------------------ pure helpers


def test_parse_receipt_validation() -> None:
    fields, conf = parse_receipt(RECEIPT, TODAY)
    assert fields == {
        "date": "2026-10-18",
        "merchant": "Marjane Californie",
        "total": 245.5,
        "currency": "MAD",
        "category": "groceries",
        "items": [{"name": "Lait", "qty": 2.0, "price": 7.5}, {"name": "Pain", "qty": None, "price": 3.0}],
    }
    assert conf == "high"

    fields, conf = parse_receipt({"merchant": "Cafe", "total": 30, "currency": "eur", "category": "Restaurant",
                                  "date": "2027-01-01", "confidence": "high"}, TODAY)
    assert fields["currency"] == "EUR" and fields["category"] == "other"
    assert fields["date"] == "2026-10-20"  # future date -> today
    assert conf == "medium"

    fields, conf = parse_receipt(None, TODAY)
    assert fields["total"] == 0 and fields["currency"] == "MAD" and fields["merchant"] == "Unknown"
    assert fields["date"] == "2026-10-20" and conf == "low"

    _f, conf = parse_receipt({"merchant": "X", "total": None, "currency": "MAD", "date": "2026-10-01"}, TODAY)
    assert conf == "low"


def test_currency_and_summary() -> None:
    assert normalise_currency("dhs") == "MAD"
    assert normalise_currency("€") == "EUR"
    assert normalise_currency("usd") == "USD"
    assert normalise_currency("dollars") is None
    assert normalise_currency(5) is None
    s = summarise([
        {"currency": "MAD", "category": "food", "total": 10.1},
        {"currency": "MAD", "category": "food", "total": 20.2},
        {"currency": "EUR", "category": "travel", "total": 5},
    ])
    assert s == {"total_by_currency": {"MAD": 30.3, "EUR": 5.0},
                 "by_category": {"food": {"MAD": 30.3}, "travel": {"EUR": 5.0}}, "count": 3}


# ------------------------------------------------------------------ receipt pipeline


def test_receipt_pipeline_routes_to_vision_provider(app_hub) -> None:
    fa.CHAT_SCRIPT["gemini"].append("```json\n" + json.dumps(RECEIPT) + "\n```")
    r = _scan(app_hub, note=" weekly shop ")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["confidence"] == "high"
    e = out["expense"]
    assert (e["merchant"], e["total"], e["currency"], e["category"], e["date"]) == (
        "Marjane Californie", 245.5, "MAD", "groceries", "2026-10-18")
    assert e["note"] == "weekly shop"
    assert e["receipt_key"].startswith("receipts/2026/10/") and e["receipt_key"].endswith(".jpg")
    assert fakes.STORES["r2"][e["receipt_key"]] == (JPEG, "image/jpeg")

    # Only vision-capable providers are tried, Gemini first, even though groq leads the chain.
    assert [name for name, _ in fa.CHAT_LOG] == ["gemini"]
    _name, req = fa.CHAT_LOG[0]
    parts = req.messages[-1].content
    assert parts[0]["type"] == "text"
    assert parts[1] == {"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}}
    assert "2026-10-20" in req.messages[0].content

    listed = app_hub.get("/expenses", params={"month": "2026-10"}).json()
    assert [x["id"] for x in listed["expenses"]] == [e["id"]]


def test_receipt_vision_failover_and_unreadable(app_hub) -> None:
    fa.CHAT_SCRIPT["gemini"].append(QuotaExhausted("429", provider="gemini", status_code=429))
    fa.CHAT_SCRIPT["vision_b"].append("I see a blurry photo, no idea.")
    r = _scan(app_hub, content=b"\x89PNG....", ctype="image/png", name="r.png")
    assert r.status_code == 201, r.text
    out = r.json()
    assert [n for n, _ in fa.CHAT_LOG] == ["gemini", "vision_b"]
    assert out["confidence"] == "low"
    assert out["expense"]["total"] == 0 and out["expense"]["currency"] == "MAD"
    assert out["expense"]["date"] == "2026-10-20" and out["expense"]["receipt_key"].endswith(".png")


def test_receipt_no_vision_provider(make_hub) -> None:
    plugins = fa.apps_plugins()
    plugins.pop("gemini")
    plugins.pop("vision_b")
    hub = make_hub(plugins)
    r = _scan(hub)
    assert r.status_code == 503
    assert fa.CHAT_LOG == []


def test_receipt_rejects_non_images_and_large_files(app_hub) -> None:
    assert _scan(app_hub, content=b"hello", ctype="text/plain", name="a.txt").status_code == 415
    assert _scan(app_hub, content=b"x" * 50_001).status_code == 413
    assert _scan(app_hub, content=b"").status_code == 422
    # Content type guessed from the filename when the browser sends octet-stream (HEIC from iPhones).
    fa.CHAT_SCRIPT["gemini"].append(json.dumps(RECEIPT))
    r = _scan(app_hub, ctype="application/octet-stream", name="IMG_1.HEIC")
    assert r.status_code == 201 and r.json()["expense"]["receipt_key"].endswith(".heic")
    # PDF receipts (Android share sheet): stored as .pdf and sent to the vision model as a PDF data URL.
    fa.CHAT_SCRIPT["gemini"].append(json.dumps(RECEIPT))
    r = _scan(app_hub, content=b"%PDF-1.4 receipt", ctype="application/pdf", name="facture.pdf")
    assert r.status_code == 201 and r.json()["expense"]["receipt_key"].endswith(".pdf")


# ------------------------------------------------------------------ manual CRUD + month summary


def test_expenses_crud_and_month_summary(app_hub) -> None:
    a = app_hub.post("/expenses", json={"date": "2026-10-02", "merchant": "Cafe", "total": 25, "category": "food"})
    assert a.status_code == 201, a.text
    a = a.json()
    assert a["currency"] == "MAD" and a["items"] == [] and a["note"] is None and a["receipt_key"] is None
    b = app_hub.post("/expenses", json={"date": "2026-10-15", "merchant": "Tram", "total": 6.5, "category": "transport",
                                        "items": [{"name": "ticket", "qty": 1, "price": 6.5}]}).json()
    c = app_hub.post("/expenses", json={"date": "2026-10-31", "merchant": "Hotel", "total": 120, "currency": "eur",
                                        "category": "travel"}).json()
    app_hub.post("/expenses", json={"date": "2026-09-30", "merchant": "Old", "total": 99, "category": "food"})
    today = app_hub.post("/expenses", json={"merchant": "Kiosk", "total": 3}).json()
    assert today["date"] == "2026-10-20" and c["currency"] == "EUR"

    r = app_hub.get("/expenses")  # default month = current (Africa/Casablanca)
    body = r.json()
    assert [e["id"] for e in body["expenses"]] == [c["id"], today["id"], b["id"], a["id"]]
    assert body["summary"] == {
        "total_by_currency": {"EUR": 120.0, "MAD": 34.5},
        "by_category": {"travel": {"EUR": 120.0}, "other": {"MAD": 3.0}, "transport": {"MAD": 6.5},
                        "food": {"MAD": 25.0}},
        "count": 4,
    }
    sep = app_hub.get("/expenses", params={"month": "2026-09"}).json()
    assert sep["summary"]["count"] == 1 and sep["summary"]["total_by_currency"] == {"MAD": 99.0}
    assert app_hub.get("/expenses", params={"month": "2026-13"}).status_code == 422

    for bad in ({"total": -1}, {"total": 1, "currency": "dollars"}, {"total": 1, "category": "pets"},
                {"total": 1, "date": "20/10/2026"}, {"merchant": "no total"}):
        assert app_hub.post("/expenses", json=bad).status_code == 422, bad

    r = app_hub.patch(f"/expenses/{a['id']}", json={"category": "groceries", "note": "fixed", "items": [{"name": "x"}]})
    assert r.status_code == 200
    p = r.json()
    assert (p["category"], p["note"], p["items"]) == ("groceries", "fixed", [{"name": "x", "qty": None, "price": None}])
    assert app_hub.patch(f"/expenses/{a['id']}", json={"total": None}).status_code == 422
    assert app_hub.patch(f"/expenses/{a['id']}", json={"currency": "xx1"}).status_code == 422
    assert app_hub.patch("/expenses/nope", json={"note": "x"}).status_code == 404
    assert app_hub.get(f"/expenses/{a['id']}").json()["category"] == "groceries"

    assert app_hub.delete(f"/expenses/{a['id']}").status_code == 204
    assert app_hub.delete(f"/expenses/{a['id']}").status_code == 404
