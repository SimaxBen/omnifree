"""GET /budget: aggregation of provider, router and config numbers, thresholds and summary."""

from __future__ import annotations

from typing import Any

import pytest

from hub_api.budget_report import fraction_of, status_of

F = "tests.hub_api.fakes"


@pytest.mark.parametrize(
    ("used", "limit", "status"),
    [
        (0, 100, "ok"),
        (79.9, 100, "ok"),
        (80, 100, "warn"),
        (99, 100, "warn"),
        (100, 100, "over"),
        (150, 100, "over"),
        (None, 100, "unknown"),
        (5, None, "ok"),
        (5, 0, "unknown"),
    ],
)
def test_status_thresholds(used, limit, status):
    assert status_of(used, limit) == status


def test_fraction():
    assert fraction_of(1, 3) == 0.3333
    assert fraction_of(150, 100) == 1.5  # not capped, so "over" is visible
    assert fraction_of(None, 10) is None and fraction_of(1, None) is None


def budget_plugins() -> dict[str, Any]:
    return {
        "llm_a": {"module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
                  "limits": [{"metric": "requests", "window": "day", "limit": 2},
                             {"metric": "tokens", "window": "month", "limit": 100}],
                  "settings": {"default_model": "a"}},
        "llm_off": {"module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 20, "enabled": False,
                    "limits": [{"metric": "requests", "window": "day", "limit": 5}]},
        "llm_free": {"module": f"{F}:FakeLLM", "kind": "llm", "group": "llm_pool", "priority": 30,
                     "settings": {"default_model": "b"}},
        "store_warn": {"module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 10,
                       "settings": {"capacity_bytes": 1000, "used_bytes": 850}},
        "store_over": {"module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 20,
                       "limits": [{"metric": "requests", "window": "month", "limit": 10}],
                       "settings": {"capacity_bytes": 1000, "used_bytes": 1200}},
        "sqldb": {"module": f"{F}:FakeSQLDB", "kind": "db", "group": "databases", "priority": 10,
                  "limits": [{"metric": "rows_read", "window": "month", "limit": 500000000},
                             {"metric": "bytes", "window": "total", "limit": 4096}]},
        "vm": {"module": f"{F}:FakePowerVM", "kind": "compute", "group": "oracle", "priority": 10,
               "limits": [{"metric": "minutes", "window": "month", "limit": 2000}]},
    }


def _find(items, provider, metric, window):
    return next(i for i in items if i["provider"] == provider and i["metric"] == metric and i["window"] == window)


def test_budget_requires_auth(hub):
    assert hub.client.get("/budget").status_code == 401


def test_budget_aggregates_every_source(make_hub):
    hub = make_hub(budget_plugins())
    for _ in range(2):
        assert hub.post("/llm/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    pinned = {"messages": [{"role": "user", "content": "hi"}], "provider": "llm_free", "allow_fallback": False}
    assert hub.post("/llm/chat", json=pinned).status_code == 200

    r = hub.get("/budget")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generated_at"].endswith("Z")
    items = body["items"]
    providers = {i["provider"] for i in items}
    assert "llm_off" not in providers  # disabled plugins are skipped
    free = _find(items, "llm_free", "requests", "day")  # no limits: router counters still shown
    assert free["used"] == 1.0 and free["limit"] is None and free["status"] == "ok"

    req = _find(items, "llm_a", "requests", "day")
    assert req == {**req, "used": 2.0, "limit": 2.0, "fraction": 1.0, "status": "over", "source": "router",
                   "kind": "llm", "group": "llm_pool"}
    assert req["reset_at"].endswith("Z")
    tokens = _find(items, "llm_a", "tokens", "month")
    assert tokens["used"] == 10.0 and tokens["status"] == "ok"

    warn = _find(items, "store_warn", "bytes", "total")
    assert warn["source"] == "provider" and warn["status"] == "warn" and warn["fraction"] == 0.85
    over = _find(items, "store_over", "bytes", "total")
    assert over["status"] == "over" and over["fraction"] == 1.2
    assert _find(items, "store_over", "requests", "month")["source"] == "router"

    db_bytes = _find(items, "sqldb", "bytes", "total")
    assert db_bytes["source"] == "provider" and db_bytes["used"] == 1024.0 and db_bytes["status"] == "ok"
    assert sum(1 for i in items if i["provider"] == "sqldb" and i["metric"] == "bytes") == 1  # provider wins
    rows = _find(items, "sqldb", "rows_read", "month")
    assert rows["used"] is None and rows["status"] == "unknown" and rows["source"] == "config"

    vm = _find(items, "vm", "minutes", "month")
    assert vm == {**vm, "used": None, "limit": 2000.0, "fraction": None, "status": "unknown", "source": "config"}

    summary = body["summary"]
    assert set(summary) == {"ok", "warn", "over", "unknown"}
    assert sum(summary.values()) == len(items)
    assert summary["over"] == sum(1 for i in items if i["status"] == "over") >= 2
    assert summary["warn"] >= 1 and summary["unknown"] >= 2


def test_budget_tolerates_failing_provider(make_hub):
    plugins = budget_plugins()
    plugins["broken"] = {"module": f"{F}:FakeSQLDB", "kind": "db", "group": "databases", "priority": 50,
                         "settings": {"capacity_bytes": "not-a-number"},
                         "limits": [{"metric": "bytes", "window": "total", "limit": 10}]}
    hub = make_hub(plugins)
    r = hub.get("/budget")
    assert r.status_code == 200
    broken = _find(r.json()["items"], "broken", "bytes", "total")
    assert broken["source"] == "config" and broken["status"] == "unknown"
