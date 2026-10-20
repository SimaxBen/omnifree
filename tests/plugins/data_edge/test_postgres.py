from __future__ import annotations

import pytest

from core.interfaces import NotConfigured, ProviderError, ProviderKind, QuotaLimit
from plugins.databases._pg import affected_from_status
from plugins.databases.neon import NeonAdapter
from plugins.databases.supabase import SupabaseAdapter

from .helpers import Routes, body, install_fake_asyncpg, make

NEON_URL = "postgresql://user:pw@ep-cool-darkness-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
BYTES = (QuotaLimit(metric="bytes", window="total", limit=536870912),)


def test_affected_from_status() -> None:
    assert affected_from_status("INSERT 0 3") == 3
    assert affected_from_status("UPDATE 2") == 2
    assert affected_from_status("CREATE TABLE") is None
    assert affected_from_status(None) is None


# --------------------------------------------------------------------------- Neon (HTTP)


def neon_http(routes: Routes, result: dict) -> None:
    routes.add("POST", r"https://api\.us-east-2\.aws\.neon\.tech/sql$", {"results": [result]})


async def test_neon_http_query_parses_text_output_and_is_read_only() -> None:
    routes = Routes()
    neon_http(routes, {
        "command": "SELECT", "rowCount": 3,
        "fields": [{"name": "id", "dataTypeID": 23}, {"name": "ok", "dataTypeID": 16},
                   {"name": "meta", "dataTypeID": 3802}, {"name": "price", "dataTypeID": 1700}],
        "rows": [["1", "t", '{"a": 1}', "9.50"], ["2", "f", None, "3"], ["3", "t", None, "1"]],
    })
    neon = make(NeonAdapter, ProviderKind.DB, routes, secrets={"database_url": NEON_URL})
    result = await neon.query("select * from items where id > $1", [0], limit=2)
    assert result.columns == ["id", "ok", "meta", "price"]
    assert result.rows == [[1, True, {"a": 1}, "9.50"], [2, False, None, 3]]
    assert result.truncated is True and result.row_count == 2
    request = routes.requests[0]
    assert request.headers["Neon-Connection-String"] == NEON_URL
    assert request.headers["Neon-Batch-Read-Only"] == "true"
    assert body(request) == {"queries": [{"query": "select * from items where id > $1", "params": ["0"]}]}
    await neon.close()


async def test_neon_execute_reports_affected_rows_and_rejects_named_params() -> None:
    routes = Routes()
    neon_http(routes, {"command": "UPDATE", "rowCount": 4, "fields": [], "rows": []})
    neon = make(NeonAdapter, ProviderKind.DB, routes, secrets={"database_url": NEON_URL})
    result = await neon.execute("update items set ok = $1", [True])
    assert result.affected_rows == 4
    assert "Neon-Batch-Read-Only" not in routes.requests[0].headers
    assert body(routes.requests[0])["queries"][0]["params"] == ["true"]
    with pytest.raises(ProviderError) as err:
        await neon.execute("update items set ok = :ok", {"ok": True})
    assert err.value.status_code == 400


async def test_neon_list_and_describe_tables() -> None:
    routes = Routes()

    def reply(request):
        sql = body(request)["queries"][0]["query"]
        if "pg_attribute" in sql:
            return {"results": [{"fields": [{"name": "attname", "dataTypeID": 19}, {"name": "t", "dataTypeID": 25},
                                            {"name": "n", "dataTypeID": 16}, {"name": "pk", "dataTypeID": 16},
                                            {"name": "d", "dataTypeID": 25}],
                                 "rows": [["id", "integer", "f", "t", "nextval('s')"], ["name", "text", "t", "f", None]]}]}
        if "c.relname = $2" in sql:
            assert body(request)["queries"][0]["params"] == ["public", "users"]
            return {"results": [{"fields": [{"name": "a", "dataTypeID": 19}] * 3 + [{"name": "r", "dataTypeID": 20},
                                                                                    {"name": "s", "dataTypeID": 20}],
                                 "rows": [["public", "users", "r", "10", "8192"]]}]}
        return {"results": [{"fields": [{"name": "a", "dataTypeID": 19}] * 3 + [{"name": "r", "dataTypeID": 20},
                                                                                {"name": "s", "dataTypeID": 20}],
                             "rows": [["public", "users", "r", "-1", "8192"], ["public", "v_users", "v", "0", "0"]]}]}

    routes.add("POST", r"/sql$", reply)
    neon = make(NeonAdapter, ProviderKind.DB, routes, secrets={"database_url": NEON_URL})
    tables = await neon.list_tables()
    assert [(t.name, t.kind, t.row_count) for t in tables] == [("users", "table", None), ("v_users", "view", 0)]
    info = await neon.describe("users")
    assert info.row_count == 10 and info.size_bytes == 8192
    assert info.columns[0].primary_key and not info.columns[0].nullable
    assert info.columns[1].nullable and info.columns[1].default is None


