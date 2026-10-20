"""upload_target (presigned PUT) on the R2 and GCS adapters."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from core.interfaces import Capability, ProviderKind
from plugins.cloudflare.r2 import R2StorageAdapter
from plugins.gcp.gcs import GCSStorageAdapter

R2_SECRETS = {"account_id": "acc123", "access_key_id": "AKID", "secret_access_key": "SECRET"}


async def test_r2_upload_target_is_presigned_put(make_ctx) -> None:
    adapter = R2StorageAdapter(make_ctx("cloudflare_r2", ProviderKind.STORAGE, settings={"bucket": "omnifree"},
                                        secrets=R2_SECRETS))
    assert adapter.supports(Capability.DIRECT_UPLOAD)
    target = await adapter.upload_target("videos/big clip.mp4", "video/mp4", 10**9, expires_s=900)
    assert target.provider == "cloudflare_r2" and target.method == "PUT" and target.key == "videos/big clip.mp4"
    assert target.headers == {"Content-Type": "video/mp4"}
    parts = urlsplit(target.url)
    assert parts.netloc == "acc123.r2.cloudflarestorage.com" and parts.path == "/omnifree/videos/big%20clip.mp4"
    query = parse_qs(parts.query)
    assert query["X-Amz-Expires"] == ["900"] and query["X-Amz-SignedHeaders"] == ["host"]
    assert target.expires_at is not None
    assert target.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=900)
    clamped = await adapter.upload_target("k", "", expires_s=10**8)
    assert parse_qs(urlsplit(clamped.url).query)["X-Amz-Expires"] == ["604800"]
    assert clamped.headers == {"Content-Type": "application/octet-stream"}


async def test_gcs_upload_target_is_signed_put(make_ctx, service_account) -> None:
    adapter = GCSStorageAdapter(make_ctx("gcp_gcs", ProviderKind.STORAGE, settings={"bucket": "omni-bucket"},
                                         secrets={"service_account_json": json.dumps(service_account)}))
    assert adapter.supports(Capability.DIRECT_UPLOAD)
    target = await adapter.upload_target("dir/a b.txt", "text/plain", 5, expires_s=600)
    assert target.method == "PUT" and target.provider == "gcp_gcs"
    assert target.headers == {"Content-Type": "text/plain"}
    parts = urlsplit(target.url)
    assert parts.path == "/omni-bucket/dir/a%20b.txt"
    query = parse_qs(parts.query)
    assert query["X-Goog-Expires"] == ["600"] and "X-Goog-Signature" in query
