from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from core.interfaces import Capability, ProviderError, ProviderKind
from plugins.storage.backblaze_b2 import B2StorageAdapter

from .helpers import Routes, body, make

API = "https://api005.backblazeb2.com/b2api/v3"
DL = "https://f005.backblazeb2.com"


def b2_routes(**auth_extra) -> Routes:
    routes = Routes()
    routes.add("GET", r"api\.backblazeb2\.com/b2api/v3/b2_authorize_account$", {
        "accountId": "acc",
        "authorizationToken": "tok",
        "apiInfo": {"storageApi": {"apiUrl": "https://api005.backblazeb2.com",
                                   "downloadUrl": DL, **auth_extra}},
    })
    routes.add("POST", r"/b2_list_buckets$", {"buckets": [{"bucketId": "bid", "bucketName": "omnifree"}]})
    return routes


def b2(routes: Routes, **settings) -> B2StorageAdapter:
    return make(B2StorageAdapter, ProviderKind.STORAGE, routes,
                settings={"bucket": "omnifree", "capacity_bytes": 10737418240, **settings},
                secrets={"key_id": "kid", "application_key": "app"})


async def test_b2_put_uploads_with_sha1_and_metadata() -> None:
    routes = b2_routes()
    routes.add("POST", r"/b2_get_upload_url$", {"uploadUrl": "https://pod-000.backblaze.com/upload/x",
                                               "authorizationToken": "up-tok"})

    def upload(request: httpx.Request):
        assert request.headers["Authorization"] == "up-tok"
        assert request.headers["X-Bz-File-Name"] == "docs/hello%20world.txt"
        assert request.headers["X-Bz-Content-Sha1"] == hashlib.sha1(b"hi").hexdigest()
        assert request.headers["X-Bz-Info-owner"] == "simax%20h"
        return {"fileId": "f1", "fileName": "docs/hello world.txt", "contentLength": 2,
                "contentType": "text/plain", "contentSha1": hashlib.sha1(b"hi").hexdigest(),
                "fileInfo": {"owner": "simax h"}, "uploadTimestamp": 1700000000000}

    routes.add("POST", r"pod-000\.backblaze\.com/upload/x$", upload)
    storage = b2(routes)
    obj = await storage.put("docs/hello world.txt", b"hi", "text/plain", {"owner": "simax h"})
    assert obj.key == "docs/hello world.txt" and obj.size == 2 and obj.backend == "test"
    assert obj.last_modified is not None and obj.metadata == {"owner": "simax h"}
    auth_req = routes.requests[0]
    assert auth_req.headers["Authorization"] == "Basic " + base64.b64encode(b"kid:app").decode()
    assert body(routes.find("POST", "b2_list_buckets")[0]) == {"accountId": "acc", "bucketName": "omnifree"}


async def test_b2_put_retries_with_fresh_upload_url_on_503() -> None:
    routes = b2_routes(bucketId="bid", bucketName="omnifree")
    urls = iter(["https://pod-1/u", "https://pod-2/u"])
    routes.add("POST", r"/b2_get_upload_url$", lambda r: {"uploadUrl": next(urls), "authorizationToken": "t"})
    routes.add("POST", r"pod-1/u$", {"code": "service_unavailable"}, status=503)
    routes.add("POST", r"pod-2/u$", {"fileId": "f", "fileName": "k", "contentLength": 1})
    storage = b2(routes)
    obj = await storage.put("k", b"x")
    assert obj.key == "k"
    assert not routes.find("POST", "b2_list_buckets")  # restricted key carries the bucket id


async def test_b2_get_stat_delete_list() -> None:
    routes = b2_routes()
    routes.add("GET", rf"{DL}/file/omnifree/a%20b\.txt$", b"content")
    routes.add("HEAD", rf"{DL}/file/omnifree/a%20b\.txt$", lambda r: httpx.Response(200, headers={
        "content-length": "7", "content-type": "text/plain", "x-bz-content-sha1": "abc",
        "x-bz-upload-timestamp": "1700000000000", "x-bz-info-owner": "me%20too"}))
    routes.add("HEAD", rf"{DL}/file/omnifree/missing$", None, status=404)
    routes.add("GET", rf"{DL}/file/omnifree/missing$", {"code": "not_found"}, status=404)
    routes.add("POST", r"/b2_list_file_versions$", {"files": [
        {"fileName": "a b.txt", "fileId": "v2"}, {"fileName": "a b.txt", "fileId": "v1"},
        {"fileName": "a b.txt.bak", "fileId": "other"}], "nextFileName": None, "nextFileId": None})
    routes.add("POST", r"/b2_delete_file_version$", lambda r: body(r))
    pages = iter([
        {"files": [{"fileName": "a", "contentLength": 1, "action": "upload"},
                   {"fileName": "b", "contentLength": 2, "action": "upload"}], "nextFileName": "c"},
        {"files": [{"fileName": "c", "contentLength": 3, "action": "upload"}], "nextFileName": None},
    ])
    routes.add("POST", r"/b2_list_file_names$", lambda r: next(pages))
    storage = b2(routes)

    assert await storage.get("a b.txt") == b"content"
    assert routes.find("GET", "/file/omnifree/")[0].headers["Authorization"] == "tok"
    stat = await storage.stat("a b.txt")
    assert stat is not None and stat.size == 7 and stat.metadata == {"owner": "me too"} and stat.etag == "abc"
    assert await storage.stat("missing") is None
    with pytest.raises(ProviderError) as err:
        await storage.get("missing")
    assert err.value.status_code == 404

    await storage.delete("a b.txt")
    deleted = [body(r)["fileId"] for r in routes.find("POST", "b2_delete_file_version")]
    assert deleted == ["v2", "v1"]

    listed = await storage.list("", limit=3)
    assert [o.key for o in listed] == ["a", "b", "c"]
    assert body(routes.find("POST", "b2_list_file_names")[1])["startFileName"] == "c"


async def test_b2_quota_sums_versions_and_caches() -> None:
    routes = b2_routes()
    routes.add("POST", r"/b2_list_file_versions$", {"files": [{"contentLength": 100}, {"contentLength": 23}],
                                                    "nextFileName": None})
    storage = b2(routes)
    quota = await storage.quota()
    assert quota.used == 123 and quota.limit == 10737418240
    assert quota.unit == "bytes" and quota.source == "provider"
    await storage.quota()
    assert len(routes.find("POST", "b2_list_file_versions")) == 1


async def test_b2_presign_and_reauth_on_expired_token() -> None:
    routes = b2_routes()
    calls = {"n": 0}

    def download_auth(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"code": "expired_auth_token"})
        assert body(request)["validDurationInSeconds"] == 600
        return {"authorizationToken": "dl/tok"}

    routes.add("POST", r"/b2_get_download_authorization$", download_auth)
    storage = b2(routes)
    assert storage.supports(Capability.PRESIGN)
    url = await storage.presign("x/y.png", expires_s=600)
    assert url == f"{DL}/file/omnifree/x/y.png?Authorization=dl%2Ftok"
    assert len(routes.find("GET", "b2_authorize_account")) == 2
    with pytest.raises(ProviderError):
        await storage.presign("x", method="PUT")
