from __future__ import annotations

import httpx
import pytest

from core.interfaces import NotConfigured, ProviderAuthError, ProviderError, ProviderKind, QuotaExhausted, QuotaLimit
from plugins.databases.cloudant import CloudantAdapter
from plugins.databases.mongodb_atlas import MongoAtlasAdapter
from plugins.databases.turso import TursoAdapter, decode_value, encode_value
from plugins.databases.upstash_redis import UpstashRedisAdapter

from .helpers import Routes, body, install_fake_motor, make

GB = (QuotaLimit(metric="bytes", window="total", limit=1073741824),)


# --------------------------------------------------------------------------- Turso


def hrana_ok(result: dict) -> dict:
    return {"type": "ok", "response": {"type": "execute", "result": result}}


def turso(routes: Routes) -> TursoAdapter:
    return make(TursoAdapter, ProviderKind.DB, routes,
                secrets={"database_url": "libsql://db-org.turso.io", "auth_token": "tok"}, limits=GB)


def test_hrana_value_codec() -> None:
    assert encode_value(5) == {"type": "integer", "value": "5"}
    assert encode_value(True) == {"type": "integer", "value": "1"}
    assert encode_value(None) == {"type": "null"}
    assert encode_value(1.5) == {"type": "float", "value": 1.5}
    assert encode_value(b"\x00\x01")["type"] == "blob"
    assert decode_value({"type": "integer", "value": "42"}) == 42
    assert decode_value({"type": "text", "value": "x"}) == "x"
    assert decode_value({"type": "null"}) is None


async def test_turso_query_checks_read_only_then_executes() -> None:
    routes = Routes()

    def pipeline(request):
        reqs = body(request)["requests"]
        assert reqs[-1] == {"type": "close"}
        first = reqs[0]
        if first["type"] == "describe":
            return {"results": [{"type": "ok", "response": {"type": "describe",
                                                            "result": {"is_readonly": "select" in first["sql"]}}},
                                {"type": "ok", "response": {"type": "close"}}]}
        assert first["stmt"]["named_args"] == [{"name": ":min", "value": {"type": "integer", "value": "1"}}]
        return {"results": [hrana_ok({"cols": [{"name": "id", "decltype": "INTEGER"}, {"name": "n"}],
                                      "rows": [[{"type": "integer", "value": "1"}, {"type": "text", "value": "a"}],
                                               [{"type": "integer", "value": "2"}, {"type": "null"}]],
                                      "affected_row_count": 0}),
                            {"type": "ok", "response": {"type": "close"}}]}

    routes.add("POST", r"https://db-org\.turso\.io/v2/pipeline$", pipeline)
    db = turso(routes)
    result = await db.query("select id, n from t where id >= :min", {"min": 1}, limit=1)
    assert result.columns == ["id", "n"] and result.rows == [[1, "a"]] and result.truncated
    assert routes.requests[0].headers["Authorization"] == "Bearer tok"
    with pytest.raises(ProviderError, match="read-only"):
        await db.query("delete from t")


async def test_turso_execute_errors_and_usage() -> None:
    routes = Routes()

    def pipeline(request):
        sql = body(request)["requests"][0]["stmt"]["sql"]
        if sql.startswith("bad"):
            return {"results": [{"type": "error", "error": {"message": "near bad: syntax error", "code": "SQLITE_ERROR"}}]}
        if "page_count" in sql:
            return {"results": [hrana_ok({"cols": [{"name": "s"}], "rows": [[{"type": "integer", "value": "8192"}]]})]}
        assert body(request)["requests"][0]["stmt"]["args"] == [{"type": "text", "value": "x"}]
        return {"results": [hrana_ok({"cols": [], "rows": [], "affected_row_count": 3})]}

    routes.add("POST", r"/v2/pipeline$", pipeline)
    db = turso(routes)
    assert (await db.execute("update t set n = ?", ["x"])).affected_rows == 3
    with pytest.raises(ProviderError, match="SQLITE_ERROR"):
        await db.execute("bad sql")
    usage = await db.usage()
    assert usage.used == 8192 and usage.limit == 1073741824 and usage.unit == "bytes"


