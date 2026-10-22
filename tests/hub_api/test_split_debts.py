"""Split <-> Debts: the split groups are the source of truth and the Debts page shows derived, linked rows
(``origin = 'split'``), one per group and person, kept in sync on every change from either page (fake D1, no network).

The matrix: who paid (me / a member) x how it is split (equal / custom amounts / custom %), then edit, delete,
partial repayment from Debts, settle from Split, net per person, no double counting in expenses and budgets,
idempotent and self-healing sync, voice."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from hub_api import money_intel
from hub_api.routers import debts as debts_mod
from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import splits as splits_mod
from hub_api.routers.money import parse_money_entries
from hub_api.routers.splits import pair_balances, pair_debt, percent_shares
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)
FX = {"result": "success", "rates": {"MAD": 1, "EUR": 0.08}}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    for mod in (exp_mod, money_mod, splits_mod, debts_mod, notes_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(200, json=FX)))


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _group(hub, members=("Ayoub", "Sara"), name="Chefchaouen", **body) -> tuple[dict[str, Any], list[str]]:
    r = hub.post("/money/splits", json={"name": name, "members": list(members), **body})
    assert r.status_code == 201, r.text
    g = r.json()
    return g, [m["id"] for m in g["members"]]


def _add(hub, gid: str, **body) -> dict[str, Any]:
    r = hub.post(f"/money/splits/{gid}/expenses", json={"date": "2026-10-18", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _debts(hub, status: str = "open") -> dict[str, Any]:
    r = hub.get("/money/debts", params={"status": status})
    assert r.status_code == 200, r.text
    return r.json()


def _linked(hub, status: str = "all") -> dict[str, dict[str, Any]]:
    """The split-linked debts by person."""
    return {d["person"]: d for d in _debts(hub, status)["debts"] if d["origin"] == "split"}


def _owed(d: dict[str, Any]) -> tuple[str, float, float, bool]:
    return d["direction"], d["amount"], d["remaining"], d["settled"]


# ------------------------------------------------------------------ pure


def test_percent_shares_and_pair_maths() -> None:
    # 100 / 3 at 33.33 % each would lose a cent: it goes to the payer (b), else to the last member.
    assert percent_shares(100, {"a": 33.33, "b": 33.33, "c": 33.34}, "b") == {"a": 33.33, "b": 33.33, "c": 33.34}
    assert percent_shares(10, {"a": 33.33, "b": 33.33, "c": 33.34}, "b") == {"a": 3.33, "b": 3.34, "c": 3.33}
    assert percent_shares(10, {"a": 33.33, "b": 33.33, "c": 33.34}, None) == {"a": 3.33, "b": 3.33, "c": 3.34}
    assert percent_shares(200, {"a": 70, "b": 30}) == {"a": 140.0, "b": 60.0}

    group = {"members": [{"id": "me", "name": "Me", "me": True}, {"id": "ay", "name": "Ayoub"}]}

    def e(i, paid_by, amount, shares, kind="expense"):
        return {"id": i, "date": "2026-10-01", "created_at": i, "kind": kind, "description": i, "amount": amount,
                "paid_by": paid_by, "shares": shares}

    dinner, taxi = e("1", "me", 40, {"me": 20, "ay": 20}), e("2", "ay", 10, {"me": 5, "ay": 5})
    p = pair_balances(group, [dinner, taxi])["ay"]
    assert (p["owes_me"], p["i_owe"], p["net"]) == (2000, 500, 1500)
    assert pair_debt(p) == {"direction": "lent", "amount": 1500, "paid": 0}
    back = e("3", "ay", 10, {"me": 10}, "settlement")
    p = pair_balances(group, [dinner, taxi, back])["ay"]
    assert pair_debt(p) == {"direction": "lent", "amount": 1500, "paid": 1000}
    # The dinner deleted while Ayoub repaid 10: I would owe him 15 (5 taxi + his 10), which the routes refuse.
    assert pair_debt(pair_balances(group, [taxi, back])["ay"]) == {"direction": "borrowed", "amount": 1500, "paid": 0}
    # Owing, repaying, then being owed by a new expense is no conflict: the row follows the net.
    hotel, repaid = e("4", "ay", 240, {"me": 120, "ay": 120}), e("5", "me", 120, {"ay": 120}, "settlement")
    lunch = e("6", "me", 200, {"me": 100, "ay": 100})
    assert pair_debt(pair_balances(group, [hotel, repaid, lunch])["ay"]) == {"direction": "lent", "amount": 10000, "paid": 0}
    assert pair_debt(pair_balances(group, [])["ay"]) is None


def test_voice_split_parsing() -> None:
    raw = {"entries": [
        {"type": "split", "amount": 300, "currency": "dh", "merchant": "Dîner", "category": "food",
         "people": ["Ayoub", "Sara", "moi", "ayoub"]},
        {"type": "split", "amount": 50, "merchant": "Taxi", "people": []},  # nobody named: a plain expense
    ]}
    out = parse_money_entries(raw, TODAY)
    assert out[0]["type"] == "split" and out[0]["people"] == ["Ayoub", "Sara"] and out[0]["currency"] == "MAD"
    assert out[1]["type"] == "expense" and out[1]["amount"] == 50
    assert parse_money_entries(raw, TODAY, "income")[0]["type"] == "income"


# ------------------------------------------------------------------ who paid x how it is split


def test_i_paid_equal_shows_who_owes_me_and_only_my_share_is_spent(app_hub, monkeypatch) -> None:
    alerts: list[dict[str, Any]] = []

    async def spy(db, expense):
        alerts.append(expense)

    monkeypatch.setattr(money_intel, "budget_alert_after_insert", spy)
    assert app_hub.post("/money/rules", json={"pattern": "riad dar", "rename": "Riad Dar Zman"}).status_code == 201
    g, (me, ayoub, sara) = _group(app_hub)
    out = _add(app_hub, g["id"], description="Riad Dar", amount=300, paid_by=me, category="travel")
    assert out["entry"]["shares"] == {me: 100.0, ayoub: 100.0, sara: 100.0}

    linked = _linked(app_hub)
    assert _owed(linked["Ayoub"]) == ("lent", 100.0, 100.0, False)
    assert _owed(linked["Sara"]) == ("lent", 100.0, 100.0, False)
    assert linked["Ayoub"]["split"]["group_id"] == g["id"] and linked["Ayoub"]["split"]["group_name"] == "Chefchaouen"
    assert [(i["description"], i["effect"]) for i in linked["Ayoub"]["split"]["items"]] == [("Riad Dar", 100.0)]
    pairs = {p["name"]: p for p in out["group"]["pairs"]}
    assert pairs["Ayoub"]["direction"] == "owes_me" and pairs["Ayoub"]["debt_id"] == linked["Ayoub"]["id"]

    # The owner's share only, through the one insert path: merchant rule and live budget alert apply.
    month = app_hub.get("/expenses", params={"month": "2026-10"}).json()["expenses"]
    assert [(e["merchant"], e["total"], e["category"], e["source"]) for e in month] == [("Riad Dar Zman", 100.0, "travel", "split")]
    assert [a["total"] for a in alerts] == [100.0]
    summary = _debts(app_hub)["summary"]
    assert (summary["owed_to_me"], summary["i_owe"]) == (200.0, 0.0)


def test_someone_paid_for_me_shows_i_owe_them(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    _add(app_hub, g["id"], description="Dinner", amount=90, paid_by=sara, participants=[sara, me])
    linked = _linked(app_hub)
    assert list(linked) == ["Sara"]  # Ayoub has nothing to do with it
    assert _owed(linked["Sara"]) == ("borrowed", 45.0, 45.0, False)
    assert _debts(app_hub)["summary"]["i_owe"] == 45.0
    # Ayoub paid for Sara only: nothing between Ayoub and me.
    _add(app_hub, g["id"], description="Snack", amount=20, paid_by=ayoub, participants=[ayoub, sara])
    assert list(_linked(app_hub)) == ["Sara"]
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["total_by_currency"] == {"MAD": 45.0}


def test_custom_amounts_and_percentages(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    out = _add(app_hub, g["id"], description="Groceries", amount=100, paid_by=me, split="custom",
               shares={me: 50, ayoub: 30, sara: 20})
    assert out["entry"]["split"] == "custom" and out["entry"]["percents"] is None
    linked = _linked(app_hub)
    assert (linked["Ayoub"]["amount"], linked["Sara"]["amount"]) == (30.0, 20.0)

    # Percentages: 10 at 33.33/33.33/33.34 -> the leftover cent goes to the payer (Ayoub).
    out = _add(app_hub, g["id"], description="Coffee", amount=10, paid_by=ayoub, split="percent",
               percents={me: 33.33, ayoub: 33.33, sara: 33.34})
    entry = out["entry"]
    assert entry["split"] == "percent" and entry["percents"] == {me: 33.33, ayoub: 33.33, sara: 33.34}
    assert entry["shares"] == {me: 3.33, ayoub: 3.34, sara: 3.33}
    linked = _linked(app_hub)
    assert _owed(linked["Ayoub"]) == ("lent", 26.67, 26.67, False)  # 30 for groceries - my 3.33 coffee
    for bad in ({me: 50, ayoub: 40}, {me: 101}, {me: -1, ayoub: 101}, {"ghost": 100}):
        r = app_hub.post(f"/money/splits/{g['id']}/expenses",
                         json={"description": "x", "amount": 10, "paid_by": me, "split": "percent", "percents": bad})
        assert r.status_code == 422, bad
    assert app_hub.post(f"/money/splits/{g['id']}/expenses",
                        json={"description": "x", "amount": 10, "paid_by": me, "split": "percent"}).status_code == 422
    # Switching an expense to percentages and back keeps everything consistent.
    r = app_hub.patch(f"/money/splits/{g['id']}/expenses/{entry['id']}", json={"split": "equal", "participants": [me, ayoub]})
    assert r.status_code == 200 and r.json()["entry"]["percents"] is None
    assert r.json()["entry"]["shares"] == {me: 5.0, ayoub: 5.0}
    assert _linked(app_hub)["Ayoub"]["amount"] == 25.0


# ------------------------------------------------------------------ net, repay, settle, edit, delete


def test_net_per_person_and_repayments_from_both_pages(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    dinner = _add(app_hub, g["id"], description="Dinner", amount=60, paid_by=me)["entry"]  # 20 each
    _add(app_hub, g["id"], description="Taxi", amount=10, paid_by=ayoub, participants=[me, ayoub], date="2026-10-19")  # I owe 5
    ay = _linked(app_hub)["Ayoub"]
    assert _owed(ay) == ("lent", 15.0, 15.0, False)  # one row, the net
    assert [(i["description"], i["effect"]) for i in ay["split"]["items"]] == [("Dinner", 20.0), ("Taxi", -5.0)]

    # Partial repayment on the Debts page = a settlement in the group.
    r = app_hub.post(f"/money/debts/{ay['id']}/repay", json={"amount": 10})
    assert r.status_code == 201, r.text
    assert (r.json()["remaining"], r.json()["paid"]) == (5.0, 10.0)
    assert app_hub.post(f"/money/debts/{ay['id']}/repay", json={"amount": 6}).status_code == 422
    detail = app_hub.get(f"/money/splits/{g['id']}").json()
    (settlement,) = [e for e in detail["entries"] if e["kind"] == "settlement"]
    assert (settlement["paid_by"], settlement["shares"], settlement["amount"]) == (ayoub, {me: 10.0}, 10.0)
    assert r.json()["payments"][0]["id"] == settlement["id"]
    pairs = {p["name"]: p for p in detail["pairs"]}
    assert pairs["Ayoub"]["net"] == 5.0 and pairs["Sara"]["net"] == 20.0
    assert [(s["from_name"], s["to_name"], s["amount"]) for s in detail["settle"]] == [("Sara", "Me", 20.0), ("Ayoub", "Me", 5.0)]

    # Settle from the Split page: the Debts row is settled.
    s = app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": ayoub, "to": me, "amount": 5})
    assert s.status_code == 201
    assert app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": ayoub, "to": me, "amount": 1}).status_code == 422
    assert "Ayoub" not in _linked(app_hub, "open")
    ay = _linked(app_hub, "settled")["Ayoub"]
    assert _owed(ay) == ("lent", 15.0, 0.0, True) and len(ay["payments"]) == 2
    assert app_hub.post(f"/money/debts/{ay['id']}/repay", json={"amount": 1}).status_code == 409

    # Editing the dinner up opens the row again, with the repayments kept.
    r = app_hub.patch(f"/money/splits/{g['id']}/expenses/{dinner['id']}", json={"amount": 90})
    assert r.status_code == 200, r.text
    ay = _linked(app_hub)["Ayoub"]
    assert _owed(ay) == ("lent", 25.0, 10.0, False) and ay["paid"] == 15.0
    # Down below what Ayoub repaid: refused with a clear message, nothing changes.
    r = app_hub.patch(f"/money/splits/{g['id']}/expenses/{dinner['id']}", json={"amount": 30})
    assert r.status_code == 409 and "already paid you back 15.00 MAD" in r.json()["detail"]
    assert "you would owe Ayoub 10.00 MAD" in r.json()["detail"]
    assert app_hub.delete(f"/money/splits/{g['id']}/expenses/{dinner['id']}").status_code == 409
    assert _linked(app_hub)["Ayoub"]["amount"] == 25.0

    # Removing his repayments (one on each page), then the dinner: the row follows each step.
    pays = sorted(_linked(app_hub)["Ayoub"]["payments"], key=lambda p: -p["amount"])  # 10, then 5
    r = app_hub.delete(f"/money/debts/{ay['id']}/payments/{pays[0]['id']}")
    assert r.status_code == 200 and r.json()["paid"] == 5.0
    assert app_hub.delete(f"/money/debts/{ay['id']}/payments/nope").status_code == 404
    r = app_hub.delete(f"/money/splits/{g['id']}/expenses/{pays[1]['id']}")
    assert r.status_code == 200
    assert not [e for e in r.json()["entries"] if e["kind"] == "settlement"]
    assert app_hub.delete(f"/money/splits/{g['id']}/expenses/{dinner['id']}").status_code == 200
    linked = _linked(app_hub)
    assert _owed(linked["Ayoub"]) == ("borrowed", 5.0, 5.0, False) and "Sara" not in linked  # only the taxi left


def test_i_owe_repaid_from_debts_and_settled_from_split(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    _add(app_hub, g["id"], description="Hotel", amount=300, paid_by=sara, split="custom", shares={me: 120, sara: 180})
    sa = _linked(app_hub)["Sara"]
    assert _owed(sa) == ("borrowed", 120.0, 120.0, False)
    r = app_hub.post(f"/money/debts/{sa['id']}/repay", json={"amount": 20})
    assert r.status_code == 201 and r.json()["remaining"] == 100.0
    detail = app_hub.get(f"/money/splits/{g['id']}").json()
    (s,) = [e for e in detail["entries"] if e["kind"] == "settlement"]
    assert (s["paid_by"], s["shares"]) == (me, {sara: 20.0})
    assert app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": sara, "to": me, "amount": 10}).status_code == 422
    assert app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": me, "to": sara, "amount": 100}).status_code == 201
    assert _owed(_linked(app_hub)["Sara"]) == ("borrowed", 120.0, 0.0, True)
    # Settlements are transfers: my expenses still only hold my 120 share.
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["total_by_currency"] == {"MAD": 120.0}
    assert app_hub.get("/money/overview", params={"month": "2026-10"}).json()["spent"]["expenses"] == 120.0


def test_linked_rows_are_owned_by_the_group(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    _add(app_hub, g["id"], description="Dinner", amount=60, paid_by=me)
    ay = _linked(app_hub)["Ayoub"]
    for patch in ({"amount": 5}, {"person": "Karim"}, {"direction": "borrowed"}, {"settled": True}, {"currency": "EUR"}):
        r = app_hub.patch(f"/money/debts/{ay['id']}", json=patch)
        assert r.status_code == 409 and "Chefchaouen" in r.json()["detail"], patch
    assert app_hub.delete(f"/money/debts/{ay['id']}").status_code == 409
    # Its own fields can change, and the PWA sending the unchanged rest back is fine.
    r = app_hub.patch(f"/money/debts/{ay['id']}", json={"person": "ayoub", "direction": "lent", "amount": 20, "currency": "MAD",
                                                       "date": ay["date"], "note": "before Friday", "due_date": "2026-10-30"})
    assert r.status_code == 200, r.text
    assert (r.json()["note"], r.json()["due_date"]) == ("before Friday", "2026-10-30")
    _add(app_hub, g["id"], description="Lunch", amount=30, paid_by=me)  # resync keeps the note and due date
    ay = _linked(app_hub)["Ayoub"]
    assert (ay["amount"], ay["note"], ay["due_date"]) == (30.0, "before Friday", "2026-10-30")

    # Renames follow; standalone loans with the same person are grouped with it for display only.
    members = [{"id": me, "name": "Me"}, {"id": ayoub, "name": "Ayoûb"}, {"id": sara, "name": "Sara"}]
    assert app_hub.patch(f"/money/splits/{g['id']}", json={"members": members, "name": "Chaouen"}).status_code == 200
    ay = _linked(app_hub)["Ayoûb"]
    assert ay["split"]["group_name"] == "Chaouen" and ay["id"] == _linked(app_hub)["Ayoûb"]["id"]
    assert app_hub.post("/money/debts", json={"person": "ayoub", "direction": "borrowed", "amount": 50}).status_code == 201
    listing = _debts(app_hub)
    assert len([d for d in listing["debts"] if d["person"].lower().startswith("ayo")]) == 2
    by_person = {p["person"].lower(): p for p in listing["summary"]["by_person"]}
    assert len([k for k in by_person if k.startswith("ayo")]) == 1
    assert next(p for k, p in by_person.items() if k.startswith("ayo"))["net"] == -20.0  # 30 owed to me, I owe 50

    # Deleting the group deletes its rows and their repayments, never the standalone loan.
    ay = _linked(app_hub)["Ayoûb"]
    app_hub.post(f"/money/debts/{ay['id']}/repay", json={"amount": 5})
    assert app_hub.delete(f"/money/splits/{g['id']}").status_code == 204
    assert _linked(app_hub) == {}
    assert fa.sql("SELECT COUNT(*) FROM app_debt_payments")[0][0] == 0
    assert [d["origin"] for d in _debts(app_hub)["debts"]] == ["manual"]


def test_sync_is_idempotent_and_heals_interrupted_changes(app_hub) -> None:
    g, (me, ayoub, sara) = _group(app_hub)
    _add(app_hub, g["id"], description="Dinner", amount=60, paid_by=me)
    ay = _linked(app_hub)["Ayoub"]
    app_hub.post(f"/money/debts/{ay['id']}/repay", json={"amount": 5})

    def snapshot():
        debts = fa.sql("SELECT id, person, direction, amount, settled, split_detail FROM app_debts ORDER BY id")
        pays = fa.sql("SELECT id, debt_id, amount FROM app_debt_payments ORDER BY id")
        return debts, pays

    before = snapshot()
    for _ in range(2):  # a forced resync changes nothing (same ids, same rows)
        fa.sql("UPDATE app_split_groups SET rev = rev + 1 WHERE id = ?", [g["id"]])
        _debts(app_hub)
        assert snapshot() == before
    assert fa.sql("SELECT rev = debts_rev FROM app_split_groups")[0][0] == 1

    # A change interrupted before the sync (rows lost, rev bumped): repaired by the next read of either page.
    fa.sql("DELETE FROM app_debt_payments")
    fa.sql("DELETE FROM app_debts")
    fa.sql("UPDATE app_split_groups SET rev = rev + 1")
    detail = app_hub.get(f"/money/splits/{g['id']}").json()
    assert {p["name"]: p["debt_id"] is not None for p in detail["pairs"]} == {"Ayoub": True, "Sara": True}
    linked = _linked(app_hub)
    assert _owed(linked["Ayoub"]) == ("lent", 20.0, 15.0, False) and _owed(linked["Sara"]) == ("lent", 20.0, 20.0, False)
    # A group deleted half-way: its orphan rows go.
    fa.sql("DELETE FROM app_split_groups")
    assert _linked(app_hub) == {}
    # Groups from before this feature (no revision yet) get their rows on the first read.
    g2, (me2, ay2, _sa2) = _group(app_hub, name="Old")
    _add(app_hub, g2["id"], description="Pizza", amount=40, paid_by=me2, participants=[me2, ay2])
    fa.sql("DELETE FROM app_debts")
    fa.sql("UPDATE app_split_groups SET debts_rev = NULL")
    assert _owed(_linked(app_hub)["Ayoub"]) == ("lent", 20.0, 20.0, False)


# ------------------------------------------------------------------ voice


def _voice(app_hub, transcript: str, entries: list[dict[str, Any]]):
    fa.TRANSCRIPTS["groq"] = transcript
    fa.CHAT_SCRIPT["groq"].append(json.dumps({"entries": entries}))
    r = app_hub.post("/money/voice", files={"file": ("m.ogg", b"OggS", "audio/ogg")})
    assert r.status_code == 201, r.text
    return r.json()


def test_voice_split_and_voice_repayment(app_hub) -> None:
    said = "j'ai payé 300 dh le dîner avec Ayoub et Sara, chacun sa part"
    entry = {"type": "split", "amount": 300, "currency": "MAD", "merchant": "Dîner", "category": "food",
             "people": ["Ayoub", "Sara"]}
    # No group with both: a new one, named after it.
    out = _voice(app_hub, said, [{**entry, "date": "2026-10-15"}])
    assert out["expenses"] == []
    (res,) = out["debts"]
    assert (res["type"], res["action"], res["person"]) == ("split", "created", "Ayoub, Sara")
    assert res["split"]["group_created"] is True and res["split"]["my_share"] == 100.0
    assert sorted((d["person"], d["direction"], d["amount"]) for d in res["debts"]) == [("Ayoub", "lent", 100.0), ("Sara", "lent", 100.0)]
    gid = res["split"]["group_id"]
    assert app_hub.get(f"/money/splits/{gid}").json()["name"] == "Dîner"
    assert app_hub.get("/expenses", params={"month": "2026-10"}).json()["summary"]["total_by_currency"] == {"MAD": 100.0}
    prompt = fa.CHAT_LOG[-1][1].messages[0].content
    assert "split" in prompt and "chacun sa part" in prompt

    # A group with them exists (matched by name, accents and case ignored): it goes there.
    g, (me, ayoub, sara, karim) = _group(app_hub, members=("ayoub", "Sára", "Karim"), name="Flat")
    entry = {**entry, "amount": 90, "people": ["sara", "KARIM"]}
    out = _voice(app_hub, said, [entry])
    (res,) = out["debts"]
    assert res["split"]["group_id"] == g["id"] and res["split"]["group_created"] is False
    shares = next(e for e in app_hub.get(f"/money/splits/{g['id']}").json()["entries"])["shares"]
    assert shares == {me: 30.0, sara: 30.0, karim: 30.0}  # Ayoub was not named
    # The same memo sent again from another recording: not stored twice.
    out = _voice(app_hub, said, [entry])
    assert out["debts"][0]["action"] == "duplicate"
    assert len(app_hub.get(f"/money/splits/{g['id']}").json()["entries"]) == 1

    # "Sara m'a rendu 30": applied to the oldest open loan with her, a split-linked one, as a settlement.
    out = _voice(app_hub, "Sara m'a rendu 30", [{"type": "repaid", "amount": 30, "person": "Sara"}])
    (res,) = out["debts"]
    assert res["action"] == "repaid" and res["applied"] == 30.0
    settlements = [e for e in app_hub.get(f"/money/splits/{gid}").json()["entries"] if e["kind"] == "settlement"]
    assert [(s["amount"], s["origin"]) for s in settlements] == [(30.0, "voice")]
    assert res["payment_ids"] == [settlements[0]["id"]]
    # Undo (what the PWA does): removing the repayment deletes the settlement.
    debt_id = res["debts"][0]["id"]
    assert app_hub.delete(f"/money/debts/{debt_id}/payments/{res['payment_ids'][0]}").status_code == 200
    assert not [e for e in app_hub.get(f"/money/splits/{gid}").json()["entries"] if e["kind"] == "settlement"]
