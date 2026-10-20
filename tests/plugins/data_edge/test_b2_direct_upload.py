"""B2 upload_target / set_cors against a respx-mocked B2 native API, plus automation.b2_cors."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from automation import b2_cors
from core.interfaces import Capability, PluginContext, ProviderError, ProviderKind
from plugins.storage.backblaze_b2 import CORS_RULE_NAME, B2StorageAdapter

AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
API = "https://api005.backblazeb2.com/b2api/v3"


def _adapter(**settings) -> B2StorageAdapter:
    return B2StorageAdapter(PluginContext(
        name="backblaze_b2", kind=ProviderKind.STORAGE,
        settings={"bucket": "omnifree", **settings},
        secrets={"key_id": "kid", "application_key": "app"},
    ))


def _mock_auth(router: respx.MockRouter) -> None:
    router.get(AUTH_URL).mock(return_value=httpx.Response(200, json={
        "accountId": "acc", "authorizationToken": "acct-tok",
        "apiInfo": {"storageApi": {"apiUrl": "https://api005.backblazeb2.com",
                                   "downloadUrl": "https://f005.backblazeb2.com"}},
    }))
    router.post(f"{API}/b2_list_buckets", json__bucketName="omnifree").mock(
        return_value=httpx.Response(200, json={"buckets": [{"bucketId": "bid", "bucketName": "omnifree"}]}))


async def test_upload_target_returns_b2_upload_url(respx_mock: respx.MockRouter) -> None:
    _mock_auth(respx_mock)
    upload = respx_mock.post(f"{API}/b2_get_upload_url").mock(return_value=httpx.Response(200, json={
        "bucketId": "bid", "uploadUrl": "https://pod-000-1-2.backblaze.com/b2api/v3/b2_upload_file/bid/c00",
        "authorizationToken": "upload-tok",
    }))
    adapter = _adapter()
    assert adapter.supports(Capability.DIRECT_UPLOAD)
    before = datetime.now(timezone.utc)
    target = await adapter.upload_target("photos/2026/été vacances+1.jpg", "image/jpeg", 123, expires_s=600)
    assert json.loads(upload.calls.last.request.content) == {"bucketId": "bid"}
    assert upload.calls.last.request.headers["Authorization"] == "acct-tok"
    assert target.provider == "backblaze_b2" and target.key == "photos/2026/été vacances+1.jpg"
    assert target.method == "POST"
    assert target.url == "https://pod-000-1-2.backblaze.com/b2api/v3/b2_upload_file/bid/c00"
    assert target.headers == {
        "Authorization": "upload-tok",
        "X-Bz-File-Name": "photos/2026/%C3%A9t%C3%A9%20vacances%2B1.jpg",
        "Content-Type": "image/jpeg",
        "X-Bz-Content-Sha1": "do_not_verify",
    }
    assert target.expires_at is not None
    assert before + timedelta(seconds=590) < target.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=600)
    # A day at most: B2 upload tokens do not outlive 24 h whatever the caller asks.
    longer = await adapter.upload_target("k", "", expires_s=7 * 86400)
    assert longer.headers["Content-Type"] == "b2/x-auto"
    assert longer.expires_at <= datetime.now(timezone.utc) + timedelta(hours=24)
    await adapter.close()


async def test_upload_target_maps_b2_errors(respx_mock: respx.MockRouter) -> None:
    _mock_auth(respx_mock)
    respx_mock.post(f"{API}/b2_get_upload_url").mock(
        return_value=httpx.Response(503, json={"code": "service_unavailable"}))
    with pytest.raises(ProviderError) as exc:
        await _adapter().upload_target("k", "text/plain")
    assert exc.value.status_code == 503


async def test_set_cors_replaces_only_omnifree_rule(respx_mock: respx.MockRouter) -> None:
    _mock_auth(respx_mock)
    other_rule = {"corsRuleName": "someoneElse", "allowedOrigins": ["https://x"],
                  "allowedOperations": ["b2_download_file_by_name"], "maxAgeSeconds": 60}
    respx_mock.post(f"{API}/b2_list_buckets", json__bucketId="bid").mock(return_value=httpx.Response(200, json={
        "buckets": [{"bucketId": "bid", "bucketName": "omnifree", "bucketType": "allPrivate", "revision": 7,
                     "corsRules": [other_rule, {"corsRuleName": CORS_RULE_NAME, "allowedOrigins": ["https://old"]}]}],
    }))
    update = respx_mock.post(f"{API}/b2_update_bucket").mock(side_effect=lambda r: httpx.Response(200, json={
        "bucketId": "bid", "bucketName": "omnifree", "bucketType": "allPrivate",
        "corsRules": json.loads(r.content)["corsRules"], "revision": 8,
    }))
    result = await _adapter().set_cors(["https://pwa.example/", "http://localhost:5173", "https://pwa.example", " "])
    sent = json.loads(update.calls.last.request.content)
    assert set(sent) == {"accountId", "bucketId", "corsRules", "ifRevisionIs"}  # bucketType etc. untouched
    assert sent["accountId"] == "acc" and sent["bucketId"] == "bid" and sent["ifRevisionIs"] == 7
    assert sent["corsRules"] == [other_rule, {
        "corsRuleName": CORS_RULE_NAME,
        "allowedOrigins": ["http://localhost:5173", "https://pwa.example"],
        "allowedOperations": ["b2_upload_file", "b2_download_file_by_name", "b2_download_file_by_id"],
        "allowedHeaders": ["*"],
        "exposeHeaders": ["x-bz-content-sha1", "x-bz-file-name", "x-bz-file-id"],
        "maxAgeSeconds": 3600,
    }]
    assert result["revision"] == 8


async def test_set_cors_requires_an_origin() -> None:
    with pytest.raises(ProviderError):
        await _adapter().set_cors([" ", ""])


class _FakeRegistry:
    def __init__(self, adapter: B2StorageAdapter) -> None:
        self.adapter = adapter
        self.closed = False
        self.asked: list[tuple[str, bool]] = []

    async def get(self, name: str, *, allow_disabled: bool = False) -> B2StorageAdapter:
        self.asked.append((name, allow_disabled))
        return self.adapter

    async def close(self) -> None:
        self.closed = True


def test_b2_cors_cli(respx_mock: respx.MockRouter, capsys: pytest.CaptureFixture[str]) -> None:
    _mock_auth(respx_mock)
    respx_mock.post(f"{API}/b2_list_buckets", json__bucketId="bid").mock(
        return_value=httpx.Response(200, json={"buckets": [{"bucketId": "bid", "corsRules": []}]}))
    update = respx_mock.post(f"{API}/b2_update_bucket").mock(side_effect=lambda r: httpx.Response(200, json={
        "bucketName": "omnifree", "corsRules": json.loads(r.content)["corsRules"]}))
    registry = _FakeRegistry(_adapter())
    code = b2_cors.main(["--origin", "https://a", "--origin", "https://b"], registry=registry)  # type: ignore[arg-type]
    assert code == 0
    assert registry.asked == [("backblaze_b2", True)] and registry.closed
    sent = json.loads(update.calls.last.request.content)
    assert sent["corsRules"][0]["allowedOrigins"] == ["https://a", "https://b"]
    assert "ifRevisionIs" not in sent
    out = json.loads(capsys.readouterr().out)
    assert out["bucket"] == "omnifree" and out["corsRules"][0]["corsRuleName"] == CORS_RULE_NAME


def test_b2_cors_cli_reports_provider_errors(respx_mock: respx.MockRouter, capsys) -> None:
    respx_mock.get(AUTH_URL).mock(return_value=httpx.Response(401, json={"code": "unauthorized"}))
    registry = _FakeRegistry(_adapter())
    assert b2_cors.main(["--origin", "https://a"], registry=registry) == 1  # type: ignore[arg-type]
    assert "error:" in capsys.readouterr().err and registry.closed