async def test_turso_list_and_describe() -> None:
    routes = Routes()

    def pipeline(request):
        reqs = body(request)["requests"]
        sql = reqs[0]["stmt"]["sql"]
        if "sqlite_schema where type in" in sql:
            return {"results": [hrana_ok({"cols": [{"name": "name"}, {"name": "type"}], "rows": [
                [{"type": "text", "value": "users"}, {"type": "text", "value": "table"}],
                [{"type": "text", "value": "v"}, {"type": "text", "value": "view"}]]})]}
        if "count(*)" in sql:
            return {"results": [hrana_ok({"cols": [{"name": "c"}], "rows": [[{"type": "integer", "value": "5"}]]})]}
        assert reqs[1]["stmt"]["sql"] == 'pragma table_info("users")'
        return {"results": [
            hrana_ok({"cols": [{"name": "type"}], "rows": [[{"type": "text", "value": "table"}]]}),
            hrana_ok({"cols": [], "rows": [
                [{"type": "integer", "value": "0"}, {"type": "text", "value": "id"}, {"type": "text", "value": "INTEGER"},
                 {"type": "integer", "value": "1"}, {"type": "null"}, {"type": "integer", "value": "1"}],
                [{"type": "integer", "value": "1"}, {"type": "text", "value": "name"}, {"type": "text", "value": "TEXT"},
                 {"type": "integer", "value": "0"}, {"type": "text", "value": "'x'"}, {"type": "integer", "value": "0"}]]}),
        ]}

    routes.add("POST", r"/v2/pipeline$", pipeline)
    db = turso(routes)
    assert [(t.name, t.kind) for t in await db.list_tables()] == [("users", "table"), ("v", "view")]
    info = await db.describe("users")
    assert info.row_count == 5
    assert info.columns[0].primary_key and not info.columns[0].nullable
    assert info.columns[1].default == "'x'"


# --------------------------------------------------------------------------- MongoDB


async def test_mongo_query_execute_describe_usage(monkeypatch) -> None:
    mongo = install_fake_motor(monkeypatch)
    mongo.on("find", lambda c: {"cursor": {"firstBatch": [{"_id": "a1", "n": 1}, {"_id": "a2", "x": True},
                                                         {"_id": "a3"}][: c["limit"]]}, "ok": 1})
    mongo.on("insert", {"n": 2, "ok": 1})
    mongo.on("listCollections", lambda c: {"cursor": {"firstBatch": [{"name": "users", "type": "collection"},
                                                                    {"name": "system.views"},
                                                                    {"name": "active", "type": "view"}]}})
    mongo.on("count", {"n": 3, "ok": 1})
    mongo.on("aggregate", {"cursor": {"firstBatch": [{"_id": 1, "name": "a"}, {"_id": 2, "name": None}]}})
    mongo.on("collStats", {"storageSize": 100, "totalIndexSize": 20})
    mongo.on("dbStats", {"storageSize": 1000, "indexSize": 24, "dataSize": 500, "ok": 1})
    adapter = make(MongoAtlasAdapter, ProviderKind.DB, None, secrets={"uri": "mongodb+srv://u:p@c.mongodb.net"},
                   settings={"database": "app"}, limits=(QuotaLimit(metric="bytes", window="total", limit=536870912),))

    result = await adapter.query('{"find": "users", "filter": {}}', limit=2)
    assert result.columns == ["_id", "n", "x"]
    assert result.rows == [["a1", 1, None], ["a2", None, True]] and result.truncated
    assert mongo.commands[-1]["limit"] == 3 and mongo.commands[-1]["singleBatch"] is True

    with pytest.raises(ProviderError, match="not a read command"):
        await adapter.query('{"insert": "users", "documents": []}')
    with pytest.raises(ProviderError, match=r"\$out"):
        await adapter.query('{"aggregate": "users", "pipeline": [{"$out": "x"}]}')
    with pytest.raises(ProviderError):
        await adapter.query("not json")

    written = await adapter.execute('{"insert": "users", "documents": [{"a": 1}, {"a": 2}]}')
    assert written.affected_rows == 2

    tables = await adapter.list_tables()
    assert [(t.name, t.kind) for t in tables] == [("active", "view"), ("users", "collection")]
    info = await adapter.describe("users")
    assert info.row_count == 3 and info.size_bytes == 120
    cols = {c.name: c for c in info.columns}
    assert cols["_id"].primary_key and "null" in cols["name"].type

    usage = await adapter.usage()
    assert usage.used == 1024 and usage.limit == 536870912 and usage.extra["dataSize"] == 500
    await adapter.close()
    assert mongo.closed


