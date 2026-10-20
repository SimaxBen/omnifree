from __future__ import annotations

import base64
import hashlib
import json
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from core.interfaces import NotConfigured, ProviderAuthError, ProviderError, ProviderKind, QuotaLimit
from plugins.gcp._auth import parse_service_account
from plugins.gcp.cloud_run import CloudRunAdapter
from plugins.gcp.compute_engine import GCEComputeAdapter
from plugins.gcp.firestore import FirestoreAdapter, decode_value, encode_value
from plugins.gcp.gcs import GCSStorageAdapter
from tests.plugins.clouds.helpers import Recorder, jresp, token_response


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def with_token(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        tok = token_response(request)
        if tok is not None:
            return tok
        assert request.headers["authorization"] == "Bearer ya29.test"
        return handler(request)
    return wrapped


# ----------------------------------------------------------------------------- auth


@pytest.mark.parametrize("google_auth_available", [True, False])
async def test_token_exchange_signs_valid_jwt(make_ctx, service_account, rsa_key, monkeypatch,
                                              google_auth_available) -> None:
    if not google_auth_available:
        monkeypatch.setitem(sys.modules, "google.auth", None)
    else:
        pytest.importorskip("google.auth")
    rec = Recorder(with_token(lambda r: jresp({"services": []})))
    adapter = CloudRunAdapter(make_ctx("gcp_cloud_run", ProviderKind.COMPUTE, settings={"region": "europe-west1"},
                              secrets={"service_account_json": json.dumps(service_account)}, handler=rec))
    await adapter.list_instances()
    await adapter.list_instances()
    token_calls = [r for r in rec.requests if r.url.host == "oauth2.googleapis.com"]
    assert len(token_calls) == 1  # cached
    form = parse_qs(token_calls[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header_b64, claims_b64, sig_b64 = form["assertion"][0].split(".")
    header = json.loads(_b64url_decode(header_b64))
    claims = json.loads(_b64url_decode(claims_b64))
    assert header["alg"] == "RS256" and header["kid"] == "kid123"
    assert claims["iss"] == service_account["client_email"] and claims["aud"] == service_account["token_uri"]
    assert claims["scope"] == "https://www.googleapis.com/auth/cloud-platform"
    assert claims["exp"] - claims["iat"] == 3600
    rsa_key.public_key().verify(_b64url_decode(sig_b64), f"{header_b64}.{claims_b64}".encode(),
                                padding.PKCS1v15(), hashes.SHA256())
    # project id defaults to the service account's
    assert rec.requests[-1].url.path == "/v2/projects/omni-proj/locations/europe-west1/services"


def test_service_account_parsing(service_account) -> None:
    raw = json.dumps(service_account)
    assert parse_service_account(raw, "p")["client_email"] == service_account["client_email"]
    assert parse_service_account(base64.b64encode(raw.encode()).decode(), "p")["project_id"] == "omni-proj"
    with pytest.raises(NotConfigured):
        parse_service_account("not json!", "p")
    with pytest.raises(NotConfigured):
        parse_service_account(json.dumps({"client_email": "x"}), "p")


async def test_token_rejected_maps_to_auth_error(make_ctx, service_account) -> None:
    rec = Recorder(lambda r: jresp({"error": "invalid_grant"}, status=400))
    adapter = CloudRunAdapter(make_ctx("gcp_cloud_run", ProviderKind.COMPUTE, settings={"region": "r"},
                              secrets={"service_account_json": json.dumps(service_account)}, handler=rec))
    with pytest.raises(ProviderAuthError):
        await adapter.list_instances()
    assert not (await adapter.health()).ok
    missing = CloudRunAdapter(make_ctx("gcp_cloud_run", ProviderKind.COMPUTE, settings={"region": "r"}))
    with pytest.raises(NotConfigured):
        await missing.list_instances()


# ----------------------------------------------------------------------------- Cloud Run


async def test_cloud_run(make_ctx, service_account) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        base = "/v2/projects/proj-x/locations/europe-west1/services"
        if request.url.path == base:
            if request.url.params.get("pageToken") == "p2":
                return jresp({"services": [{"name": "projects/proj-x/locations/europe-west1/services/broken",
                                            "terminalCondition": {"state": "CONDITION_FAILED", "message": "bad image"}}]})
            return jresp({"services": [{"name": "projects/proj-x/locations/europe-west1/services/omnifree-hub",
                                        "uri": "https://omnifree-hub-xyz.a.run.app",
                                        "terminalCondition": {"type": "Ready", "state": "CONDITION_SUCCEEDED"},
                                        "latestReadyRevision": "rev-1"}], "nextPageToken": "p2"})
        if request.url.path == base + "/omnifree-hub":
            return jresp({"name": f"{base[4:]}/omnifree-hub", "reconciling": True,
                          "terminalCondition": {"state": "CONDITION_SUCCEEDED"}})
        return jresp({"error": {"code": 404}}, status=404)

    adapter = CloudRunAdapter(make_ctx("gcp_cloud_run", ProviderKind.COMPUTE,
                              settings={"region": "europe-west1", "project_id": "proj-x", "services": ["omnifree-hub"]},
                              secrets={"service_account_json": json.dumps(service_account)},
                              handler=Recorder(with_token(handler))))
    instances = await adapter.list_instances()
    assert [(i.id, i.state) for i in instances] == [("omnifree-hub", "running"), ("broken", "error")]
    assert instances[0].url == "https://omnifree-hub-xyz.a.run.app" and instances[0].tags == {"managed": "true"}
    assert instances[1].meta["condition"] == "bad image"
    assert (await adapter.status("omnifree-hub")).state == "deploying"
    with pytest.raises(ProviderError) as exc:
        await adapter.status("nope")
    assert exc.value.status_code == 404
    assert not adapter.supports("power")


# ----------------------------------------------------------------------------- Compute Engine


async def test_compute_engine(make_ctx, service_account) -> None:
    state = {"status": "TERMINATED"}

    def handler(request: httpx.Request) -> httpx.Response:
        base = "/compute/v1/projects/omni-proj/zones/us-central1-a"
        path = request.url.path
        item = {"id": "123", "name": "gcp-monitor", "status": state["status"],
                "machineType": f"https://compute.googleapis.com{base}/machineTypes/e2-micro",
                "zone": f"https://compute.googleapis.com{base}",
                "networkInterfaces": [{"networkIP": "10.128.0.2", "accessConfigs": [{"natIP": "34.1.2.3"}]}],
                "labels": {"role": "monitor"}}
        if path == base + "/instances":
            return jresp({"items": [item, {**item, "id": "999", "name": "unmanaged"}]})
        if path == base + "/instances/gcp-monitor":
            return jresp(item)
        if path == base + "/instances/gcp-monitor/start":
            state["status"] = "STAGING"
            return jresp({"name": "op-1", "status": "RUNNING"})
        if path == base + "/operations/op-1/wait":
            state["status"] = "RUNNING"
            return jresp({"name": "op-1", "status": "DONE"})
        if path == base + "/instances/gcp-monitor/stop":
            state["status"] = "STOPPING"
            return jresp({"name": "op-2", "status": "RUNNING"})
        if path == base + "/operations/op-2/wait":
            return jresp({"name": "op-2", "status": "RUNNING"})  # wait timed out server-side
        raise AssertionError(path)

    rec = Recorder(with_token(handler))
    adapter = GCEComputeAdapter(make_ctx("gcp_e2_micro", ProviderKind.COMPUTE,
                                settings={"zone": "us-central1-a", "instances": ["gcp-monitor"],
                                          "operation_wait_s": 30},
                                secrets={"service_account_json": json.dumps(service_account)}, handler=rec))
    instances = await adapter.list_instances()
    assert len(instances) == 1
    vm = instances[0]
    assert vm.state == "stopped" and vm.shape == "e2-micro" and vm.public_ip == "34.1.2.3"
    assert vm.private_ip == "10.128.0.2" and vm.region == "us-central1-a" and vm.tags == {"role": "monitor"}
    started = await adapter.start("gcp-monitor")
    assert started.state == "running"
    assert "POST /compute/v1/projects/omni-proj/zones/us-central1-a/operations/op-1/wait" in rec.paths()
    stopped = await adapter.stop("gcp-monitor")
    assert stopped.state == "stopping"
    assert adapter.supports("power")


# ----------------------------------------------------------------------------- Firestore


def test_firestore_codec_roundtrip() -> None:
    value = {"s": "x", "i": 5, "f": 1.5, "b": True, "n": None, "l": [1, "a"], "m": {"k": [False]}}
    encoded = encode_value(value)
    assert encoded["mapValue"]["fields"]["i"] == {"integerValue": "5"}
    assert decode_value(encoded) == value
    assert decode_value({"timestampValue": "2026-01-01T00:00:00Z"}) == "2026-01-01T00:00:00Z"


def _doc(path: str, **fields: Any) -> dict[str, Any]:
    return {"name": f"projects/omni-proj/databases/(default)/documents/{path}",
            "fields": {k: encode_value(v) for k, v in fields.items()}}


async def test_firestore(make_ctx, service_account) -> None:
    root = "/v1/projects/omni-proj/databases/(default)/documents"
    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if path == root + ":listCollectionIds":
            if body.get("pageToken") == "t2":
                return jresp({"collectionIds": ["alpha"]})
            return jresp({"collectionIds": ["users"], "nextPageToken": "t2"})
        if path == root + ":runQuery":
            sq = body["structuredQuery"]
            if sq.get("where"):
                return jresp([{"document": _doc("users/bob", name="Bob", age=40)}, {"readTime": "x"}])
            return jresp([{"document": _doc("users/alice", name="Alice", age=30)},
                          {"document": _doc("users/bob", name="Bob", age=40, admin=True)}])
        if path == root + ":runAggregationQuery":
            return jresp([{"result": {"aggregateFields": {"n": {"integerValue": "2"}}}}])
        if path == root + "/users" and request.method == "GET":
            return jresp({"documents": [_doc("users/alice", name="Alice")]})
        if path == root + "/users/alice" and request.method == "GET":
            return jresp(_doc("users/alice", name="Alice"))
        if path == root + "/users/ghost" and request.method == "GET":
            return jresp({"error": {"code": 404}}, status=404)
        if path == root + "/users" and request.method == "POST":
            assert request.url.params["documentId"] == "carol"
            return jresp(_doc("users/carol", **{k: decode_value(v) for k, v in body["fields"].items()}))
        if path == root + "/users/alice" and request.method == "PATCH":
            return jresp(_doc("users/alice", **{k: decode_value(v) for k, v in body["fields"].items()}))
        if path == root + "/users/alice" and request.method == "DELETE":
            return jresp({})
        raise AssertionError(f"{request.method} {path}")

    rec = Recorder(with_token(handler))
    adapter = FirestoreAdapter(make_ctx("gcp_firestore", ProviderKind.DB, settings={"database": "(default)"},
                               secrets={"service_account_json": json.dumps(service_account)}, handler=rec,
                               limits=[QuotaLimit(metric="bytes", window="total", limit=1073741824)]))
    assert [t.name for t in await adapter.list_tables()] == ["alpha", "users"]
    info = await adapter.describe("users")
    cols = {c.name: c for c in info.columns}
    assert info.row_count == 2 and cols["_id"].primary_key and cols["age"].type == "integer"
    assert cols["admin"].nullable and not cols["name"].nullable

    res = await adapter.query("users")
    assert res.columns == ["_id", "_path", "name"] and res.rows == [["alice", "users/alice", "Alice"]]
    res = await adapter.query(json.dumps({"get": "users/alice"}))
    assert res.rows[0][2] == "Alice"
    assert (await adapter.query('{"get": "users/ghost"}')).row_count == 0
    res = await adapter.query(json.dumps({"collection": "users", "where": [["age", ">=", 35]],
                                          "order_by": [["age", "desc"]], "limit": 5}))
    assert res.rows == [["bob", "users/bob", "Bob", 40]]
    sq = [c for c in calls if c[1].endswith(":runQuery")][-1][2]["structuredQuery"]
    assert sq["where"]["fieldFilter"] == {"field": {"fieldPath": "age"}, "op": "GREATER_THAN_OR_EQUAL",
                                          "value": {"integerValue": "35"}}
    assert sq["orderBy"] == [{"field": {"fieldPath": "age"}, "direction": "DESCENDING"}] and sq["limit"] == 5
    res = await adapter.query('{"collection": "users"}', limit=1)
    assert res.truncated and res.row_count == 1
    with pytest.raises(ProviderError):
        await adapter.query('{"collection": "users", "where": [["age", "~", 1]]}')
    with pytest.raises(ProviderError):
        await adapter.query("{bad json")

    out = await adapter.execute(json.dumps([
        {"op": "create", "collection": "users", "id": "carol", "data": {"name": "Carol"}},
        {"op": "update", "path": "users/alice", "data": {"age": 31}},
        {"op": "delete", "path": "users/alice"},
    ]))
    assert out.affected_rows == 3 and out.row_count == 2
    patch = next(r for r in rec.requests if r.method == "PATCH")
    assert patch.url.params.get_list("updateMask.fieldPaths") == ["age"]
    assert patch.url.params["currentDocument.exists"] == "true"
    with pytest.raises(ProviderError):
        await adapter.execute('{"op": "explode", "path": "users/a"}')
    usage = await adapter.usage()
    assert usage.limit == 1073741824 and usage.source == "config"
    assert (await adapter.health()).ok


# ----------------------------------------------------------------------------- GCS


class FakeGCS:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?")[0]
        if raw == "/upload/storage/v1/b/omni-bucket/o":
            assert request.url.params["uploadType"] == "multipart"
            boundary = request.headers["content-type"].split("boundary=")[1]
            parts = request.content.split(f"--{boundary}".encode())
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
            data = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            item = {"name": meta["name"], "size": str(len(data)), "contentType": meta["contentType"],
                    "etag": "CAE=", "updated": "2026-10-19T10:00:00.000Z", "metadata": meta.get("metadata", {})}
            self.objects[meta["name"]] = {**item, "_data": data}
            return jresp(item)
        if raw == "/storage/v1/b/omni-bucket":
            return jresp({"name": "omni-bucket"})
        if raw == "/storage/v1/b/omni-bucket/o":
            items = [{k: v for k, v in o.items() if k != "_data"} for o in self.objects.values()
                     if o["name"].startswith(request.url.params.get("prefix", ""))]
            if request.url.params.get("pageToken") == "2":
                return jresp({"items": items[1:]})
            if len(items) > 1:
                return jresp({"items": items[:1], "nextPageToken": "2"})
            return jresp({"items": items})
        prefix = "/storage/v1/b/omni-bucket/o/"
        assert raw.startswith(prefix)
        name = unquote(raw[len(prefix):])
        assert "/" not in raw[len(prefix):]  # object names must be fully encoded
        obj = self.objects.get(name)
        if request.method == "DELETE":
            if obj is None:
                return jresp({"error": {"code": 404}}, status=404)
            del self.objects[name]
            return httpx.Response(204)
        if obj is None:
            return jresp({"error": {"code": 404}}, status=404)
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=obj["_data"])
        return jresp({k: v for k, v in obj.items() if k != "_data"})


async def test_gcs(make_ctx, service_account, rsa_key) -> None:
    fake = FakeGCS()
    adapter = GCSStorageAdapter(make_ctx("gcp_gcs", ProviderKind.STORAGE,
                                settings={"bucket": "omni-bucket", "capacity_bytes": 5368709120},
                                secrets={"service_account_json": json.dumps(service_account)},
                                handler=Recorder(with_token(fake))))
    obj = await adapter.put("dir/a b.txt", b"hello", "text/plain", {"src": "pwa"})
    assert obj.size == 5 and obj.content_type == "text/plain" and obj.metadata == {"src": "pwa"}
    await adapter.put("dir/c.bin", b"\x00\x01\x02")
    assert await adapter.get("dir/a b.txt") == b"hello"
    stat = await adapter.stat("dir/a b.txt")
    assert stat is not None and stat.size == 5 and stat.last_modified is not None
    assert await adapter.stat("nope") is None
    with pytest.raises(ProviderError) as exc:
        await adapter.get("nope")
    assert exc.value.status_code == 404
    listed = await adapter.list("dir/")
    assert sorted(o.key for o in listed) == ["dir/a b.txt", "dir/c.bin"]
    quota = await adapter.quota()
    assert quota.used == 8 and quota.limit == 5368709120 and quota.source == "provider"
    await adapter.delete("dir/c.bin")
    await adapter.delete("dir/c.bin")  # idempotent
    assert (await adapter.health()).ok

    url = await adapter.presign("dir/a b.txt", 900)
    parts = urlsplit(url)
    assert parts.path == "/omni-bucket/dir/a%20b.txt"
    query = parse_qs(parts.query)
    assert query["X-Goog-Expires"] == ["900"] and query["X-Goog-Algorithm"] == ["GOOG4-RSA-SHA256"]
    signature = bytes.fromhex(query["X-Goog-Signature"][0])
    canonical_query = parts.query.split("&X-Goog-Signature=")[0]
    canonical_request = "\n".join(["GET", parts.path, canonical_query, "host:storage.googleapis.com\n", "host",
                                   "UNSIGNED-PAYLOAD"])
    date = query["X-Goog-Date"][0]
    scope = f"{date[:8]}/auto/storage/goog4_request"
    string_to_sign = "\n".join(["GOOG4-RSA-SHA256", date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    rsa_key.public_key().verify(signature, string_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())
