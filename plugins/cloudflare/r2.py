"""Cloudflare R2 storage through its S3-compatible API (SigV4, region ``auto``).

Settings:
  bucket            bucket name (required)
  endpoint          optional override of ``https://{account_id}.r2.cloudflarestorage.com``
                    (e.g. the EU jurisdiction endpoint ``https://{account_id}.eu.r2.cloudflarestorage.com``)
  region            SigV4 region, default ``auto``
  public_base_url   optional r2.dev / custom-domain base used to fill ``StoredObject.url``
  capacity_bytes    free-tier capacity used by ``quota()``
  usage_scan_ttl_s  how long a bucket-size scan is cached (ListObjectsV2 = class A operations), default 900
"""

from __future__ import annotations

import time
from typing import ClassVar

from core.interfaces import (
    Capability,
    HealthStatus,
    PluginMeta,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
)
from plugins._s3 import S3Client, SigV4Signer


class R2StorageAdapter(StorageAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.PRESIGN})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cloudflare R2",
        description="S3-compatible object storage, 10GB free, zero egress.",
        required_secrets=("account_id", "access_key_id", "secret_access_key"),
        docs_url="https://developers.cloudflare.com/r2/api/s3/api/",
    )

    _usage_cache: tuple[float, int, int] | None = None

    def _client(self) -> S3Client:
        account_id = self.require_secret("account_id")
        endpoint = str(self.setting("endpoint", "https://{account_id}.r2.cloudflarestorage.com"))
        endpoint = endpoint.replace("{account_id}", account_id)
        signer = SigV4Signer(
            access_key_id=self.require_secret("access_key_id"),
            secret_access_key=self.require_secret("secret_access_key"),
            region=str(self.setting("region", "auto")),
        )
        return S3Client(
            http=self.http,
            endpoint=endpoint,
            bucket=str(self.require_setting("bucket")),
            signer=signer,
            provider=self.name,
            public_base_url=self.setting("public_base_url"),
        )

    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._client().head_bucket())

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        result = await self._client().put_object(key, data, content_type, metadata)
        self._usage_cache = None
        return result

    async def get(self, key: str) -> bytes:
        return await self._client().get_object(key)

    async def delete(self, key: str) -> None:
        await self._client().delete_object(key)
        self._usage_cache = None

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        return await self._client().list_objects(prefix, limit)

    async def stat(self, key: str) -> StoredObject | None:
        return await self._client().head_object(key)

    async def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        return self._client().presign(key, expires_s, method)

    async def quota(self) -> QuotaInfo:
        ttl = float(self.setting("usage_scan_ttl_s", 900))
        now = time.monotonic()
        if self._usage_cache is None or now - self._usage_cache[0] > ttl:
            used, count = await self._client().total_size()
            self._usage_cache = (now, used, count)
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