async def test_mongo_errors(monkeypatch) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "motor", None)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", None)
    adapter = make(MongoAtlasAdapter, ProviderKind.DB, None, secrets={"uri": "mongodb://x"})
    with pytest.raises(NotConfigured, match="motor"):
        await adapter.list_tables()

    mongo = install_fake_motor(monkeypatch)

    class OperationFailure(Exception):
        code = 18

    def fail(_c):
        raise OperationFailure("auth failed")

    mongo.on("ping", fail)
    adapter = make(MongoAtlasAdapter, ProviderKind.DB, None, secrets={"uri": "mongodb://x"})
    with pytest.raises(ProviderAuthError):
        await adapter._command({"ping": 1})
    assert (await adapter.health()).ok is False


# --------------------------------------------------------------------------- Upstash Redis


def redis(routes: Routes) -> UpstashRedisAdapter:
    return make(UpstashRedisAdapter, ProviderKind.DB, routes,
                secrets={"rest_url": "https://eu1-x.upstash.io", "rest_token": "rt"},
                limits=(QuotaLimit(metric="bytes", window="total", limit=268435456),))


async def test_redis_commands_and_read_only_guard() -> None:
    routes = Routes()

    def reply(request):
        cmd = body(request)
        assert request.headers["Authorization"] == "Bearer rt"
        if cmd[0] == "HGETALL":
            return {"result": ["name", "Ada", "age", "36"]}
        if cmd[0] == "SET":
            assert cmd == ["SET", "user:1", "Ada Lovelace", "EX", "60"]
            return {"result": "OK"}
        if cmd[0] == "GET":
            return {"result": None}
        return httpx.Response(400, json={"error": "ERR unknown command"})

    routes.add("POST", r"upstash\.io$", reply)
    db = redis(routes)
    result = await db.query('["HGETALL", "user:1"]')
    assert result.columns == ["field", "value"] and result.rows == [["name", "Ada"], ["age", "36"]]
    written = await db.execute('SET user:1 "Ada Lovelace"', ["EX", 60])
    assert written.affected_rows == 1 and written.rows == [["OK"]]
    assert (await db.query("GET missing")).rows == [[None]]
    with pytest.raises(ProviderError, match="not a read command"):
        await db.query("DEL user:1")
    with pytest.raises(ProviderError, match="unknown command"):
        await db.execute("NOPE")


async def test_redis_keyspaces_usage_and_quota() -> None:
    routes = Routes()
    pages = {"0": ["100", ["user:1", "user:2", "lonely"]], "100": ["0", ["session:abc"]]}

    def reply(request):
        cmd = body(request)
        if cmd[0] == "SCAN":
            return {"result": pages[cmd[1]]}
        return {"result": "PONG"}

    routes.add("POST", r"upstash\.io$", reply)
    routes.add("POST", r"/pipeline$", [{"result": "# Memory\r\nused_memory:2048\r\n"}, {"result": 4}])
    db = redis(routes)
    tables = await db.list_tables()
    assert [(t.name, t.row_count, t.kind) for t in tables] == [
        ("(root)", 1, "keyspace"), ("session:*", 1, "keyspace"), ("user:*", 2, "keyspace")]
    info = await db.describe("(root)")
    assert info.row_count == 1
    usage = await db.usage()
    assert usage.used == 2048 and usage.extra["keys"] == 4 and usage.limit == 268435456
    assert (await db.health()).ok

    limited = Routes().add("POST", r"upstash\.io$",
                           {"error": "ERR max daily request limit exceeded. Limit: 10000"}, status=400)
    with pytest.raises(QuotaExhausted):
        await redis(limited).execute("PING")


