from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from core.interfaces import (
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderKind,
    QuotaExhausted,
    QuotaLimit,
)
from plugins._s3 import SigV4Signer
from plugins.cloudflare.d1 import D1Adapter
from plugins.cloudflare.r2 import R2StorageAdapter
from plugins.cloudflare.workers import CloudflareWorkersAdapter
from tests.plugins.clouds.helpers import Recorder, jresp

# ----------------------------------------------------------------------------- SigV4


def test_sigv4_matches_aws_documented_vectors() -> None:
    signer = SigV4Signer("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1")
    now = datetime(2013, 5, 24, tzinfo=timezone.utc)
    headers = signer.sign_headers(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt", headers={"range": "bytes=0-9"}, now=now
    )
    assert headers["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
    url = signer.presign_url("GET", "https://examplebucket.s3.amazonaws.com/test.txt", expires_s=86400, now=now)
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_presign_rejects_bad_expiry() -> None:
    signer = SigV4Signer("a", "b", "auto")
    with pytest.raises(ValueError):
        signer.presign_url("GET", "https://x/y", expires_s=0)


# ----------------------------------------------------------------------------- R2

R2_SECRETS = {"account_id": "acc123", "access_key_id": "AKID", "secret_access_key": "SECRET"}

LIST_PAGE_1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>omnifree</Name><IsTruncated>true</IsTruncated><NextContinuationToken>tok2</NextContinuationToken>
  <Contents><Key>docs/a.txt</Key><Size>10</Size><ETag>"e1"</ETag><LastModified>2026-01-01T00:00:00.000Z</LastModified></Contents>
</ListBucketResult>"""
LIST_PAGE_2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>omnifree</Name><IsTruncated>false</IsTruncated>
  <Contents><Key>docs/b c.txt</Key><Size>32</Size><ETag>"e2"</ETag></Contents>
</ListBucketResult>"""


class FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acc123.r2.cloudflarestorage.com"
        auth = request.headers["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/") and "/auto/s3/aws4_request" in auth
        assert request.headers["x-amz-content-sha256"]
        path = request.url.path
        assert path.startswith("/omnifree")
        key = httpx.URL(str(request.url)).path[len("/omnifree/"):]
        if request.method == "PUT":
            meta = {k: v for k, v in request.headers.items() if k.startswith("x-amz-meta-")}
            meta["content-type"] = request.headers["content-type"]
            self.objects[key] = (request.content, meta)
            return httpx.Response(200, headers={"etag": '"abc"'})
        if request.method == "GET" and request.url.params.get("list-type") == "2":
            body = LIST_PAGE_2 if request.url.params.get("continuation-token") == "tok2" else LIST_PAGE_1
            return httpx.Response(200, content=body)
        if request.method in ("GET", "HEAD") and key:
            if key not in self.objects:
                return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
            data, meta = self.objects[key]
            headers = {"content-length": str(len(data)), "content-type": meta["content-type"], "etag": '"abc"',
                       "last-modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                       **{k: v for k, v in meta.items() if k.startswith("x-amz-meta-")}}
            return httpx.Response(200, content=data if request.method == "GET" else b"", headers=headers)
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(400)


async def test_r2_roundtrip(make_ctx) -> None:
    fake = FakeR2()
    rec = Recorder(fake)
    adapter = R2StorageAdapter(make_ctx("cloudflare_r2", ProviderKind.STORAGE, settings={"bucket": "omnifree",
                               "capacity_bytes": 1000}, secrets=R2_SECRETS, handler=rec))
    stored = await adapter.put("photos/2026/cat pic.jpg", b"meow", "image/jpeg", {"Origin": "phone"})
    assert stored.size == 4 and stored.etag == "abc" and stored.backend == "cloudflare_r2"
    assert rec.requests[0].url.raw_path == b"/omnifree/photos/2026/cat%20pic.jpg"
    assert await adapter.get("photos/2026/cat pic.jpg") == b"meow"
    st = await adapter.stat("photos/2026/cat pic.jpg")
    assert st is not None and st.size == 4 and st.content_type == "image/jpeg" and st.metadata == {"origin": "phone"}
    assert await adapter.stat("missing") is None
    with pytest.raises(ProviderError) as exc:
        await adapter.get("missing")
    assert exc.value.status_code == 404
    listed = await adapter.list("docs/")
    assert [o.key for o in listed] == ["docs/a.txt", "docs/b c.txt"]
    assert listed[0].etag == "e1" and listed[0].last_modified is not None
    quota = await adapter.quota()
    assert quota.used == 42 and quota.limit == 1000 and quota.source == "provider" and quota.unit == "bytes"
    await adapter.delete("photos/2026/cat pic.jpg")
    assert "photos/2026/cat pic.jpg" not in fake.objects
    assert (await adapter.health()).ok
    url = await adapter.presign("docs/a.txt", 600)
    assert url.startswith("https://acc123.r2.cloudflarestorage.com/omnifree/docs/a.txt?X-Amz-Algorithm=")
    assert "X-Amz-Expires=600" in url
    await adapter.close()


async def test_r2_quota_is_cached(make_ctx) -> None:
    rec = Recorder(FakeR2())
    adapter = R2StorageAdapter(make_ctx("cloudflare_r2", ProviderKind.STORAGE, settings={"bucket": "omnifree"},
                               secrets=R2_SECRETS, handler=rec,
                               limits=[QuotaLimit(metric="bytes", window="total", limit=500)]))
    first = await adapter.quota()
    count = len(rec.requests)
    second = await adapter.quota()
    assert len(rec.requests) == count and first.used == second.used and second.limit == 500


async def test_r2_errors(make_ctx) -> None:
    adapter = R2StorageAdapter(make_ctx("cloudflare_r2", ProviderKind.STORAGE, settings={"bucket": "b"},
                               secrets={"account_id": "acc123"}))
    with pytest.raises(NotConfigured):
        await adapter.get("x")
    rec = Recorder(lambda r: httpx.Response(429, headers={"retry-after": "7"}))
    adapter = R2StorageAdapter(make_ctx("cloudflare_r2", ProviderKind.STORAGE, settings={"bucket": "omnifree"},
                               secrets=R2_SECRETS, handler=rec))
    with pytest.raises(QuotaExhausted) as exc:
        await adapter.put("k", b"v")
    assert exc.value.retry_after == 7
    health = await adapter.health()
    assert not health.ok


# ----------------------------------------------------------------------------- D1

CF_SECRETS = {"account_id": "acc123", "api_token": "cf-token"}


def d1_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer cf-token"
    base = "/client/v4/accounts/acc123/d1/database/db-1"
    if request.method == "GET" and request.url.path == base:
        return jresp({"success": True, "errors": [], "result": {"uuid": "db-1", "file_size": 12288, "num_tables": 2}})
    assert request.url.path == base + "/query"
    body = json.loads(request.content)
    sql = body["sql"]
    if sql.startswith("SELECT name, type FROM sqlite_master"):
        rows = [{"name": "notes", "type": "table"}, {"name": "v_notes", "type": "view"}]
    elif sql.startswith("PRAGMA table_info"):
        assert sql == 'PRAGMA table_info("notes")'
        rows = [{"cid": 0, "name": "id", "type": "INTEGER", "notnull": 0, "dflt_value": None, "pk": 1},
                {"cid": 1, "name": "body", "type": "TEXT", "notnull": 1, "dflt_value": "''", "pk": 0}]
    elif sql.startswith("SELECT COUNT(*)"):
        rows = [{"n": 3}]
    elif sql.startswith("SELECT * FROM notes"):
        assert body.get("params") == [1]
        rows = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}, {"id": 3, "body": "c"}]
    elif sql.startswith("INSERT"):
        return jresp({"success": True, "errors": [], "result": [
            {"results": [], "success": True, "meta": {"changes": 1, "duration": 0.4, "last_row_id": 4}}]})
    elif sql == "SELECT 1":
        rows = [{"1": 1}]
    elif sql.startswith("SELECT boom"):
        return jresp({"success": False, "errors": [{"code": 7500, "message": "no such column: boom"}],
                      "result": None}, status=400)
    elif sql.startswith("SELECT heavy"):
        return jresp({"success": False, "errors": [{"code": 7429, "message": "D1 DB exceeded daily rows read limit"}],
                      "result": None}, status=429 if "429" in sql else 400)
    else:
        raise AssertionError(sql)
    return jresp({"success": True, "errors": [], "result": [{"results": rows, "success": True,
                                                              "meta": {"duration": 0.2, "rows_read": len(rows)}}]})


