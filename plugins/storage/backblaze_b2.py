"""Backblaze B2 storage adapter using the native B2 API v3.

(``plugins/_s3.py`` was not available when this adapter was written, so the S3-compatible
endpoint is not used.) Flow: ``b2_authorize_account`` (Basic ``key_id:application_key``,
token cached ~23 h and refreshed on ``expired_auth_token``), ``b2_get_upload_url`` +
upload, downloads from ``downloadUrl/file/<bucket>/<key>``, ``b2_list_file_versions`` for
listing, deletion and capacity accounting, ``b2_get_download_authorization`` for presigning.

Settings: ``bucket`` (required), ``bucket_id`` (optional, skips a lookup),
``capacity_bytes``, ``auth_url`` (default ``https://api.backblazeb2.com``),
``usage_cache_s`` (default 300: bucket size scans cost class C transactions).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import quote, unquote

import httpx

from core.interfaces import (
    Capability,
    HealthStatus,
    PluginMeta,
    ProviderError,
    ProviderUnavailable,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
)

API = "/b2api/v3"
_TOKEN_TTL_S = 23 * 3600
_INFO_PREFIX = "x-bz-info-"


def _b2_quote(key: str) -> str:
    """B2 file names are percent-encoded UTF-8 with '/' kept."""
    return quote(key, safe="/")


def _ms_to_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


class B2StorageAdapter(StorageAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.PRESIGN})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Backblaze B2",
        description="Backblaze B2 via the native B2 API v3.",
        required_secrets=("key_id", "application_key"),
        docs_url="https://www.backblaze.com/apidocs/introduction-to-the-b2-native-api",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._auth: dict[str, Any] | None = None
        self._auth_at = 0.0
        self._bucket_id: str | None = None
        self._lock = asyncio.Lock()
        self._usage: tuple[float, QuotaInfo] | None = None

    # ------------------------------------------------------------------ auth
    @property
    def bucket(self) -> str:
        return str(self.require_setting("bucket"))

    async def _authorize(self, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if not force and self._auth and time.time() - self._auth_at < _TOKEN_TTL_S:
                return self._auth
            base = str(self.setting("auth_url", "https://api.backblazeb2.com")).rstrip("/")
            response = await self.request(
                "GET", f"{base}{API}/b2_authorize_account",
                auth=httpx.BasicAuth(self.require_secret("key_id"), self.require_secret("application_key")),
            )
            payload = response.json()
            storage = (payload.get("apiInfo") or {}).get("storageApi") or {}
            self._auth = {
                "accountId": payload["accountId"],
                "token": payload["authorizationToken"],
                "apiUrl": storage["apiUrl"].rstrip("/"),
                "downloadUrl": storage["downloadUrl"].rstrip("/"),
                "restrictedBucketId": storage.get("bucketId"),
                "restrictedBucketName": storage.get("bucketName"),
            }
            self._auth_at = time.time()
            return self._auth

    async def _api(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a B2 API call, re-authorizing once when the token expired."""
        for attempt in (0, 1):
            auth = await self._authorize(force=attempt == 1)
            try:
                response = await self.request(
                    "POST", f"{auth['apiUrl']}{API}/{operation}", json=body,
                    headers={"Authorization": auth["token"]},
                )
            except ProviderError as exc:
                if exc.status_code == 401 and attempt == 0:
                    continue
                raise
            return response.json()
        raise ProviderError(f"{operation} failed after re-authorization", provider=self.name)

    async def _get_bucket_id(self) -> str:
        if self._bucket_id:
            return self._bucket_id
        configured = self.setting("bucket_id")
        auth = await self._authorize()
        if configured:
            self._bucket_id = str(configured)
        elif auth.get("restrictedBucketId") and auth.get("restrictedBucketName") == self.bucket:
            self._bucket_id = str(auth["restrictedBucketId"])
        else:
            payload = await self._api("b2_list_buckets", {"accountId": auth["accountId"], "bucketName": self.bucket})
            buckets = payload.get("buckets") or []
            if not buckets:
                raise ProviderError(f"bucket {self.bucket} not found", provider=self.name, status_code=404)
            self._bucket_id = str(buckets[0]["bucketId"])
        return self._bucket_id

    def _object(self, info: dict[str, Any]) -> StoredObject:
        sha1 = info.get("contentSha1")
        return StoredObject(
            key=info["fileName"],
            size=int(info.get("contentLength") or 0),
            content_type=info.get("contentType"),
            etag=None if sha1 in (None, "none") else sha1,
            last_modified=_ms_to_dt(info.get("uploadTimestamp")),
            backend=self.name,
            metadata={str(k): str(v) for k, v in (info.get("fileInfo") or {}).items()},
        )

    def _download_url(self, auth: dict[str, Any], key: str) -> str:
        return f"{auth['downloadUrl']}/file/{quote(self.bucket, safe='')}/{_b2_quote(key)}"

    async def _download(self, method: str, key: str) -> httpx.Response:
        for attempt in (0, 1):
            auth = await self._authorize(force=attempt == 1)
            try:
                return await self.request(method, self._download_url(auth, key),
                                          headers={"Authorization": auth["token"]})
            except ProviderError as exc:
                if exc.status_code == 401 and attempt == 0:
                    continue
                raise
        raise ProviderError("download failed after re-authorization", provider=self.name)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(self._get_bucket_id)

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        bucket_id = await self._get_bucket_id()
        headers = {
            "X-Bz-File-Name": _b2_quote(key),
            "Content-Type": content_type or "b2/x-auto",
            "Content-Length": str(len(data)),
            "X-Bz-Content-Sha1": hashlib.sha1(data).hexdigest(),
        }
        for name, value in (metadata or {}).items():
            headers[f"X-Bz-Info-{name}"] = quote(str(value), safe="")
        last_error: ProviderError | None = None
        for _attempt in range(3):  # B2 asks clients to fetch a fresh upload URL on 401/408/5xx
            target = await self._api("b2_get_upload_url", {"bucketId": bucket_id})
            try:
                response = await self.request(
                    "POST", target["uploadUrl"], content=data,
                    headers={**headers, "Authorization": target["authorizationToken"]},
                )
            except ProviderUnavailable as exc:
                last_error = exc
                continue
            except ProviderError as exc:
                if exc.status_code == 401:
                    last_error = exc
                    continue
                raise
            self._usage = None
            return self._object(response.json())
        raise last_error or ProviderUnavailable("upload failed", provider=self.name)

    async def get(self, key: str) -> bytes:
        response = await self._download("GET", key)
        return response.content

    async def stat(self, key: str) -> StoredObject | None:
        try:
            response = await self._download("HEAD", key)
        except ProviderError as exc:
            if exc.status_code == 404:
                return None
            raise
        h = response.headers
        sha1 = h.get("x-bz-content-sha1")
        return StoredObject(
            key=key,
            size=int(h.get("content-length") or 0),
            content_type=h.get("content-type"),
            etag=None if sha1 in (None, "none") else sha1,
            last_modified=_ms_to_dt(h.get("x-bz-upload-timestamp")),
            backend=self.name,
            metadata={k[len(_INFO_PREFIX):]: unquote(v) for k, v in h.items() if k.lower().startswith(_INFO_PREFIX)},
        )

    async def delete(self, key: str) -> None:
        bucket_id = await self._get_bucket_id()
        start_id: str | None = None
        while True:
            body: dict[str, Any] = {"bucketId": bucket_id, "startFileName": key, "prefix": key, "maxFileCount": 100}
            if start_id:
                body["startFileId"] = start_id
            page = await self._api("b2_list_file_versions", body)
            versions = [f for f in page.get("files") or [] if f.get("fileName") == key]
            for version in versions:
                await self._api("b2_delete_file_version", {"fileName": key, "fileId": version["fileId"]})
            start_id = page.get("nextFileId")
            if not start_id or page.get("nextFileName") != key:
                break
        self._usage = None

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        bucket_id = await self._get_bucket_id()
        out: list[StoredObject] = []
        start: str | None = None
        while len(out) < limit:
            body: dict[str, Any] = {"bucketId": bucket_id, "prefix": prefix,
                                    "maxFileCount": min(limit - len(out), 10_000)}
            if start:
                body["startFileName"] = start
            page = await self._api("b2_list_file_names", body)
            out.extend(self._object(f) for f in page.get("files") or [] if f.get("action", "upload") == "upload")
            start = page.get("nextFileName")
            if not start:
                break
        return out[:limit]

    async def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        if method.upper() != "GET":
            raise ProviderError("B2 download authorizations only cover GET", provider=self.name, status_code=400)
        bucket_id = await self._get_bucket_id()
        payload = await self._api("b2_get_download_authorization", {
            "bucketId": bucket_id,
            "fileNamePrefix": key,
            "validDurationInSeconds": max(1, min(int(expires_s), 604_800)),
        })
        auth = await self._authorize()
        return f"{self._download_url(auth, key)}?Authorization={quote(payload['authorizationToken'], safe='')}"

    async def quota(self) -> QuotaInfo:
        ttl = float(self.setting("usage_cache_s", 300))
        if self._usage and time.time() - self._usage[0] < ttl:
            return self._usage[1]
        bucket_id = await self._get_bucket_id()
        used = 0
        files = 0
        start_name: str | None = None
        start_id: str | None = None
        while True:
            body: dict[str, Any] = {"bucketId": bucket_id, "maxFileCount": 10_000}
            if start_name:
                body["startFileName"] = start_name
            if start_id:
                body["startFileId"] = start_id
            page = await self._api("b2_list_file_versions", body)
            for f in page.get("files") or []:
                used += int(f.get("contentLength") or 0)
                files += 1
            start_name, start_id = page.get("nextFileName"), page.get("nextFileId")
            if not start_name:
                break
        limit = self.setting("capacity_bytes") or next(
            (lim.limit for lim in self.context.limits if lim.metric == "bytes"), None
        )
        info = QuotaInfo(provider=self.name, used=float(used), limit=None if limit is None else float(limit),
                         unit="bytes", window="total", source="provider", extra={"file_versions": files})
        self._usage = (time.time(), info)
        return info