async def test_neon_usage_prefers_api_then_sql() -> None:
    routes = Routes()
    routes.add("GET", r"console\.neon\.tech/api/v2/projects/proj-1$",
               {"project": {"synthetic_storage_size": 1234, "compute_time_seconds": 99}})
    neon = make(NeonAdapter, ProviderKind.DB, routes, secrets={"database_url": NEON_URL, "api_key": "k"},
                settings={"project_id": "proj-1"}, limits=BYTES)
    usage = await neon.usage()
    assert usage.used == 1234 and usage.limit == 536870912 and usage.source == "provider"
    assert usage.extra["compute_time_seconds"] == 99
    assert routes.requests[0].headers["Authorization"] == "Bearer k"

    routes2 = Routes()
    neon_http(routes2, {"fields": [{"name": "s", "dataTypeID": 20}], "rows": [["4096"]]})
    neon2 = make(NeonAdapter, ProviderKind.DB, routes2, secrets={"database_url": NEON_URL}, limits=BYTES)
    assert (await neon2.usage()).used == 4096


async def test_neon_missing_secret_and_health() -> None:
    neon = make(NeonAdapter, ProviderKind.DB, Routes())
    with pytest.raises(NotConfigured):
        await neon.query("select 1")
    status = await neon.health()
    assert status.ok is False and "NotConfigured" in status.detail


async def test_neon_asyncpg_driver(monkeypatch) -> None:
    db = install_fake_asyncpg(monkeypatch)
    db.responses["select id"] = (["id"], [(1,), (2,), (3,)], "SELECT 3")
    neon = make(NeonAdapter, ProviderKind.DB, None, secrets={"database_url": NEON_URL},
                settings={"driver": "asyncpg"})
    result = await neon.query("select id from t", limit=2)
    assert result.rows == [[1], [2]] and result.truncated
    assert db.executed[-1][2] is True  # read-only transaction
    await neon.close()
    assert db.closed


# --------------------------------------------------------------------------- Supabase


OPENAPI = {
    "definitions": {
        "todos": {
            "required": ["id", "title"],
            "properties": {
                "id": {"format": "bigint", "type": "integer", "description": "Note:\nThis is a Primary Key.<pk/>"},
                "title": {"format": "text", "type": "string"},
                "done": {"format": "boolean", "type": "boolean", "default": False},
            },
        }
    }
}


def supabase(routes: Routes | None, **secrets) -> SupabaseAdapter:
    return make(SupabaseAdapter, ProviderKind.DB, routes,
                settings={"project_url": "https://abc.supabase.co"},
                secrets={"service_role_key": "srk", **secrets})


async def test_supabase_postgrest_introspection() -> None:
    routes = Routes()
    routes.add("GET", r"/rest/v1/$", OPENAPI)
    import httpx
    routes.add("HEAD", r"/rest/v1/todos$", lambda r: httpx.Response(206, headers={"content-range": "0-0/42"}))
    sb = supabase(routes)
    tables = await sb.list_tables()
    assert [t.name for t in tables] == ["todos"]
    info = await sb.describe("todos")
    assert info.row_count == 42
    cols = {c.name: c for c in info.columns}
    assert cols["id"].primary_key and not cols["id"].nullable
    assert cols["done"].nullable and cols["done"].default == "False"
    assert routes.requests[0].headers["apikey"] == "srk"
    assert routes.requests[0].headers["Authorization"] == "Bearer srk"
    assert routes.requests[0].headers["Accept-Profile"] == "public"
    assert (await sb.health()).ok


async def test_supabase_postgrest_statements() -> None:
    routes = Routes()
    routes.add("GET", r"/rest/v1/todos\?select=id,title", [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}])
    routes.add("PATCH", r"/rest/v1/todos\?id=eq\.1", lambda r: [{"id": 1, **body(r)}])
    sb = supabase(routes)
    result = await sb.query("GET /todos?select=id,title", limit=1)
    assert result.rows == [[1, "a"]] and result.truncated
    assert routes.requests[0].headers["Range"] == "0-1"
    written = await sb.execute('PATCH /todos?id=eq.1 {"done": true}')
    assert written.affected_rows == 1 and written.rows == [[1, True]]
    assert routes.requests[1].headers["Prefer"] == "return=representation"
    with pytest.raises(ProviderError):
        await sb.query("select * from todos")  # SQL without db_url
    with pytest.raises(ProviderError):
        await sb.query('POST /todos {"title": "x"}')
    with pytest.raises(NotConfigured):
        await sb.usage()


async def test_supabase_sql_via_asyncpg(monkeypatch) -> None:
    db = install_fake_asyncpg(monkeypatch)
    db.responses["insert into"] = (["id"], [(7,)], "INSERT 0 1")
    db.responses["pg_database_size"] = (["size"], [(2048,)], "SELECT 1")
    db.responses["select * from todos"] = (["id", "title"], [(1, "a")], "SELECT 1")
    sb = supabase(None, db_url="postgresql://postgres:pw@pooler.supabase.com:6543/postgres")
    sb.context.limits = (QuotaLimit(metric="bytes", window="total", limit=524288000),)
    result = await sb.execute("insert into todos(title) values ($1) returning id", ["x"])
    assert result.affected_rows == 1 and result.rows == [[7]]
    assert db.executed[-1] == ("insert into todos(title) values ($1) returning id", ("x",), False)
    rows = await sb.query("select * from todos")
    assert rows.rows == [[1, "a"]] and db.executed[-1][2] is True
    usage = await sb.usage()
    assert usage.used == 2048 and usage.limit == 524288000
    assert db.pools_created == 1
    await sb.close()
    assert db.closed


async def test_supabase_without_asyncpg_installed(monkeypatch) -> None:
    import sys
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    sb = supabase(None, db_url="postgresql://x")
    with pytest.raises(NotConfigured, match="asyncpg"):
        await sb.query("select 1")
