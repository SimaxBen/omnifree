"""/money/debts: loans with partial repayments, voice loans ("j'ai prêté 200 dh à Ayoub"), and the "debts" pushes
(due dates daily, money lent and not returned on Mondays). Fake D1, fake ntfy, no network."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from hub_api import notifier
from hub_api.routers import debts as debts_mod
from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers.debts import same_person
from hub_api.routers.money import parse_money_entries
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 19)  # a Monday
FX = {"result": "success", "rates": {"MAD": 1, "EUR": 0.08}}  # 1 EUR = 12.5 MAD


class Clock:
    today = TODAY


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    fa.reset()
    Clock.today = TODAY
    for mod in (exp_mod, money_mod, notes_mod, notify_mod, debts_mod):
        monkeypatch.setattr(mod, "local_today", lambda: Clock.today)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(200, json=FX)))
    monkeypatch.setenv("NTFY_TOPIC", "omnifree-test-topic")
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"m{len(log)}"})

    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


@pytest.fixture
def app_hub(make_hub, sent):
    return make_hub(fa.apps_plugins())


def _debt(hub, **body):
    r = hub.post("/money/debts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ pure


def test_same_person_and_voice_parsing() -> None:
    assert same_person("Ayoub", "ayoub") and same_person("Ayoub B.", "Ayoub Benali") and same_person("Élise", "elise")
    assert not same_person("Ayoub", "Karim") and not same_person("", "Karim")
    data = {"entries": [
        {"type": "lend", "amount": 200, "currency": "dh", "person": "Ayoub", "date": "hier"},
        {"type": "repaid", "amount": "100", "merchant": "Ayoub"},
        {"type": "borrow", "amount": 500, "person": " "},  # no person: dropped
        {"type": "expense", "amount": 45, "merchant": "Taxi", "category": "transport"},
    ]}
    out = parse_money_entries(data, TODAY)
    assert [(e["type"], e["amount"], e.get("person"), e["date"]) for e in out] == [
        ("lend", 200.0, "Ayoub", "2026-10-18"), ("repaid", 100.0, "Ayoub", "2026-10-19"), ("expense", 45.0, None, "2026-10-19")]
    assert out[0]["currency"] == "MAD"
    forced = parse_money_entries(data, TODAY, "expense")
    assert [e["type"] for e in forced] == ["lend", "repaid", "expense"]  # loans keep their type


# ------------------------------------------------------------------ CRUD + repayments


def test_debts_crud_repay_and_summary(app_hub) -> None:
    a = _debt(app_hub, person="Ayoub", direction="lent", amount=200, date="2026-09-01", note="loyer")
    assert (a["remaining"], a["paid"], a["settled"], a["origin"], a["age_days"]) == (200.0, 0.0, False, "manual", 48)
    k = _debt(app_hub, person="Karim", direction="borrowed", amount=40, currency="eur", due_date="2026-10-25")
    assert k["date"] == "2026-10-19" and k["currency"] == "EUR"

    for bad in ({"person": "", "direction": "lent", "amount": 1}, {"person": "x", "direction": "gift", "amount": 1},
                {"person": "x", "direction": "lent", "amount": 0}, {"person": "x", "direction": "lent", "amount": 1, "due_date": "soon"},
                {"person": "x", "direction": "lent", "amount": 1, "date": "2026-10-10", "due_date": "2026-10-01"}):
        assert app_hub.post("/money/debts", json=bad).status_code == 422, bad

    listing = app_hub.get("/money/debts").json()
    s = listing["summary"]
    assert (s["owed_to_me"], s["i_owe"], s["net"]) == (200.0, 500.0, -300.0)
    assert [(p["person"], p["net"]) for p in s["by_person"]] == [("Karim", -500.0), ("Ayoub", 200.0)]

    r = app_hub.post(f"/money/debts/{a['id']}/repay", json={"amount": 50, "date": "2026-10-01"})
    assert r.status_code == 201
    a = r.json()
    assert (a["paid"], a["remaining"], a["settled"]) == (50.0, 150.0, False)
    assert app_hub.post(f"/money/debts/{a['id']}/repay", json={"amount": 151}).status_code == 422
    a = app_hub.post(f"/money/debts/{a['id']}/repay", json={}).json()  # the rest
    assert (a["paid"], a["remaining"], a["settled"], len(a["payments"])) == (200.0, 0.0, True, 2)
    assert app_hub.post(f"/money/debts/{a['id']}/repay", json={"amount": 1}).status_code == 409
    assert [d["id"] for d in app_hub.get("/money/debts").json()["debts"]] == [k["id"]]
    assert [d["id"] for d in app_hub.get("/money/debts", params={"status": "settled"}).json()["debts"]] == [a["id"]]
    assert len(app_hub.get("/money/debts", params={"status": "all", "person": "ayoub"}).json()["debts"]) == 1

    # Removing a repayment opens the loan again.
    pay = a["payments"][-1]["id"]
    a = app_hub.delete(f"/money/debts/{a['id']}/payments/{pay}").json()
    assert (a["settled"], a["remaining"]) == (False, 150.0)
    assert app_hub.delete(f"/money/debts/{a['id']}/payments/{pay}").status_code == 404

    # PATCH: amount below what was repaid is refused; lowering it to the repaid amount settles it.
    assert app_hub.patch(f"/money/debts/{a['id']}", json={"amount": 10}).status_code == 422
    assert app_hub.patch(f"/money/debts/{a['id']}", json={"amount": 50}).json()["settled"] is True
    p = app_hub.patch(f"/money/debts/{k['id']}", json={"settled": True, "note": "oublié", "due_date": None}).json()
    assert (p["settled"], p["note"], p["due_date"], p["remaining"]) == (True, "oublié", None, 0.0)
    assert app_hub.patch(f"/money/debts/{k['id']}", json={"person": None}).status_code == 422
    assert app_hub.get("/money/debts/nope").status_code == 404
    assert app_hub.delete(f"/money/debts/{k['id']}").status_code == 204
    assert app_hub.get(f"/money/debts/{k['id']}").status_code == 404


# ------------------------------------------------------------------ voice


def _voice(app_hub, transcript: str, entries: list[dict[str, Any]]):
    fa.TRANSCRIPTS["groq"] = transcript
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": entries}))
    r = app_hub.post("/money/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")})
    assert r.status_code == 201, r.text
    return r.json()


def test_voice_lend_borrow_and_repay(app_hub) -> None:
    out = _voice(app_hub, "j'ai prêté 200 dh à Ayoub et j'ai emprunté 500 à Karim", [
        {"type": "lend", "amount": 200, "currency": "dh", "person": "Ayoub", "merchant": "Ayoub", "category": None},
        {"type": "borrow", "amount": 500, "currency": "MAD", "person": "Karim"},
    ])
    assert out["expenses"] == [] and out["incomes"] == []
    assert [(d["action"], d["type"], d["person"]) for d in out["debts"]] == [("created", "lend", "Ayoub"), ("created", "borrow", "Karim")]
    first = out["debts"][0]["debts"][0]
    assert (first["direction"], first["amount"], first["origin"]) == ("lent", 200.0, "voice")
    prompt = fa.CHAT_LOG[-1][1].messages[0].content
    assert "lend" in prompt and "repaid" in prompt and "prêté" in prompt

    _debt(app_hub, person="Ayoub", direction="lent", amount=100, date="2026-10-01")  # older loan: repaid first
    out = _voice(app_hub, "Ayoub m'a rendu 250", [{"type": "repaid", "amount": 250, "person": "ayoub"}])
    (res,) = out["debts"]
    assert (res["action"], res["applied"], res["unapplied"]) == ("repaid", 250.0, 0.0)
    assert [(d["amount"], d["remaining"], d["settled"]) for d in res["debts"]] == [(100.0, 0.0, True), (200.0, 50.0, False)]

    out = _voice(app_hub, "Ayoub m'a rendu 80", [{"type": "repaid", "amount": 80, "person": "Ayoub"}])
    assert (out["debts"][0]["applied"], out["debts"][0]["unapplied"]) == (50.0, 30.0)

    out = _voice(app_hub, "Sara m'a rendu 20", [{"type": "repaid", "amount": 20, "person": "Sara"}])
    assert (out["debts"][0]["action"], out["debts"][0]["debts"]) == ("unmatched", [])

    out = _voice(app_hub, "j'ai rendu 200 à Karim", [{"type": "repay", "amount": 200, "person": "Karim"}])
    assert out["debts"][0]["debts"][0]["remaining"] == 300.0


# ------------------------------------------------------------------ pushes


def test_debts_push_monday_and_due_dates(app_hub, sent) -> None:
    _debt(app_hub, person="Ayoub", direction="lent", amount=200, date="2026-09-20")  # 29 days: stale
    _debt(app_hub, person="Sara", direction="lent", amount=50, date="2026-10-10")  # 9 days: too recent
    _debt(app_hub, person="Karim", direction="borrowed", amount=300, date="2026-09-01", due_date="2026-10-19")
    _debt(app_hub, person="Omar", direction="lent", amount=80, date="2026-10-01", due_date="2026-10-17")  # 2 days late
    _debt(app_hub, person="Old", direction="lent", amount=10, date="2026-09-01", due_date="2026-10-01")  # too late now

    preview = app_hub.post("/notify/run", params={"kind": "debts", "preview": 1}).json()
    assert preview["skipped"] == "preview" and preview["title"] == "3 rappels de dettes" and sent == []

    r = app_hub.post("/notify/run", params={"kind": "debts"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] is True and out["count"] == 3
    titles = [m["title"] for m in sent]
    assert titles[0] == "💸 Omar doit te rendre 80 MAD"  # due dates in order
    assert titles[1] == "💸 Tu dois rendre 300 MAD à Karim"
    assert titles[2] == "On te doit encore 290 MAD"
    weekly = sent[2]["message"]
    assert "Ayoub : 200 MAD (depuis 4 sem.)" in weekly and "Old : 10 MAD" in weekly and "Sara" not in weekly
    assert sent[0]["message"].startswith("En retard : c'était prévu le 17/10")
    assert sent[1]["message"].startswith("Échéance aujourd'hui.")

    again = app_hub.post("/notify/run", params={"kind": "debts"}).json()
    assert again["sent"] is False and again["skipped"] == "already sent" and len(sent) == 3

    Clock.today = date(2026, 10, 20)  # Tuesday: no weekly list; the due dates are still in their grace days
    tue = app_hub.post("/notify/run", params={"kind": "debts"}).json()
    assert tue["skipped"] == "already sent" and len(sent) == 3
    Clock.today = date(2026, 10, 23)  # past the grace days
    assert app_hub.post("/notify/run", params={"kind": "debts"}).json()["skipped"] == "nothing to report"

    app_hub.put("/notify/prefs", json={"prefs": {"debts": False}})
    assert app_hub.post("/notify/run", params={"kind": "debts"}).json()["skipped"] == "turned off in the app"


def test_settled_by_hand_stays_settled_and_repaid_loans_stay_closed(app_hub) -> None:
    # "Forget the rest": 500 lent, 200 back, settled by hand.
    d = _debt(app_hub, person="Sara", direction="lent", amount=500, date="2026-10-01")
    d = app_hub.post(f"/money/debts/{d['id']}/repay", json={"amount": 100}).json()
    d = app_hub.post(f"/money/debts/{d['id']}/repay", json={"amount": 100}).json()
    d = app_hub.patch(f"/money/debts/{d['id']}", json={"settled": True}).json()
    assert (d["settled"], d["paid"]) == (True, 200.0)
    # The loan sheet saves every field back (same amount): editing the note must not open it again.
    same = {"person": "Sara", "direction": "lent", "amount": 500, "currency": "MAD", "date": "2026-10-01",
            "due_date": None, "note": "cadeau"}
    d = app_hub.patch(f"/money/debts/{d['id']}", json=same).json()
    assert (d["settled"], d["note"]) == (True, "cadeau")
    d = app_hub.patch(f"/money/debts/{d['id']}", json={**same, "amount": 600}).json()
    assert d["settled"] is True  # still forgiven
    # Removing one of its repayments does not open it again either.
    d = app_hub.delete(f"/money/debts/{d['id']}/payments/{d['payments'][0]['id']}").json()
    assert (d["settled"], d["paid"]) == (True, 100.0)
    # A loan closed by its repayments: "open it again" makes no sense (nothing left), it is refused.
    full = _debt(app_hub, person="Omar", direction="borrowed", amount=50)
    full = app_hub.post(f"/money/debts/{full['id']}/repay", json={}).json()
    assert full["settled"] is True
    r = app_hub.patch(f"/money/debts/{full['id']}", json={"settled": False})
    assert r.status_code == 422 and "remove a repayment" in r.text
    assert app_hub.patch(f"/money/debts/{full['id']}", json={"settled": True}).json()["settled"] is True  # no-op
    # Raising its amount opens it again (there is something left now).
    again = app_hub.patch(f"/money/debts/{full['id']}", json={"amount": 80}).json()
    assert (again["settled"], again["remaining"]) == (False, 30.0)
    # An open loan with nothing left (legacy rows) cannot take a zero repayment.
    fa.sql("UPDATE app_debts SET settled = 0, amount = 50 WHERE id = ?", [full["id"]])
    assert app_hub.post(f"/money/debts/{full['id']}/repay", json={}).status_code == 409
    # Amounts are bounded.
    assert app_hub.post("/money/debts", json={"person": "x", "direction": "lent", "amount": 1e12}).status_code == 422


def test_voice_duplicates_are_not_stored_twice_and_can_be_undone(app_hub) -> None:
    lend = [{"type": "lend", "amount": 200, "currency": "MAD", "person": "Ayoub"}]
    first = _voice(app_hub, "j'ai prêté 200 dh à Ayoub", lend)["debts"][0]
    assert first["action"] == "created" and first["payment_ids"] == []
    # The same memo sent again (the first answer timed out on the phone): not a second loan.
    again = _voice(app_hub, "j'ai prêté 200 dh à Ayoub", lend)["debts"][0]
    assert again["action"] == "duplicate" and again["debts"][0]["id"] == first["debts"][0]["id"]
    assert len(app_hub.get("/money/debts").json()["debts"]) == 1
    # Two loans said in one memo are two loans.
    two = _voice(app_hub, "j'ai prêté 50 à Karim, et encore 50 à Karim", [
        {"type": "lend", "amount": 50, "person": "Karim"}, {"type": "lend", "amount": 50, "person": "Karim"}])
    assert [d["action"] for d in two["debts"]] == ["created", "created"]
    # A different amount, or later than the window, is a new loan.
    assert _voice(app_hub, "j'ai prêté 300 à Ayoub", [{**lend[0], "amount": 300}])["debts"][0]["action"] == "created"
    fa.sql("UPDATE app_debts SET created_at = '2026-01-01T00:00:00Z'")
    assert _voice(app_hub, "j'ai prêté 200 dh à Ayoub", lend)["debts"][0]["action"] == "created"

    # Repayments: applied once, the same memo again is a duplicate; payment_ids lists what to undo.
    repaid = [{"type": "repaid", "amount": 250, "person": "Ayoub"}]
    out = _voice(app_hub, "Ayoub m'a rendu 250", repaid)["debts"][0]
    assert out["action"] == "repaid" and len(out["payment_ids"]) == len(out["debts"]) >= 1
    dup = _voice(app_hub, "Ayoub m'a rendu 250", repaid)["debts"][0]
    assert dup["action"] == "duplicate" and dup["payment_ids"] == []
    paid = sum(d["paid"] for d in app_hub.get("/money/debts", params={"status": "all", "person": "Ayoub"}).json()["debts"])
    assert paid == 250.0
    for debt, pid in zip(out["debts"], out["payment_ids"], strict=True):  # what the PWA's Undo does
        assert app_hub.delete(f"/money/debts/{debt['id']}/payments/{pid}").status_code == 200
    paid = sum(d["paid"] for d in app_hub.get("/money/debts", params={"status": "all", "person": "Ayoub"}).json()["debts"])
    assert paid == 0.0
