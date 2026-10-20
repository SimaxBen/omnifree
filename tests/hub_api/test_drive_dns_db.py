from __future__ import annotations

import pytest

from hub_api.routers.db import sql_write_reason
from tests.hub_api import fakes

# ------------------------------------------------------------------ drive


def test_upload_goes_to_backend_with_most_free_space(hub):
    r = hub.post("/drive/upload", files={"file": ("notes.txt", b"hello world", "text/plain")}, data={"prefix": "docs"})
    assert r.status_code == 201, r.text
    obj = r.json()
    assert obj["key"] == "docs/notes.txt"
    assert obj["backend"] == "store_big"
    assert fakes.STORES["store_big"]["docs/notes.txt"][0] == b"hello world"


def test_upload_pinned_provider_and_explicit_key(hub):
    r = hub.post("/drive/upload", files={"file": ("a.bin", b"\x00\x01", None)}, data={"key": "raw/a.bin", "provider": "store_small"})
    assert r.status_code == 201
    assert r.json()["backend"] == "store_small"


def test_upload_rejects_bad_keys_unknown_provider_and_oversize(hub):
    r = hub.post("/drive/upload", files={"file": ("x", b"1", None)}, data={"key": "../etc/passwd"})
    assert r.status_code == 400
    r = hub.post("/drive/upload", files={"file": ("x", b"1", None)}, data={"provider": "sqldb"})
    assert r.status_code == 400
    r = hub.post("/drive/upload", files={"file": ("x", b"1", None)}, data={"provider": "nope"})
    assert r.status_code == 404
    r = hub.post("/drive/upload", files={"file": ("big.bin", b"0" * 50_001, None)})
    assert r.status_code == 413


def test_list_search_download_delete_and_usage(hub):
    hub.post("/drive/upload", files={"file": ("Report-2026.pdf", b"%PDF-1.7 data", "application/pdf")}, data={"prefix": "docs"})
    hub.post("/drive/upload", files={"file": ("cat.jpg", b"jpeg", "image/jpeg")}, data={"provider": "store_small", "prefix": "pics"})

    listing = hub.get("/drive/files").json()
    assert {(o["key"], o["backend"]) for o in listing["items"]} == {
        ("docs/Report-2026.pdf", "store_big"),
        ("pics/cat.jpg", "store_small"),
    }
    assert listing["errors"] == {}
    assert [o["key"] for o in hub.get("/drive/files", params={"prefix": "pics/"}).json()["items"]] == ["pics/cat.jpg"]
    assert hub.get("/drive/files", params={"provider": "store_small"}).json()["count"] == 1

    hits = hub.get("/drive/search", params={"q": "report"}).json()
    assert [h["key"] for h in hits["items"]] == ["docs/Report-2026.pdf"]

    r = hub.get("/drive/files/store_big/docs/Report-2026.pdf")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.7 data"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-storage-backend"] == "store_big"
    # Untrusted stored content must never execute on the hub origin.
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"

    located = hub.get("/drive/locate/pics/cat.jpg").json()
    assert located["backend"] == "store_small"

    usage = hub.get("/drive/usage").json()
    by_name = {b["provider"]: b for b in usage["backends"]}
    assert by_name["store_small"]["used"] == 4 and by_name["store_small"]["limit"] == 1000
    assert by_name["store_big"]["source"] == "provider"
    assert usage["total"]["limit"] == 101000

    r = hub.delete("/drive/files/store_small/pics/cat.jpg")
    assert r.status_code == 200
    assert "pics/cat.jpg" not in fakes.STORES["store_small"]
    assert hub.get("/drive/files/store_small/pics/cat.jpg").status_code == 404


def test_presign_without_capability_is_501(hub):
    r = hub.get("/drive/presign/store_big/a.txt")
    assert r.status_code == 501
    assert r.json()["error"] == "capability_not_supported"


# ------------------------------------------------------------------ dns / network


