"""/money/splits: groups, shared expenses, balances, settle-up and the owner's share in app_expenses (fake D1, no network)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from hub_api.routers import expenses as exp_mod
from hub_api.routers import money as money_mod
from hub_api.routers import splits as splits_mod
from hub_api.routers.splits import compute_balances, equal_shares, settle_up
from tests.hub_api import fakes_apps as fa

TODAY = date(2026, 10, 20)
FX = {"result": "success", "rates": {"MAD": 1, "EUR": 0.08}}  # 1 EUR = 12.5 MAD


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    for mod in (exp_mod, money_mod, splits_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    monkeypatch.setattr(money_mod, "fx_transport", httpx.MockTransport(lambda r: httpx.Response(200, json=FX)))


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


# ------------------------------------------------------------------ pure maths


def test_equal_shares_balances_and_settle_up() -> None:
    assert equal_shares(100, ["a", "b", "c"]) == {"a": 33.34, "b": 33.33, "c": 33.33}
    assert sum(equal_shares(10.01, ["a", "b"]).values()) == pytest.approx(10.01)
    assert equal_shares(5, []) == {}
    entries = [
        {"paid_by": "a", "amount": 90, "shares": {"a": 30, "b": 30, "c": 30}},
        {"paid_by": "b", "amount": 30, "shares": {"a": 10, "b": 10, "c": 10}},
        {"paid_by": "c", "amount": 15, "shares": {"a": 15}},  # a settlement-like transfer
    ]
    bal = compute_balances(["a", "b", "c"], entries)
    assert {m: v["net"] for m, v in bal.items()} == {"a": 35.0, "b": -10.0, "c": -25.0}
    assert bal["a"] == {"paid": 90.0, "share": 55.0, "net": 35.0}
    assert settle_up({m: v["net"] for m, v in bal.items()}) == [
        {"from": "c", "to": "a", "amount": 25.0}, {"from": "b", "to": "a", "amount": 10.0}]
    # Greedy pairing never needs more than n - 1 payments and always zeroes everyone.
    nets = {"a": 50.0, "b": 20.0, "c": -30.0, "d": -25.0, "e": -15.0}
    plan = settle_up(nets)
    assert len(plan) <= 4
    after = dict(nets)
    for p in plan:
        after[p["from"]] += p["amount"]
        after[p["to"]] -= p["amount"]
    assert all(abs(v) < 0.001 for v in after.values())
    assert settle_up({"a": 0.0, "b": 0.0}) == []


# ------------------------------------------------------------------ groups and shared expenses


def _group(hub, **body):
    r = hub.post("/money/splits", json={"name": "Chefchaouen", "members": ["Ayoub", "Sara"], **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_split_flow_and_owner_share_rule(app_hub) -> None:
    g = _group(app_hub)
    me, ayoub, sara = (m["id"] for m in g["members"])
    assert g["me"] == me and [m["name"] for m in g["members"]] == ["Me", "Ayoub", "Sara"] and g["settle"] == []
    for bad in ({"name": "x", "members": []}, {"name": "x", "members": ["Me"]}, {"name": "x", "members": ["Sara", "sara"]},
                {"name": "", "members": ["A"]}):
        assert app_hub.post("/money/splits", json=bad).status_code == 422, bad

    # I pay 300 for the riad, split equally: only my 100 counts as my spending.
    r = app_hub.post(f"/money/splits/{g['id']}/expenses",
                     json={"description": "Riad", "amount": 300, "paid_by": me, "category": "travel", "date": "2026-10-18"})
    assert r.status_code == 201, r.text
    riad = r.json()["entry"]
    assert riad["shares"] == {me: 100.0, ayoub: 100.0, sara: 100.0} and riad["expense_id"]
    mine = app_hub.get(f"/expenses/{riad['expense_id']}").json()
    assert (mine["merchant"], mine["total"], mine["category"], mine["source"], mine["source_ref"]) == (
        "Riad", 100.0, "travel", "split", riad["id"])
    assert "Your share of 300.00 MAD (split: Chefchaouen)" in mine["note"]

    # Sara pays dinner 90 for Sara and me only: not my spending (I owe her), nothing mirrored.
    dinner = app_hub.post(f"/money/splits/{g['id']}/expenses",
                          json={"description": "Dinner", "amount": 90, "paid_by": sara, "participants": [sara, me]}).json()["entry"]
    assert dinner["expense_id"] is None and dinner["shares"] == {me: 45.0, sara: 45.0}

    # Custom shares must add up.
    bad = {"description": "Taxi", "amount": 60, "paid_by": me, "split": "custom", "shares": {me: 10, ayoub: 20}}
    assert app_hub.post(f"/money/splits/{g['id']}/expenses", json=bad).status_code == 422
    taxi = app_hub.post(f"/money/splits/{g['id']}/expenses",
                        json={**bad, "shares": {me: 10, ayoub: 50}, "category": "transport"}).json()
    detail = taxi["group"]
    nets = {b["name"]: b["net"] for b in detail["balances"]}
    assert nets == {"Me": 205.0, "Ayoub": -150.0, "Sara": -55.0}
    assert [(s["from_name"], s["to_name"], s["amount"]) for s in detail["settle"]] == [("Ayoub", "Me", 150.0), ("Sara", "Me", 55.0)]
    assert detail["totals"] == {"spent": 450.0, "my_share": 155.0, "my_net": 205.0, "count": 3}

    # Budgets/overview only see my shares: 100 (riad) + 10 (taxi).
    month = app_hub.get("/expenses", params={"month": "2026-10"}).json()
    assert sorted(e["total"] for e in month["expenses"]) == [10.0, 100.0]
    assert app_hub.get("/money/overview", params={"month": "2026-10"}).json()["spent"]["expenses"] == 110.0

    for wrong in ({"description": "x", "amount": 5, "paid_by": "nobody"},
                  {"description": "x", "amount": 5, "paid_by": me, "participants": ["ghost"]},
                  {"description": "x", "amount": 5, "paid_by": me, "split": "custom"}):
        assert app_hub.post(f"/money/splits/{g['id']}/expenses", json=wrong).status_code == 422, wrong

    # Editing keeps the mirror in sync: Ayoub paid the riad after all -> my share leaves my expenses.
    r = app_hub.patch(f"/money/splits/{g['id']}/expenses/{riad['id']}", json={"amount": 330})
    assert r.status_code == 200
    riad2 = r.json()["entry"]
    assert riad2["shares"][me] == 110.0
    assert app_hub.get(f"/expenses/{riad2['expense_id']}").json()["total"] == 110.0
    assert app_hub.get(f"/expenses/{riad['expense_id']}").status_code == 404  # the old mirror is gone
    r = app_hub.patch(f"/money/splits/{g['id']}/expenses/{riad['id']}", json={"paid_by": ayoub})
    assert r.json()["entry"]["expense_id"] is None
    assert app_hub.get(f"/expenses/{riad2['expense_id']}").status_code == 404

    # Settle up: Sara pays me back.
    s = app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": sara, "to": me, "amount": 45})
    assert s.status_code == 201
    assert app_hub.post(f"/money/splits/{g['id']}/settle", json={"from": me, "to": me, "amount": 1}).status_code == 422
    settlement = next(e for e in s.json()["entries"] if e["kind"] == "settlement")
    assert settlement["description"] == "Sara → Me" and settlement["expense_id"] is None
    assert app_hub.patch(f"/money/splits/{g['id']}/expenses/{settlement['id']}", json={"amount": 1}).status_code == 409

    listing = app_hub.get("/money/splits").json()["groups"]
    assert [x["name"] for x in listing] == ["Chefchaouen"] and listing[0]["totals"]["count"] == 3

    # Members: rename and add are fine; removing someone with expenses or myself is not.
    members = [{"id": me, "name": "Moi"}, {"id": ayoub, "name": "Ayoub"}, {"id": sara, "name": "Sara"}, {"name": "Omar"}]
    p = app_hub.patch(f"/money/splits/{g['id']}", json={"members": members, "name": "Chaouen"})
    assert p.status_code == 200 and [m["name"] for m in p.json()["members"]] == ["Moi", "Ayoub", "Sara", "Omar"]
    assert p.json()["members"][0]["me"] is True
    assert app_hub.patch(f"/money/splits/{g['id']}", json={"members": members[1:]}).status_code == 422
    assert app_hub.patch(f"/money/splits/{g['id']}", json={"members": [members[0], members[1]]}).status_code == 409
    assert app_hub.patch(f"/money/splits/{g['id']}", json={"currency": "EUR"}).status_code == 409

    # Deleting a shared expense removes my mirrored share; deleting the group removes everything.
    taxi_entry = taxi["entry"]
    app_hub.delete(f"/money/splits/{g['id']}/expenses/{taxi_entry['id']}")
    assert app_hub.get(f"/expenses/{taxi_entry['expense_id']}").status_code == 404
    assert app_hub.delete(f"/money/splits/{g['id']}").status_code == 204
    assert app_hub.get(f"/money/splits/{g['id']}").status_code == 404
    assert fa.sql("SELECT COUNT(*) FROM app_split_expenses")[0][0] == 0
