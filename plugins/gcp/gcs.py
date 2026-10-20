"""Google Cloud Storage through the JSON API (service-account OAuth2, no SDK required).

* put: ``POST /upload/storage/v1/b/{bucket}/o?uploadType=multipart`` (metadata + bytes)
* get: ``GET /storage/v1/b/{bucket}/o/{object}?alt=media``
* stat/delete/list: ``/storage/v1/b/{bucket}/o[...]``
* presign: V4 signed URL (``GOOG4-RSA-SHA256``) signed locally with the service-account key.
* quota: total object bytes from a paginated listing (class A operations, so cached for
  ``settings.usage_scan_ttl_s`` seconds, default 3600) against ``capacity_bytes``.

Settings: ``bucket`` (required), ``project_id``, ``capacity_bytes``, ``usage_scan_ttl_s``,
``api_base`` (override, default ``https://storage.googleapis.com``).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from core.interfaces import (
    Capability,
    HealthStatus,
    PluginMeta,
    ProviderError,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
)
from plugins.gcp._auth import GCPAuthMixin, rsa_sign

_API = "https://storage.googleapis.com"


def _enc(value: str) -> str:
    return quote(value, safe="-_.~")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GCSStorageAdapter(GCPAuthMixin, StorageAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.PRESIGN})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Google Cloud Storage",
        description="5GB-months free (US regions, standard class).",
        required_secrets=("service_account_json",),
        docs_url="https://cloud.google.com/storage/docs/json_api",
    )

    _usage_cache: tuple[float, int, int] | None = None

    def _api(self) -> str:
        return str(self.setting("api_base", _API)).rstrip("/")

    def _bucket(self) -> str:
        return str(self.require_setting("bucket"))

    def _object_url(self, key: str) -> str:
        return f"{self._api()}/storage/v1/b/{_enc(self._bucket())}/o/{_enc(key)}"

    def _to_object(self, item: dict[str, Any]) -> StoredObject:
        size = str(item.get("size", "0"))
        return StoredObject(
            key=str(item.get("name", "")),
            size=int(size) if size.isdigit() else 0,
            content_type=item.get("contentType"),
            etag=item.get("etag") or item.get("md5Hash"),
            last_modified=_parse_time(item.get("updated")),
            backend=self.name,
            metadata={str(k): str(v) for k, v in (item.get("metadata") or {}).items()},
        )

    async def health(self) -> HealthStatus:
        return await self.probe(
            lambda: self.gcp_call("GET", f"{self._api()}/storage/v1/b/{_enc(self._bucket())}",
                                  params={"fields": "name"})
        )

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        ctype = content_type or "application/octet-stream"
        resource: dict[str, Any] = {"name": key, "contentType": ctype}
        if metadata:
            resource["metadata"] = metadata
        boundary = f"omnifree-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(resource).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {ctype}\r\n\r\n".encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        item = await self.gcp_call(
            "POST",
            f"{self._api()}/upload/storage/v1/b/{_enc(self._bucket())}/o",
            params={"uploadType": "multipart"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        self._usage_cache = None
        return self._to_object(item)

    async def get(self, key: str) -> bytes:
        headers = await self.gcp_headers()
        response = await self.request("GET", self._object_url(key), params={"alt": "media"}, headers=headers)
        return response.content

    async def delete(self, key: str) -> None:
        try:
            await self.gcp_call("DELETE", self._object_url(key))
        except ProviderError as exc:
            if exc.status_code != 404:
                raise
        self._usage_cache = None

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        out: list[StoredObject] = []
        params: dict[str, Any] = {"maxResults": min(limit, 1000)}
        if prefix:
            params["prefix"] = prefix
        while len(out) < limit:
            body = await self.gcp_call("GET", f"{self._api()}/storage/v1/b/{_enc(self._bucket())}/o", params=params)
            out.extend(self._to_object(i) for i in body.get("items") or [])
            token = body.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return out[:limit]

    async def stat(self, key: str) -> StoredObject | None:
        try:
            item = await self.gcp_call("GET", self._object_url(key))
        except ProviderError as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._to_object(item)

    async def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        if not 1 <= expires_s <= 604800:
            raise ProviderError("expires_s must be between 1 and 604800", provider=self.name, status_code=400)
        info = self.service_account()
        api = self._api()
        host = urlsplit(api).netloc
        now = datetime.now(timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        scope = f"{day}/auto/storage/goog4_request"
        path = f"/{_enc(self._bucket())}/{quote(key, safe='-_.~/')}"
        query = {
            "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
            "X-Goog-Credential": f"{info['client_email']}/{scope}",
            "X-Goog-Date": stamp,
            "X-Goog-Expires": str(expires_s),
            "X-Goog-SignedHeaders": "host",
        }
        canonical_query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
        canonical_request = "\n".join(
            [method.upper(), path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
        )
        string_to_sign = "\n".join(
            ["GOOG4-RSA-SHA256", stamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signature = rsa_sign(info, string_to_sign.encode()).hex()
        return f"{api}{path}?{canonical_query}&X-Goog-Signature={signature}"

    async def quota(self) -> QuotaInfo:
        ttl = float(self.setting("usage_scan_ttl_s", 3600))
        now = time.monotonic()
        if self._usage_cache is None or now - self._usage_cache[0] > ttl:
            total = count = 0
            params: dict[str, Any] = {"maxResults": 1000, "fields": "items(size),nextPageToken"}
            while True:
                body = await self.gcp_call(
                    "GET", f"{self._api()}/storage/v1/b/{_enc(self._bucket())}/o", params=params
                )
                for item in body.get("items") or []:
                    size = str(item.get("size", "0"))
                    total += int(size) if size.isdigit() else 0
                    count += 1
                token = body.get("nextPageToken")
                if not token:
                    break
                params["pageToken"] = token
            self._usage_cache = (now, total, count)
        _, used, count = self._usage_cache
        capacity = self.setting("capacity_bytes")
        if capacity is None:
            capacity = next((lim.limit for lim in self.context.limits if lim.metric == "bytes"), None)
        return QuotaInfo(
            provider=self.name,
            used=float(used),
            limit=float(capacity) if capacity is not None else None,
            unit="bytes",
            window="total",
            source="provider",
            extra={"objects": count},
        )
