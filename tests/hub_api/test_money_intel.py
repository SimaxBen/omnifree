"""Money intelligence: merchant keys, detected subscriptions, bills pushes, live budget alerts, the end-of-month
forecast and savings goals (fake D1, mocked ntfy, no network)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from hub_api import money_intel, notifier
from hub_api.money_intel import budget_level, detect_subscriptions, months_until, next_after, occurrences_between
from hub_api.money_match import amount_close, merchant_key
from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import money_plus as plus_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers.money import Rates, build_overview
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 22)  # a Thursday
TOPIC = "omnifree-test-topic-money"
FX_PAYLOAD = {"result": "success", "base_code": "MAD", "rates": {"MAD": 1, "USD": 0.1, "EUR": 0.08}}
RATES = {"MAD": 1.0, "USD": 10.0, "EUR": 12.5}


def to_mad(amount: float, currency: str) -> float | None:
    rate = RATES.get(currency)
    return None if rate is None else amount * rate


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    for mod in (money_mod, money_intel, plus_mod, exp_mod, notes_mod, notify_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(200, json=FX_PAYLOAD)))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """ntfy configured and mocked: every push lands here."""
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"msg{len(log)}"})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _exp(day: str, merchant: str, total: float, currency: str = "MAD", category: str = "bills", source: str = "sms",
         **kw: Any) -> dict[str, Any]:
    return {"id": f"{merchant}-{day}", "date": day, "merchant": merchant, "total": total, "currency": currency,
            "category": category, "source": source, **kw}


# ------------------------------------------------------------------ pure helpers


def test_merchant_key_and_amounts() -> None:
    assert merchant_key("PRLV SEPA NETFLIX.COM 866-579") == merchant_key("Netflix") == "netflix"
    assert merchant_key("ANTHROPIC* CLAUDE.AI SUBSCRIPTION") == merchant_key("Claude Max") == "claude"
    assert merchant_key("GOOGLE *Google One") == merchant_key("googleone storage") == "google one"
    assert merchant_key("IAM recharge") == "maroc telecom" and merchant_key("William Shop") == "william shop"
    assert merchant_key("Bab Café") == "bab cafe"
    assert merchant_key("") == merchant_key("Unknown") == merchant_key("CB 1234") == ""
    assert amount_close(110, 100) and not amount_close(120, 100) and amount_close(0, 0) and not amount_close(1, 0)


def test_dates_and_levels() -> None:
    assert next_after(date(2026, 1, 31), "monthly", date(2026, 2, 1), 31) == date(2026, 2, 28)
    assert next_after(date(2026, 10, 15), "weekly", TODAY) == date(2026, 10, 22)
    assert next_after(date(2024, 2, 29), "yearly", date(2025, 1, 1)) == date(2025, 2, 28)
    assert occurrences_between(date(2026, 10, 2), "weekly", date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, d) for d in (2, 9, 16, 23, 30)
    ]
    assert occurrences_between(date(2026, 11, 5), "yearly", date(2026, 10, 1), date(2026, 10, 31)) == []
    assert [budget_level(f) for f in (0.5, 0.8, 0.99, 1.0, 1.7)] == [None, 80, 80, 100, 100]
    assert months_until(date(2027, 3, 31), TODAY) == 5
    assert months_until(date(2026, 11, 10), TODAY) == 1
    assert months_until(date(2026, 10, 1), TODAY) == 0


def test_detect_subscriptions() -> None:
    expenses = [
        # monthly, from every source, one outlier amount that is not part of the subscription
        _exp("2026-07-14", "PRLV NETFLIX.COM", 65, source="sms"),
        _exp("2026-08-14", "Netflix", 65, source="email"),
        _exp("2026-09-15", "NETFLIX.COM", 65, source="receipt"),
        _exp("2026-10-14", "Netflix", 66, source="manual"),
        _exp("2026-10-01", "Netflix", 199, source="manual"),
        # yearly, in USD
        _exp("2024-11-05", "ANTHROPIC* CLAUDE.AI", 200, "USD"),
        _exp("2025-11-05", "Anthropic", 210, "USD"),
        # weekly
        *[_exp(f"2026-{d}", "Tramway Pass", 60, category="transport") for d in ("09-24", "10-01", "10-08", "10-15")],
        # irregular groceries, a stale subscription, a single charge, a future-dated one
        _exp("2026-10-02", "Marjane", 300, category="groceries"), _exp("2026-10-09", "Marjane", 120, category="groceries"),
        _exp("2026-10-19", "Marjane", 560, category="groceries"),
        _exp("2026-03-01", "Spotify", 60), _exp("2026-04-01", "Spotify", 60), _exp("2026-05-01", "Spotify", 60),
        _exp("2026-10-03", "Inwi", 99), _exp("2026-12-01", "Inwi", 99),
    ]
    found = {s["key"]: s for s in detect_subscriptions(expenses, TODAY, to_mad)}
    assert set(found) == {"netflix", "claude", "tramway pass"}
    nf = found["netflix"]
    assert nf["cadence"] == "monthly" and nf["occurrences"] == 4 and nf["name"] == "Netflix"
    assert nf["avg_amount"] == 65.25 and nf["currency"] == "MAD" and nf["day"] == 14
    assert nf["next_date"] == "2026-11-14" and nf["level"] == "high" and nf["confidence"] >= 0.9
    assert nf["sources"] == ["email", "manual", "receipt", "sms"] and "Netflix-2026-10-01" not in nf["expense_ids"]
    cl = found["claude"]
    assert cl["cadence"] == "yearly" and cl["currency"] == "USD" and cl["avg_amount"] == 205
    assert cl["avg_amount_mad"] == 2050 and cl["monthly_mad"] == pytest.approx(170.83, abs=0.01)
    assert cl["next_date"] == "2026-11-05"
    tram = found["tramway pass"]
    assert tram["cadence"] == "weekly" and tram["next_date"] == "2026-10-22" and tram["category"] == "transport"
    assert list(found) == sorted(found, key=lambda k: (-found[k]["confidence"], -found[k]["avg_amount_mad"], k))
    assert detect_subscriptions(expenses, TODAY, to_mad, skip={"netflix", "claude", "tramway pass"}) == []


def _rec(**kw: Any) -> dict[str, Any]:
    base = {"id": "r", "name": "Rent", "amount": 0.0, "currency": "MAD", "category": "housing", "day": 1,
            "start_month": "2026-01", "end_month": None, "note": None, "created_at": "2026-01-01T00:00:00Z"}
    return {**base, **kw}


def _ov(today: date, month: str = "2026-10", extra: list[dict[str, Any]] | None = None,
        expenses: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
    rates = Rates(dict(RATES), "live", "2026-10-20T00:00:00Z")
    expenses = expenses if expenses is not None else [
        _exp("2026-10-05", "Café", 220, category="food"),
        _exp("2026-10-10", "Marjane", 440, category="groceries"),
        _exp("2026-10-14", "PRLV NETFLIX", 65),
        _exp("2026-09-12", "Marjane", 900, category="groceries"),
    ]
    income = {"id": "sal", "date": "2026-01-25", "amount": 10000.0, "currency": "MAD", "source": "Salaire",
              "category": "salary", "note": None, "recurring": True, "end_month": None, "origin": None,
              "audio_key": None, "created_at": "x"}
    return build_overview(
        month, today, rates, expenses=expenses, incomes=[income],
        recurring=[
            _rec(id="rent", amount=3000.0, day=1),
            _rec(id="nf", name="Netflix", amount=65.0, category="bills", day=14),
            _rec(id="net", name="Internet", amount=300.0, category="bills", day=28),
        ],
        accounts=[], budgets=[{"category": "bills", "amount": 1000.0, "currency": "MAD", "updated_at": "x"}],
        extra_recurring=extra, **kw,
    )


def test_overview_counts_a_paid_charge_once_and_forecasts() -> None:
    extra = [{"id": "s1", "name": "Tram card", "amount": 50.0, "currency": "MAD", "category": "transport", "on": "2026-10-29"},
             {"id": "s1", "name": "Tram card", "amount": 50.0, "currency": "MAD", "category": "transport", "on": "2026-10-15"}]
    ov = _ov(TODAY, extra=extra)
    assert ov["spent"] == {"total": 4025.0, "expenses": 725.0, "recurring": 3300.0, "count": 3}
    rec = {r["id"]: r for r in ov["recurring"]}
    assert rec["nf"]["paid"] is True and rec["nf"]["upcoming"] is False
    assert rec["net"]["paid"] is False and rec["net"]["upcoming"] is True
    bills = next(c for c in ov["categories"] if c["category"] == "bills")
    assert bills["spent"] == 365.0  # Netflix through its expense, Internet through the charge
    assert [s["spent"] for s in ov["series"] if s["month"] == "2026-09"] == [4265.0]  # Netflix not paid in Sep: charge
    assert [(u["name"], u["date"]) for u in ov["upcoming"]] == [
        ("Salaire", "2026-10-25"), ("Internet", "2026-10-28"), ("Tram card", "2026-10-29"),
    ]
    fc = ov["forecast"]
    assert fc["daily_avg"] == 30.0 and fc["remaining_days"] == 9 and fc["month_end"] == "2026-10-31"
    assert fc["spent_so_far"] == 3725.0 and fc["remaining_recurring"] == 350.0
    assert fc["projected_spent"] == 4345.0 and fc["projected_left"] == 5655.0
    assert fc["confidence"] == "high" and fc["history_months"] == 1 and fc["history_daily_avg"] == 30.0

    early = _ov(date(2026, 10, 3), expenses=[_exp("2026-10-02", "Café", 100, category="food"),
                                             _exp("2026-09-12", "Marjane", 900, category="groceries")])
    fc = early["forecast"]
    assert fc["daily_avg"] == 31.0  # (100 spent + 7 days x 30 from September) / 10
    assert fc["confidence"] == "medium" and fc["remaining_days"] == 28
    assert fc["remaining_recurring"] == 365.0  # Netflix (not paid yet) and Internet; rent on the 1st is past

    past = _ov(TODAY, month="2026-09")["forecast"]
    assert past["remaining_days"] == 0 and past["remaining_recurring"] == 0 and past["confidence"] == "high"
    assert past["projected_spent"] == past["spent_so_far"] == 4265.0

    future = _ov(TODAY, month="2026-11")["forecast"]
    assert future["remaining_days"] == 30 and future["confidence"] == "low"
    assert future["projected_spent"] == round(3365 + future["daily_avg"] * 30, 2)


def test_forecast_counts_weekly_and_yearly_subscriptions_once() -> None:
    tram = [_exp(d, "Tram card", 50, category="transport") for d in
            ("2026-09-03", "2026-09-10", "2026-09-17", "2026-09-24", "2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22")]
    expenses = [_exp("2026-10-05", "Café", 220, category="food"), _exp("2026-09-12", "Marjane", 900, category="groceries"),
                _exp("2026-09-02", "Anthropic", 200, currency="USD"), *tram]
    extra = [{"id": "s1", "name": "Tram card", "amount": 50.0, "currency": "MAD", "category": "transport",
              "on": f"2026-10-{d:02d}", "frequency": "weekly"} for d in (1, 8, 15, 22, 29)]
    subs = [{"id": "s1", "key": "tram", "name": "Tram card", "amount": 50.0, "currency": "MAD"},
            {"id": "s2", "key": merchant_key("Anthropic"), "name": "Claude", "amount": 200.0, "currency": "USD"}]
    ov = _ov(TODAY, extra=extra, expenses=expenses, subscriptions=subs)
    assert ov["spent"]["expenses"] == 420.0  # the paid tram charges count once, as expenses
    assert [(u["name"], u["date"]) for u in ov["upcoming"]] == [
        ("Salaire", "2026-10-25"), ("Internet", "2026-10-28"), ("Tram card", "2026-10-29"),
    ]  # today's charge was paid this morning
    fc = ov["forecast"]
    assert fc["variable_spent"] == 220.0 and fc["daily_avg"] == 10.0  # neither the tram card nor Claude
    assert fc["history_daily_avg"] == 30.0  # September: Marjane only
    assert fc["remaining_recurring"] == 350.0  # Internet + the tram charge of the 29th, once
    assert fc["projected_spent"] == round(fc["spent_so_far"] + 350 + 10 * 9, 2)

    default = _ov(TODAY, extra=extra, expenses=expenses)  # subscriptions default to the ones of extra_recurring
    assert default["forecast"]["remaining_recurring"] == 350.0 and default["forecast"]["daily_avg"] == 10.0


# ------------------------------------------------------------------ subscriptions API


def _seed_netflix(hub) -> None:
    for day, merchant in (("2026-07-14", "PRLV NETFLIX.COM"), ("2026-08-14", "Netflix"), ("2026-09-14", "Netflix"),
                          ("2026-10-14", "NETFLIX")):
        assert hub.post("/expenses", json={"date": day, "merchant": merchant, "total": 65, "category": "entertainment"}).status_code == 201
    for day in ("2024-11-05", "2025-11-05"):
        assert hub.post("/expenses", json={"date": day, "merchant": "Anthropic", "total": 200, "currency": "USD",
                                           "category": "bills"}).status_code == 201


def test_subscription_suggestions_accept_dismiss(app_hub) -> None:
    _seed_netflix(app_hub)
    r = app_hub.get("/money/subscriptions/suggestions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of"] == "2026-10-22" and [s["key"] for s in body["suggestions"]] == ["netflix", "claude"]

    r = app_hub.post("/money/subscriptions/accept", json={"key": "netflix", "name": "Netflix Premium"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["recurring"]["name"] == "Netflix Premium" and out["recurring"]["day"] == 14
    assert out["recurring"]["category"] == "entertainment" and out["recurring"]["start_month"] == "2026-10"
    assert out["subscription"]["status"] == "accepted" and out["subscription"]["active"] is False
    assert app_hub.post("/money/subscriptions/accept", json={"key": "netflix"}).status_code == 409
    assert [s["key"] for s in app_hub.get("/money/subscriptions/suggestions").json()["suggestions"]] == ["claude"]

    ov = app_hub.get("/money/overview").json()  # the October charge was paid by the expense: counted once
    nf = next(r for r in ov["recurring"] if r["name"] == "Netflix Premium")
    assert nf["paid"] is True and ov["spent"]["total"] == 65 and "forecast" in ov

    r = app_hub.post("/money/subscriptions/accept", json={"key": "claude", "amount": 210})
    assert r.status_code == 201, r.text
    sub = r.json()["subscription"]
    assert r.json()["recurring"] is None and sub["active"] is True and sub["cadence"] == "yearly"
    assert sub["amount"] == 210 and sub["currency"] == "USD" and sub["next_date"] == "2026-11-05"
    nov = app_hub.get("/money/overview", params={"month": "2026-11"}).json()
    assert ("Claude", "2026-11-05") in [(u["name"], u["date"]) for u in nov["upcoming"]]
    assert nov["forecast"]["remaining_recurring"] == 65 + 2100

    subs = app_hub.get("/money/subscriptions").json()["subscriptions"]
    assert {s["key"]: s["status"] for s in subs} == {"netflix": "accepted", "claude": "accepted"}
    claude_id = next(s["id"] for s in subs if s["key"] == "claude")
    assert app_hub.delete(f"/money/subscriptions/{claude_id}").status_code == 204
    assert app_hub.delete(f"/money/subscriptions/{claude_id}").status_code == 404

    r = app_hub.post("/money/subscriptions/dismiss", json={"key": "claude"})
    assert r.status_code == 200 and r.json()["subscription"]["status"] == "dismissed"
    assert app_hub.get("/money/subscriptions/suggestions").json()["suggestions"] == []
    assert app_hub.post("/money/subscriptions/accept", json={"key": "spotify"}).status_code == 404
    assert app_hub.post("/money/subscriptions/accept", json={"key": "claude", "currency": "nope"}).status_code == 422


def test_deleting_an_accepted_monthly_charge_frees_the_merchant(app_hub) -> None:
    _seed_netflix(app_hub)
    out = app_hub.post("/money/subscriptions/accept", json={"key": "netflix"}).json()
    assert app_hub.delete(f"/money/recurring/{out['recurring']['id']}").status_code == 204
    assert app_hub.get("/money/subscriptions").json()["subscriptions"] == []  # forgotten with its charge
    assert "netflix" in [s["key"] for s in app_hub.get("/money/subscriptions/suggestions").json()["suggestions"]]
    assert app_hub.post("/money/subscriptions/accept", json={"key": "netflix"}).status_code == 201

    # a row already orphaned (charge deleted before this fix) no longer blocks the merchant either
    fa.sql("DELETE FROM app_recurring")
    assert "netflix" in [s["key"] for s in app_hub.get("/money/subscriptions/suggestions").json()["suggestions"]]
    r = app_hub.post("/money/subscriptions/accept", json={"key": "netflix"})
    assert r.status_code == 201, r.text
    assert len(app_hub.get("/money/subscriptions").json()["subscriptions"]) == 1


# ------------------------------------------------------------------ bills


def test_bills_push_two_days_before(app_hub, sent) -> None:
    assert app_hub.post("/money/recurring", json={"name": "Internet Inwi", "amount": 349, "day": 24,
                                                  "start_month": "2026-01"}).status_code == 201
    assert app_hub.post("/money/recurring", json={"name": "Loyer", "amount": 3000, "day": 1}).status_code == 201
    for day in ("2026-10-02", "2026-10-09", "2026-10-16"):
        app_hub.post("/expenses", json={"date": day, "merchant": "Basic Fit", "total": 50, "category": "health"})
    assert app_hub.post("/money/subscriptions/accept", json={"key": "basic fit"}).status_code == 201  # weekly: 23 Oct

    r = app_hub.post("/notify/run", params={"kind": "bills", "preview": True})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == "preview" and sent == []
    r = app_hub.post("/notify/run", params={"kind": "bills"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] is True and out["count"] == 2
    assert [m["title"] for m in sent] == ["💳 Basic Fit : 50 MAD demain", "💳 Internet Inwi : 349 MAD après-demain"]
    assert "samedi 24 octobre" in sent[1]["message"] and sent[0]["message"].count("hebdomadaire") == 1
    again = app_hub.post("/notify/run", params={"kind": "bills"}).json()
    assert again["sent"] is False and again["skipped"] == "already sent" and len(sent) == 2

    app_hub.put("/notify/prefs", json={"prefs": {"bills": False}})
    assert app_hub.post("/notify/run", params={"kind": "bills"}).json()["skipped"] == "turned off in the app"


def test_bills_nothing_due(app_hub, sent) -> None:
    out = app_hub.post("/notify/run", params={"kind": "bills"}).json()
    assert out["sent"] is False and out["skipped"] == "nothing to report" and sent == []


# ------------------------------------------------------------------ live budget alerts


def test_budget_alert_at_80_then_100_once(app_hub, sent) -> None:
    assert app_hub.put("/money/budgets/groceries", json={"amount": 1000}).status_code == 200
    add = lambda total, day="2026-10-20": app_hub.post(  # noqa: E731
        "/expenses", json={"date": day, "merchant": "Marjane", "total": total, "category": "groceries"})
    assert add(500).status_code == 201 and sent == []
    assert add(320).status_code == 201
    assert [m["title"] for m in sent] == ["⚠ Budget courses : 82 %"]
    assert "il reste 180 MAD" in sent[0]["message"]
    assert add(50).status_code == 201 and len(sent) == 1  # still 80 %: already told
    assert add(900, "2026-09-03").status_code == 201 and len(sent) == 1  # a past month never alerts
    assert add(200).status_code == 201
    assert sent[-1]["title"] == "⛔ Budget courses dépassé : 107 %" and sent[-1]["priority"] == 4
    assert add(10).status_code == 201 and len(sent) == 2
    # the other paths go through the same insert: a voice memo in another category with a budget
    assert app_hub.put("/money/budgets/transport", json={"amount": 100}).status_code == 200
    fa.TRANSCRIPTS["groq"] = "taxi 95 dh"
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": [{"type": "expense", "amount": 95, "currency": "MAD",
                                                            "merchant": "Taxi", "category": "transport"}]}))
    r = app_hub.post("/money/voice", files={"file": ("m.ogg", b"OggS-audio", "audio/ogg")}, data={"kind": "auto"})
    assert r.status_code == 201, r.text
    assert sent[-1]["title"] == "⚠ Budget transport : 95 %"


def test_budget_alert_respects_prefs_and_never_breaks_the_insert(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    app_hub.put("/money/budgets/food", json={"amount": 100})
    app_hub.put("/notify/prefs", json={"prefs": {"spending": False}})
    assert app_hub.post("/expenses", json={"total": 150, "category": "food"}).status_code == 201
    assert sent == []

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("D1 down")

    monkeypatch.setattr(money_intel, "_budget_push", boom)
    app_hub.put("/notify/prefs", json={"prefs": {"spending": True}})
    r = app_hub.post("/expenses", json={"total": 10, "category": "food"})
    assert r.status_code == 201 and sent == []


def test_budget_alert_timeout_releases_the_claim(app_hub, sent, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    app_hub.put("/money/budgets/food", json={"amount": 100})
    real_publish = notifier.publish

    async def slow_publish(*_a: Any, **_k: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(money_intel, "ALERT_DELIVER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(notifier, "publish", slow_publish)
    assert app_hub.post("/expenses", json={"total": 85, "category": "food"}).status_code == 201
    assert sent == [] and fa.sql("SELECT status, error FROM app_notify_log WHERE key LIKE 'spending:food:%'") == [
        ("failed", "timed out")]

    monkeypatch.setattr(notifier, "publish", real_publish)  # the next expense retries the same alert
    assert app_hub.post("/expenses", json={"total": 1, "category": "food"}).status_code == 201
    assert [m["title"] for m in sent] == ["⚠ Budget restaurants : 86 %"]
    assert fa.sql("SELECT status FROM app_notify_log WHERE key LIKE 'spending:food:%'") == [("sent",)]


def test_budget_alert_without_push_channel_is_silent(app_hub) -> None:
    app_hub.put("/money/budgets/food", json={"amount": 100})
    assert app_hub.post("/expenses", json={"total": 150, "category": "food"}).status_code == 201
    assert fa.sql("SELECT COUNT(*) FROM sqlite_master WHERE name = 'app_notify_log'") == [(0,)]


# ------------------------------------------------------------------ goals


def test_goals_crud_and_contributions(app_hub) -> None:
    acc = app_hub.post("/money/accounts", json={"name": "Épargne", "kind": "savings", "balance": 5000}).json()
    r = app_hub.post("/money/goals", json={"name": "Voyage Japon", "target": 12000, "saved": 2000,
                                           "deadline": "2027-03-31", "emoji": "🗾", "color": "#F5B43C",
                                           "account_id": acc["id"]})
    assert r.status_code == 201, r.text
    goal = r.json()
    assert goal["remaining"] == 10000 and goal["months_left"] == 5 and goal["per_month_needed"] == 2000
    assert goal["progress"] == pytest.approx(0.1667, abs=1e-4) and goal["account"] == {"id": acc["id"], "name": "Épargne"}
    assert goal["saved_mad"] == 2000 and goal["done"] is False and goal["overdue"] is False

    r = app_hub.post(f"/money/goals/{goal['id']}/contribute", json={"amount": 1000, "note": "salaire"})
    assert r.status_code == 201, r.text
    assert r.json()["goal"]["saved"] == 3000 and r.json()["goal"]["per_month_needed"] == 1800
    r = app_hub.post(f"/money/goals/{goal['id']}/contribute", json={"amount": -500, "date": "2026-10-20"})
    assert r.status_code == 201 and r.json()["goal"]["saved"] == 2500
    assert app_hub.post(f"/money/goals/{goal['id']}/contribute", json={"amount": -9000}).status_code == 422
    assert app_hub.post(f"/money/goals/{goal['id']}/contribute", json={"amount": 0}).status_code == 422

    detail = app_hub.get(f"/money/goals/{goal['id']}").json()
    assert [(c["amount"], c["date"]) for c in detail["contributions"]] == [(1000, "2026-10-22"), (-500, "2026-10-20")]

    app_hub.post("/money/goals", json={"name": "Fonds d'urgence", "target": 100, "currency": "EUR"})
    listed = app_hub.get("/money/goals").json()
    assert [g["name"] for g in listed["goals"]] == ["Voyage Japon", "Fonds d'urgence"]
    other = listed["goals"][1]
    assert other["deadline"] is None and other["per_month_needed"] is None and other["target_mad"] > 100
    assert listed["total_saved_mad"] == 2500

    r = app_hub.patch(f"/money/goals/{goal['id']}", json={"target": 2500, "deadline": None, "account_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True and r.json()["progress"] == 1.0 and r.json()["account"] is None
    assert app_hub.patch(f"/money/goals/{goal['id']}", json={"deadline": "2026-10-01", "target": 9000}).json()["overdue"] is True
    assert app_hub.patch(f"/money/goals/{goal['id']}", json={"account_id": "nope"}).status_code == 422
    assert app_hub.patch(f"/money/goals/{goal['id']}", json={"name": None}).status_code == 422
    assert app_hub.post("/money/goals", json={"name": "X", "target": 0}).status_code == 422
    assert app_hub.post("/money/goals", json={"name": "X", "target": 10, "deadline": "31/12/2026"}).status_code == 422
    assert app_hub.post("/money/goals", json={"name": "X", "target": 10, "color": "red"}).status_code == 422
    assert app_hub.delete(f"/money/goals/{goal['id']}").status_code == 204
    assert app_hub.get(f"/money/goals/{goal['id']}").status_code == 404
    assert fa.sql("SELECT COUNT(*) FROM app_goal_contributions") == [(0,)]