async def test_d1_operations(make_ctx) -> None:
    rec = Recorder(d1_handler)
    adapter = D1Adapter(make_ctx("cloudflare_d1", ProviderKind.DB, settings={"database_id": "db-1"},
                        secrets=CF_SECRETS, handler=rec,
                        limits=[QuotaLimit(metric="bytes", window="total", limit=5e9)]))
    tables = await adapter.list_tables()
    assert [(t.name, t.kind) for t in tables] == [("notes", "table"), ("v_notes", "view")]
    info = await adapter.describe("notes")
    assert info.row_count == 3 and info.columns[0].primary_key and not info.columns[1].nullable
    assert info.columns[1].default == "''"
    res = await adapter.query("SELECT * FROM notes WHERE id >= ?", [1], limit=2)
    assert res.columns == ["id", "body"] and res.rows == [[1, "a"], [2, "b"]] and res.truncated
    ins = await adapter.execute("INSERT INTO notes(body) VALUES (?)", ["x"])
    assert ins.affected_rows == 1
    usage = await adapter.usage()
    assert usage.used == 12288 and usage.limit == 5e9 and usage.extra["num_tables"] == 2
    assert (await adapter.health()).ok
    with pytest.raises(ProviderError) as exc:
        await adapter.query("SELECT * FROM notes", {"id": 1})
    assert exc.value.status_code == 400