# --------------------------------------------------------------------------- Cloudant


def cloudant_routes() -> Routes:
    routes = Routes()
    routes.add("POST", r"iam\.cloud\.ibm\.com/identity/token$",
               {"access_token": "iam-token", "expires_in": 3600, "expiration": 9999999999})
    return routes


async def test_cloudant_iam_and_operations() -> None:
    routes = cloudant_routes()
    routes.add("GET", r"/_all_dbs$", ["_replicator", "orders", "users"])
    routes.add("POST", r"/_dbs_info$", [
        {"key": "orders", "info": {"doc_count": 3, "sizes": {"external": 300, "file": 4000}}},
        {"key": "users", "info": {"doc_count": 1, "sizes": {"external": 100, "file": 1000}}}])
    routes.add("POST", r"/orders/_find$", lambda r: {"docs": [{"_id": str(i), "total": i} for i in range(body(r)["limit"])]})
    routes.add("POST", r"/orders$", {"ok": True, "id": "x", "rev": "1-a"}, status=201)
    routes.add("DELETE", r"/orders/x\?rev=1-a$", {"ok": True, "id": "x", "rev": "2-b"})
    routes.add("GET", r"/orders$", {"doc_count": 3, "sizes": {"external": 300}})
    routes.add("GET", r"/orders/_all_docs", {"rows": [{"id": "1", "doc": {"_id": "1", "_rev": "1-a", "total": 3}},
                                                      {"id": "_design/x", "doc": {"_id": "_design/x"}}]})
    db = make(CloudantAdapter, ProviderKind.DB, routes,
              secrets={"url": "https://acct.cloudantnosqldb.appdomain.cloud", "apikey": "k"}, limits=GB)

    tables = await db.list_tables()
    assert [(t.name, t.row_count, t.size_bytes) for t in tables] == [("orders", 3, 300), ("users", 1, 100)]
    iam = routes.find("POST", "identity/token")
    assert len(iam) == 1 and b"apikey=k" in iam[0].content
    assert routes.find("GET", "_all_dbs")[0].headers["Authorization"] == "Bearer iam-token"

    result = await db.query('{"db": "orders", "selector": {"total": {"$gt": 0}}}', limit=2)
    assert result.row_count == 2 and result.truncated
    written = await db.execute('{"db": "orders", "doc": {"_id": "x", "total": 1}}')
    assert written.affected_rows == 1
    deleted = await db.execute('{"db": "orders", "delete": "x", "rev": "1-a"}')
    assert deleted.affected_rows == 1
    with pytest.raises(ProviderError):
        await db.execute('{"db": "orders", "delete": "x"}')
    info = await db.describe("orders")
    assert info.row_count == 3 and [c.name for c in info.columns] == ["_id", "_rev", "total"]
    usage = await db.usage()
    assert usage.used == 5000 and usage.limit == 1073741824
    assert len(routes.find("POST", "identity/token")) == 1  # token cached


async def test_cloudant_refreshes_token_on_401() -> None:
    routes = cloudant_routes()
    calls = {"n": 0}

    def welcome(_request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"}) if calls["n"] == 1 else httpx.Response(200, json={"couchdb": "Welcome"})

    routes.add("GET", r"appdomain\.cloud/$", welcome)
    db = make(CloudantAdapter, ProviderKind.DB, routes, secrets={"url": "https://acct.cloudantnosqldb.appdomain.cloud/",
                                                               "apikey": "k"})
    assert (await db.health()).ok
    assert len(routes.find("POST", "identity/token")) == 2
