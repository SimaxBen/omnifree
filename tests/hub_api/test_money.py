"""/money: incomes, recurring expenses, accounts, budgets, FX cache, overview and voice capture (fake D1, no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers.money import (
    FALLBACK_TO_MAD,
    Rates,
    active_in,
    build_overview,
    month_shift,
    occurrence,
    parse_money_entries,
    rates_from_payload,
    resolve_spent_date,
)
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)  # a Tuesday
# 1 MAD = 0.1 USD = 0.08 EUR  ->  1 USD = 10 MAD, 1 EUR = 12.5 MAD
FX_PAYLOAD = {"result": "success", "base_code": "MAD", "rates": {"MAD": 1, "USD": 0.1, "EUR": 0.08}}
FX_CALLS: list[str] = []


def _fx_ok(request: httpx.Request) -> httpx.Response:
    FX_CALLS.append(str(request.url))
    return httpx.Response(200, json=FX_PAYLOAD)


def _fx_down(request: httpx.Request) -> httpx.Response:
    FX_CALLS.append(str(request.url))
    return httpx.Response(503, text="down")


@pytest.fixture(autouse=True)
def _reset_app_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    FX_CALLS.clear()
    monkeypatch.setattr(money_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(notes_mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(_fx_ok))


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


# ------------------------------------------------------------------ pure helpers


def test_month_helpers() -> None:
    assert month_shift("2026-01", -1) == "2025-12"
    assert month_shift("2026-10", 3) == "2027-01"
    assert month_shift("2026-10", -5) == "2026-05"
    assert active_in("2026-03", None, "2026-10") and active_in("2026-03", "2026-10", "2026-10")
    assert not active_in("2026-11", None, "2026-10") and not active_in("2026-01", "2026-09", "2026-10")
    assert occurrence("2026-02", 31) == "2026-02-28" and occurrence("2026-10", 5) == "2026-10-05"


def test_rates_from_payload() -> None:
    rates = rates_from_payload(FX_PAYLOAD)
    assert rates == {"MAD": 1.0, "USD": pytest.approx(10.0), "EUR": pytest.approx(12.5)}
    assert rates_from_payload({"result": "error", "rates": {"USD": 0.1}}) is None
    assert rates_from_payload({"rates": "nope"}) is None
    assert rates_from_payload({"rates": {"USD": 0}}) is None
    r = Rates({"MAD": 1.0, "USD": 10.0}, "live")
    assert r.convert(4.5, "usd") == 45.0 and r.convert(3, "XYZ") is None


def test_resolve_spent_date() -> None:
    assert resolve_spent_date(None, TODAY) == "2026-10-20"
    assert resolve_spent_date("hier", TODAY) == "2026-10-19"
    assert resolve_spent_date("lbare7", TODAY) == "2026-10-19"
    assert resolve_spent_date("avant-hier", TODAY) == "2026-10-18"
    assert resolve_spent_date("lundi", TODAY) == "2026-10-19"  # the last Monday, never the next one
    assert resolve_spent_date("friday", TODAY) == "2026-10-16"
    assert resolve_spent_date("mardi dernier", TODAY) == "2026-10-20"
    assert resolve_spent_date("2026-10-02", TODAY) == "2026-10-02"
    assert resolve_spent_date("2026-12-25", TODAY) == "2026-10-20"  # future -> today
    assert resolve_spent_date("whenever", TODAY) == "2026-10-20"


def test_parse_money_entries() -> None:
    data = {
        "entries": [
            {"type": "expense", "amount": 45, "currency": "dirhams", "merchant": "Taxi", "category": "transport"},
            {"type": "expense", "amount": "30,5", "currency": "dh", "merchant": "Bab Café", "category": "café",
             "date": "hier", "note": " latte "},
            {"type": "income", "amount": 12000, "currency": None, "merchant": "Salaire", "category": "salary"},
            {"type": "expense", "amount": 0, "merchant": "nothing"},
            {"type": "expense", "amount": None},
            "junk",
        ]
    }
    out = parse_money_entries(data, TODAY)
    assert [(e["type"], e["amount"], e["currency"], e["merchant"], e["category"], e["date"]) for e in out] == [
        ("expense", 45.0, "MAD", "Taxi", "transport", "2026-10-20"),
        ("expense", 30.5, "MAD", "Bab Café", "other", "2026-10-19"),
        ("income", 12000.0, "MAD", "Salaire", "salary", "2026-10-20"),
    ]
    assert out[1]["note"] == "latte" and out[0]["note"] is None
    forced = parse_money_entries(data, TODAY, "income")
    assert [e["type"] for e in forced] == ["income"] * 3
    assert forced[0]["category"] == "other"  # "transport" is not an income category
    assert parse_money_entries(None, TODAY) == [] and parse_money_entries({"entries": "x"}, TODAY) == []


def _income(**kw):
    base = {"id": "i", "date": "2026-10-01", "amount": 0.0, "currency": "MAD", "source": "", "category": "other",
            "note": None, "recurring": False, "end_month": None, "origin": None, "audio_key": None,
            "created_at": "2026-10-01T00:00:00Z"}
    return {**base, **kw}


def _rec(**kw):
    base = {"id": "r", "name": "Rent", "amount": 0.0, "currency": "MAD", "category": "housing", "day": 1,
            "start_month": "2026-01", "end_month": None, "note": None, "created_at": "2026-01-01T00:00:00Z"}
    return {**base, **kw}


def test_build_overview_math() -> None:
    rates = Rates({"MAD": 1.0, "USD": 10.0, "EUR": 12.5}, "live", "2026-10-20T00:00:00Z")
    ov = build_overview(
        "2026-10",
        TODAY,
        rates,
        expenses=[
            {"date": "2026-10-03", "total": 100.0, "currency": "MAD", "category": "food"},
            {"date": "2026-10-05", "total": 10.0, "currency": "USD", "category": "shopping"},
            {"date": "2026-09-10", "total": 50.0, "currency": "MAD", "category": "food"},
            {"date": "2026-10-06", "total": 3.0, "currency": "XYZ", "category": "other"},
        ],
        incomes=[
            _income(id="sal", date="2026-08-25", amount=10000.0, source="Salaire", category="salary", recurring=True),
            _income(id="fr", date="2026-10-02", amount=100.0, currency="EUR", source="Client X", category="freelance"),
            _income(id="old", date="2026-06-25", amount=500.0, recurring=True, end_month="2026-07"),
        ],
        recurring=[
            _rec(id="rent", amount=3000.0, day=1),
            _rec(id="claude", name="Claude Max", amount=100.0, currency="USD", category="bills", day=28,
                 start_month="2026-09"),
            _rec(id="gone", name="Gym", amount=200.0, category="health", end_month="2026-08"),
        ],
        accounts=[
            {"id": "a1", "name": "CIH", "kind": "bank", "balance": 5000.0, "currency": "MAD", "note": None,
             "updated_at": "x", "created_at": "x"},
            {"id": "a2", "name": "Wise", "kind": "bank", "balance": 100.0, "currency": "EUR", "note": None,
             "updated_at": "x", "created_at": "x"},
        ],
        budgets=[
            {"category": "food", "amount": 80.0, "currency": "MAD", "updated_at": "x"},
            {"category": "travel", "amount": 100.0, "currency": "EUR", "updated_at": "x"},
        ],
    )
    assert ov["income"]["total"] == 11250.0  # salary + 100 EUR
    assert [i["id"] for i in ov["income"]["items"]] == ["sal", "fr"]
    assert ov["income"]["items"][0]["on"] == "2026-10-25"
    assert ov["spent"] == {"total": 4200.0, "expenses": 200.0, "recurring": 4000.0, "count": 3}
    assert ov["net"] == 7050.0 and ov["savings_rate"] == pytest.approx(0.6267, abs=1e-4)
    cats = {c["category"]: c for c in ov["categories"]}
    assert ov["categories"][0]["category"] == "housing"
    assert cats["food"]["spent"] == 100.0 and cats["food"]["budget"] == 80.0 and cats["food"]["fraction"] == 1.25
    assert cats["bills"]["spent"] == 1000.0 and cats["bills"]["budget"] is None
    assert cats["travel"]["spent"] == 0 and cats["travel"]["budget"] == 1250.0
    assert cats["travel"]["budget_original"] == {"amount": 100.0, "currency": "EUR"}
    series = {s["month"]: s for s in ov["series"]}
    assert list(series) == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert series["2026-06"] == {"month": "2026-06", "income": 500.0, "spent": 3200.0}
    assert series["2026-09"] == {"month": "2026-09", "income": 10000.0, "spent": 4050.0}
    assert [r["id"] for r in ov["recurring"]] == ["rent", "claude"]
    assert [r["upcoming"] for r in ov["recurring"]] == [False, True]
    assert [(u["kind"], u["id"], u["date"]) for u in ov["upcoming"]] == [
        ("income", "sal", "2026-10-25"), ("expense", "claude", "2026-10-28"),
    ]
    assert ov["accounts"]["total"] == 6250.0
    assert ov["fx"] == {"base": "MAD", "source": "live", "fetched_at": "2026-10-20T00:00:00Z", "unconverted": ["XYZ"]}

    empty = build_overview("2026-11", TODAY, rates, expenses=[], incomes=[], recurring=[], accounts=[], budgets=[])
    assert empty["savings_rate"] is None and empty["net"] == 0 and empty["upcoming"] == []


# ------------------------------------------------------------------ FX cache


def test_fx_live_then_cached_then_fallback(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    r = app_hub.get("/money/fx")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source"] == "live" and out["base"] == "MAD"
    assert out["to_mad"]["USD"] == pytest.approx(10.0)
    assert FX_CALLS == [money_mod.FX_URL]

    assert app_hub.get("/money/fx").json()["source"] == "cache"
    assert len(FX_CALLS) == 1  # served from D1 within 24 h

    # Expired cache + API down: the stale rates are better than the static table.
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    fa.sql("UPDATE app_fx_cache SET fetched_at = ?", [old])
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(_fx_down))
    stale = app_hub.get("/money/fx").json()
    assert stale["source"] == "stale" and stale["to_mad"]["EUR"] == pytest.approx(12.5)

    fa.sql("DELETE FROM app_fx_cache")
    fallback = app_hub.get("/money/fx").json()
    assert fallback["source"] == "fallback" and fallback["to_mad"] == FALLBACK_TO_MAD


# ------------------------------------------------------------------ CRUD


def test_incomes_crud_and_validation(app_hub) -> None:
    r = app_hub.post("/money/incomes", json={"amount": 12000, "source": "Salaire", "category": "salary",
                                             "date": "2026-09-25", "recurring": True, "currency": "dh"})
    assert r.status_code == 201, r.text
    sal = r.json()
    assert sal["currency"] == "MAD" and sal["recurring"] is True and sal["origin"] == "manual"
    one = app_hub.post("/money/incomes", json={"amount": 300, "currency": "EUR", "source": "Client X",
                                               "category": "freelance"}).json()
    assert one["date"] == "2026-10-20" and one["recurring"] is False

    assert {i["id"] for i in app_hub.get("/money/incomes", params={"month": "2026-10"}).json()["incomes"]} == {
        sal["id"], one["id"]}
    assert [i["id"] for i in app_hub.get("/money/incomes", params={"month": "2026-09"}).json()["incomes"]] == [sal["id"]]
    assert len(app_hub.get("/money/incomes").json()["incomes"]) == 2

    patched = app_hub.patch(f"/money/incomes/{sal['id']}", json={"end_month": "2026-10", "note": "CDI"}).json()
    assert patched["end_month"] == "2026-10" and patched["note"] == "CDI"
    assert app_hub.get("/money/incomes", params={"month": "2026-11"}).json()["incomes"] == []

    assert app_hub.patch(f"/money/incomes/{sal['id']}", json={"end_month": "2026-08"}).status_code == 422
    assert app_hub.patch(f"/money/incomes/{sal['id']}", json={"amount": None}).status_code == 422
    assert app_hub.post("/money/incomes", json={"amount": -1}).status_code == 422
    assert app_hub.post("/money/incomes", json={"amount": 1, "category": "lottery"}).status_code == 422
    assert app_hub.post("/money/incomes", json={"amount": 1, "end_month": "2026-13"}).status_code == 422
    assert app_hub.post("/money/incomes", json={"amount": 1, "currency": "dollars"}).status_code == 422
    assert app_hub.delete(f"/money/incomes/{one['id']}").status_code == 204
    assert app_hub.delete(f"/money/incomes/{one['id']}").status_code == 404
    assert app_hub.patch("/money/incomes/nope", json={"note": "x"}).status_code == 404


def test_recurring_crud(app_hub) -> None:
    r = app_hub.post("/money/recurring", json={"name": " Claude Max ", "amount": 100, "currency": "USD",
                                               "category": "bills", "day": 28})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["name"] == "Claude Max" and item["start_month"] == "2026-10" and item["end_month"] is None
    rent = app_hub.post("/money/recurring", json={"name": "Rent", "amount": 3000, "category": "housing",
                                                  "start_month": "2026-01"}).json()
    assert [x["id"] for x in app_hub.get("/money/recurring").json()["recurring"]] == [rent["id"], item["id"]]
    up = app_hub.patch(f"/money/recurring/{item['id']}", json={"amount": 110, "end_month": "2027-06"}).json()
    assert up["amount"] == 110 and up["end_month"] == "2027-06"
    assert app_hub.patch(f"/money/recurring/{item['id']}", json={"start_month": "2027-07"}).status_code == 422
    assert app_hub.post("/money/recurring", json={"name": "x", "amount": 1, "day": 32}).status_code == 422
    assert app_hub.post("/money/recurring", json={"name": "", "amount": 1}).status_code == 422
    assert app_hub.delete(f"/money/recurring/{item['id']}").status_code == 204
    assert app_hub.delete(f"/money/recurring/{item['id']}").status_code == 404


def test_accounts_and_budgets(app_hub) -> None:
    cih = app_hub.post("/money/accounts", json={"name": "CIH", "balance": 5400.5}).json()
    cash = app_hub.post("/money/accounts", json={"name": "Cash", "kind": "cash", "balance": 20, "currency": "EUR"}).json()
    assert cih["kind"] == "bank" and cih["currency"] == "MAD"
    listing = app_hub.get("/money/accounts").json()
    assert [a["name"] for a in listing["accounts"]] == ["CIH", "Cash"]
    assert listing["total"] == 5650.5 and listing["currency"] == "MAD"

    up = app_hub.patch(f"/money/accounts/{cih['id']}", json={"balance": 6000}).json()
    assert up["balance"] == 6000 and up["updated_at"] >= cih["updated_at"]
    assert app_hub.patch(f"/money/accounts/{cih['id']}", json={"kind": "piggy"}).status_code == 422
    assert app_hub.delete(f"/money/accounts/{cash['id']}").status_code == 204
    assert app_hub.get("/money/accounts").json()["total"] == 6000

    b = app_hub.put("/money/budgets/food", json={"amount": 1500})
    assert b.status_code == 200, b.text
    assert b.json()["currency"] == "MAD"
    assert app_hub.put("/money/budgets/food", json={"amount": 1800, "currency": "MAD"}).json()["amount"] == 1800
    assert app_hub.get("/money/budgets").json()["budgets"][0]["amount"] == 1800
    assert app_hub.put("/money/budgets/casino", json={"amount": 1}).status_code == 422
    assert app_hub.put("/money/budgets/food", json={"amount": 0}).status_code == 422
    assert app_hub.delete("/money/budgets/food").status_code == 204
    assert app_hub.delete("/money/budgets/food").status_code == 404


def test_overview_endpoint(app_hub) -> None:
    app_hub.post("/expenses", json={"total": 200, "merchant": "Marjane", "category": "groceries", "date": "2026-10-03"})
    app_hub.post("/expenses", json={"total": 10, "currency": "USD", "category": "shopping", "date": "2026-10-04"})
    app_hub.post("/money/incomes", json={"amount": 12000, "source": "Salaire", "category": "salary",
                                         "date": "2026-09-25", "recurring": True})
    app_hub.post("/money/recurring", json={"name": "Rent", "amount": 3000, "category": "housing",
                                           "start_month": "2026-01"})
    app_hub.post("/money/accounts", json={"name": "CIH", "balance": 1000, "currency": "USD"})
    app_hub.put("/money/budgets/groceries", json={"amount": 400})

    r = app_hub.get("/money/overview", params={"month": "2026-10"})
    assert r.status_code == 200, r.text
    ov = r.json()
    assert ov["income"]["total"] == 12000 and ov["spent"]["total"] == 3300 and ov["net"] == 8700
    assert ov["savings_rate"] == 0.725
    groceries = next(c for c in ov["categories"] if c["category"] == "groceries")
    assert groceries["fraction"] == 0.5
    assert ov["accounts"]["total"] == 10000 and ov["fx"]["source"] == "live"
    assert [s["month"] for s in ov["series"]][-1] == "2026-10" and ov["series"][-2]["income"] == 12000
    assert [u["name"] for u in ov["upcoming"]] == ["Salaire"]
    assert app_hub.get("/money/overview").json()["month"] == "2026-10"
    assert app_hub.get("/money/overview", params={"month": "Oct"}).status_code == 422


# ------------------------------------------------------------------ voice


MONEY_JSON = {
    "entries": [
        {"type": "expense", "amount": 45, "currency": "dirhams", "merchant": "Taxi", "category": "transport",
         "date": None, "note": None},
        {"type": "expense", "amount": 30, "currency": "dh", "merchant": "Bab Café", "category": "food",
         "date": "hier", "note": "café"},
        {"type": "income", "amount": 12000, "currency": "MAD", "merchant": "Salaire", "category": "salary"},
    ]
}


def _voice(app_hub, kind: str = "auto", content: bytes = b"OggS-audio"):
    return app_hub.post("/money/voice", files={"file": ("m.ogg", content, "audio/ogg")}, data={"kind": kind})


def test_voice_money_creates_expenses_and_incomes(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "j'ai payé 45 dirhams de taxi et hier 30 dh un café chez Bab Café, et j'ai reçu mon salaire"
    fa.CHAT_SCRIPT["groq"].append("```json\n" + json.dumps(MONEY_JSON) + "\n```")
    r = _voice(app_hub)
    assert r.status_code == 201, r.text
    out = r.json()
    key = out["audio_key"]
    assert key.startswith("voice/2026/10/") and fakes.STORES["r2"][key] == (b"OggS-audio", "audio/ogg")
    assert out["transcript"].startswith("j'ai payé")
    assert [(e["merchant"], e["total"], e["currency"], e["category"], e["date"], e["source"]) for e in out["expenses"]] == [
        ("Taxi", 45.0, "MAD", "transport", "2026-10-20", "voice"),
        ("Bab Café", 30.0, "MAD", "food", "2026-10-19", "voice"),
    ]
    assert out["expenses"][0]["source_ref"] == key
    (inc,) = out["incomes"]
    assert (inc["source"], inc["amount"], inc["category"], inc["origin"], inc["audio_key"]) == (
        "Salaire", 12000.0, "salary", "voice", key)
    listed = app_hub.get("/expenses", params={"month": "2026-10"}).json()
    assert len(listed["expenses"]) == 2
    _name, req = fa.CHAT_LOG[0]
    assert "2026-10-20" in req.messages[0].content and req.response_format == {"type": "json_object"}


def test_voice_money_kind_and_edge_cases(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "freelance 300 euros"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": [{"type": "expense", "amount": 300, "currency": "euros",
                                                           "merchant": "Client X", "category": "freelance"}]}))
    out = _voice(app_hub, kind="income").json()
    assert out["expenses"] == [] and out["incomes"][0]["currency"] == "EUR"
    assert out["incomes"][0]["category"] == "freelance"
    assert "every amount is an income" in fa.CHAT_LOG[-1][1].messages[0].content

    fa.CHAT_SCRIPT["groq"].append("sorry, no idea")
    out = _voice(app_hub).json()
    assert out["expenses"] == [] and out["incomes"] == [] and out["transcript"] == "freelance 300 euros"

    fa.TRANSCRIPTS["groq"] = "   "
    assert _voice(app_hub).status_code == 422
    assert _voice(app_hub, kind="lottery").status_code == 422


def test_notes_voice_auto_routes_money(app_hub) -> None:
    fa.TRANSCRIPTS["groq"] = "j'ai payé 45 dh de taxi"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "title": "Taxi", "body": "Taxi 45 dh", "tags": [], "tasks": [],
        "money": [{"type": "expense", "amount": 45, "currency": "dh", "merchant": "Taxi", "category": "transport"}],
        "money_only": True,
    }))
    r = app_hub.post("/notes/voice", files={"file": ("m.ogg", b"a", "audio/ogg")}, data={"mode": "auto"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["note"] is None and out["tasks"] == [] and out["incomes"] == []
    assert out["expenses"][0]["merchant"] == "Taxi" and out["expenses"][0]["source"] == "voice"

    # Mixed memo: the note stays, the money is recorded too.
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "title": "Day", "body": "Met Sara. Paid 30 dh coffee.", "tags": [], "tasks": [],
        "money": [{"type": "expense", "amount": 30, "currency": "dh", "merchant": "Café", "category": "food"}],
        "money_only": False,
    }))
    out = app_hub.post("/notes/voice", files={"file": ("m.ogg", b"a", "audio/ogg")}, data={"mode": "auto"}).json()
    assert out["note"]["title"] == "Day" and len(out["expenses"]) == 1

    # Explicit note mode ignores money.
    fa.CHAT_SCRIPT["groq"].append(json.dumps({
        "title": "T", "body": "b", "tasks": [], "money": [{"type": "expense", "amount": 5}], "money_only": True,
    }))
    out = app_hub.post("/notes/voice", files={"file": ("m.ogg", b"a", "audio/ogg")}, data={"mode": "note"}).json()
    assert out["note"] is not None and out["expenses"] == []