async def test_d1_error_mapping(make_ctx) -> None:
    adapter = D1Adapter(make_ctx("cloudflare_d1", ProviderKind.DB, settings={"database_id": "db-1"},
                        secrets=CF_SECRETS, handler=Recorder(d1_handler)))
    with pytest.raises(ProviderError) as exc:
        await adapter.query("SELECT boom")
    assert "no such column" in str(exc.value) and not isinstance(exc.value, QuotaExhausted)
    with pytest.raises(QuotaExhausted):
        await adapter.query("SELECT heavy")
    with pytest.raises(QuotaExhausted):
        await adapter.query("SELECT heavy 429")
    missing = D1Adapter(make_ctx("cloudflare_d1", ProviderKind.DB, secrets=CF_SECRETS))
    with pytest.raises(NotConfigured):
        await missing.query("SELECT 1")
    denied = D1Adapter(make_ctx("cloudflare_d1", ProviderKind.DB, settings={"database_id": "db-1"},
                       secrets=CF_SECRETS, handler=Recorder(lambda r: jresp({"success": False}, status=403))))
    with pytest.raises(ProviderAuthError):
        await denied.query("SELECT 1")


# ----------------------------------------------------------------------------- Workers


def workers_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    base = "/client/v4/accounts/acc123/workers"
    if path == base + "/scripts" and request.method == "GET":
        return jresp({"success": True, "result": [
            {"id": "omnifree-edge", "created_on": "2026-01-01T00:00:00Z", "modified_on": "2026-02-01T00:00:00Z",
             "etag": "e", "usage_model": "standard"},
            {"id": "other", "created_on": "2026-01-01T00:00:00Z"},
        ]})
    if path == base + "/subdomain":
        return jresp({"success": True, "result": {"subdomain": "simax"}})
    if path == base + "/scripts/omnifree-edge" and request.method == "PUT":
        ctype = request.headers["content-type"]
        assert ctype.startswith("multipart/form-data")
        body = request.content
        assert b'"main_module": "worker.js"' in body and b"export default" in body
        assert b'"compatibility_date": "2026-10-01"' in body
        return jresp({"success": True, "result": {"id": "omnifree-edge", "etag": "new"}})
    if path == base + "/scripts/omnifree-edge/subdomain":
        assert json.loads(request.content) == {"enabled": True}
        return jresp({"success": True, "result": {"enabled": True}})
    if path == base + "/scripts/old" and request.method == "DELETE":
        return jresp({"success": True, "result": None})
    if path == "/client/v4/graphql":
        payload = json.loads(request.content)
        assert payload["variables"]["accountTag"] == "acc123"
        return jresp({"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [
            {"sum": {"requests": 120, "errors": 0}, "dimensions": {"scriptName": "omnifree-edge"}},
            {"sum": {"requests": 5, "errors": 1}, "dimensions": {"scriptName": "other"}},
        ]}]}}, "errors": None})
    raise AssertionError(f"{request.method} {path}")


async def test_workers(make_ctx) -> None:
    adapter = CloudflareWorkersAdapter(make_ctx(
        "cloudflare_workers", ProviderKind.COMPUTE,
        settings={"scripts": ["omnifree-edge"], "compatibility_date": "2026-10-01"},
        secrets=CF_SECRETS, handler=Recorder(workers_handler),
        limits=[QuotaLimit(metric="requests", window="day", limit=100000)]))
    instances = await adapter.list_instances()
    assert [i.id for i in instances] == ["omnifree-edge", "other"]
    assert instances[0].url == "https://omnifree-edge.simax.workers.dev"
    assert instances[0].state == "running" and instances[0].tags == {"managed": "true"} and instances[1].tags == {}
    assert (await adapter.status("other")).name == "other"
    with pytest.raises(ProviderError) as exc:
        await adapter.status("nope")
    assert exc.value.status_code == 404
    deployed = await adapter.deploy_script("omnifree-edge", "export default { fetch() { return new Response('hi') } }")
    assert deployed.meta["etag"] == "new" and deployed.url.endswith(".simax.workers.dev")
    await adapter.delete_script("old")
    quota = await adapter.quota()
    assert quota.used == 125 and quota.limit == 100000 and quota.window == "day"
    assert quota.extra["per_script"]["omnifree-edge"] == 120
    assert not adapter.supports("power")
    assert (await adapter.health()).ok