def test_dns_status_toggle_and_query_log(hub):
    assert hub.get("/dns/status").json()["blocking_enabled"] is True
    sub = hub.state.bus.subscribe(["dns.*"])
    r = hub.post("/dns/blocking", json={"enabled": False, "duration_s": 300})
    sub.close()
    assert r.status_code == 200
    assert r.json()["blocking_enabled"] is False
    event = sub.queue.get_nowait()
    assert event.type == "dns.blocking_changed"
    assert event.data == {"enabled": False, "duration_s": 300}
    assert fakes.DNS_STATE["pihole"] == {"enabled": False, "duration": 300}

    log = hub.get("/dns/queries", params={"blocked_only": "true"}).json()
    assert [e["domain"] for e in log["items"]] == ["ads.example.com"]
    assert hub.get("/dns/queries").json()["count"] == 2


def test_dns_records_unsupported_and_wrong_kind(hub):
    assert hub.get("/dns/records").status_code == 501
    assert hub.get("/dns/status", params={"provider": "tailscale"}).status_code == 400


def test_tailscale_nodes(hub):
    nodes = hub.get("/dns/nodes").json()
    assert nodes["provider"] == "tailscale"
    assert nodes["online"] == 1 and nodes["total"] == 2
    assert nodes["items"][0]["tailscale_ip"] == "100.64.0.1"


def test_compute_instances_and_power(hub):
    data = hub.get("/compute/instances").json()
    assert set(data["providers"]) == {"tailscale", "vm"}
    sub = hub.state.bus.subscribe(["compute.*"])
    r = hub.post("/compute/vm/instances/i-1/start")
    sub.close()
    assert r.status_code == 200 and r.json()["state"] == "starting"
    assert sub.queue.get_nowait().data["instance_id"] == "i-1"
    assert fakes.POWER_LOG == [("vm", "start", "i-1")]
    assert hub.post("/compute/tailscale/instances/n1/stop").status_code == 501


# ------------------------------------------------------------------ db


@pytest.mark.parametrize(
    "statement",
    ["SELECT * FROM users", "select id, comment from posts where x = 'DELETE me'", "WITH t AS (SELECT 1) SELECT * FROM t;",
     "EXPLAIN SELECT 1", "PRAGMA table_info(users)", "SELECT \"update\" FROM t -- drop table x"],
)
def test_sql_guard_allows_reads(statement):
    assert sql_write_reason(statement) is None


@pytest.mark.parametrize(
    "statement",
    ["INSERT INTO t VALUES (1)", "DELETE FROM users", "SELECT 1; DROP TABLE users", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
     "SELECT * INTO backup FROM users", "PRAGMA journal_mode = WAL", "", "/* only a comment */"],
)
def test_sql_guard_refuses_writes(statement):
    assert sql_write_reason(statement) is not None


def test_db_explorer_flow(hub):
    providers = hub.get("/db/providers", params={"usage": "true"}).json()["providers"]
    assert providers[0]["name"] == "sqldb" and providers[0]["sql"] is True
    assert providers[0]["usage"]["used"] == 1024.0

    tables = hub.get("/db/sqldb/tables").json()["items"]
    assert [t["name"] for t in tables] == ["users", "notes"]
    cols = hub.get("/db/sqldb/tables/users").json()["columns"]
    assert cols[0]["primary_key"] is True
    assert hub.get("/db/sqldb/tables/missing").status_code == 404

    r = hub.post("/db/sqldb/query", json={"statement": "SELECT * FROM users", "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "read" and body["rows"] == [[1, "user1"], [2, "user2"]] and body["truncated"] is True

    r = hub.post("/db/sqldb/query", json={"statement": "DELETE FROM users"})
    assert r.status_code == 400
    assert "write=true" in r.json()["detail"]
    assert all(mode == "read" for _p, mode, _s in fakes.DB_LOG)

    r = hub.post("/db/sqldb/query", json={"statement": "DELETE FROM users", "write": True})
    assert r.status_code == 200 and r.json()["affected_rows"] == 1 and r.json()["mode"] == "write"
    assert fakes.DB_LOG[-1] == ("sqldb", "write", "DELETE FROM users")

    r = hub.post("/db/sqldb/query", json={"statement": "SELECT broken"})
    assert r.status_code == 400 and r.json()["error"] == "provider_rejected"
    assert hub.get("/db/sqldb/usage").json()["fraction_used"] == 0.25
