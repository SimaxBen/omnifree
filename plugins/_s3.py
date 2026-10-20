"""Small async S3 client with AWS Signature Version 4, shared by S3-compatible storage plugins.

Used by ``plugins.cloudflare.r2`` (and usable by Backblaze B2's S3 endpoint or any other
S3-compatible API). Only the operations OmniFree needs are implemented: PutObject,
GetObject, HeadObject, DeleteObject, ListObjectsV2 and presigned URLs.

The client never owns credentials beyond what the adapter passes in and never creates
its own HTTP client: it sends requests through the adapter's managed ``httpx.AsyncClient``
so test transports and error mapping keep working.

Signing follows https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from core.interfaces import (
    ProviderError,
    StoredObject,
    raise_for_response,
    translate_transport_error,
)

__all__ = [
    "EMPTY_SHA256",
    "UNSIGNED_PAYLOAD",
    "SigV4Signer",
    "S3Client",
    "sha256_hex",
    "uri_encode",
]

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
_ALGORITHM = "AWS4-HMAC-SHA256"
_META_PREFIX = "x-amz-meta-"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 encoding as required by SigV4 (unreserved characters kept)."""
    return quote(value, safe="-_.~" if encode_slash else "-_.~/")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _amz_dates(now: datetime | None) -> tuple[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


@dataclass(frozen=True)
class SigV4Signer:
    """Stateless SigV4 signer for one (credentials, region, service) triple."""

    access_key_id: str
    secret_access_key: str
    region: str
    service: str = "s3"
    session_token: str | None = None

    def _signing_key(self, datestamp: str) -> bytes:
        k_date = _hmac(("AWS4" + self.secret_access_key).encode("utf-8"), datestamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, self.service)
        return _hmac(k_service, "aws4_request")

    def _scope(self, datestamp: str) -> str:
        return f"{datestamp}/{self.region}/{self.service}/aws4_request"

    @staticmethod
    def canonical_query(params: Mapping[str, str]) -> str:
        pairs = sorted((uri_encode(str(k)), uri_encode(str(v))) for k, v in params.items())
        return "&".join(f"{k}={v}" for k, v in pairs)

    def _signature(self, canonical_request: str, amz_date: str, datestamp: str) -> str:
        string_to_sign = "\n".join(
            [_ALGORITHM, amz_date, self._scope(datestamp), sha256_hex(canonical_request.encode("utf-8"))]
        )
        return hmac.new(
            self._signing_key(datestamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def sign_headers(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload_hash: str = EMPTY_SHA256,
        now: datetime | None = None,
    ) -> dict[str, str]:
        """Return the full header set (input headers + x-amz-* + Authorization).

        ``url`` must already contain the canonical (encoded) path and query string.
        """
        amz_date, datestamp = _amz_dates(now)
        parts = urlsplit(url)
        out: dict[str, str] = {k: v for k, v in (headers or {}).items()}
        out["host"] = parts.netloc
        out["x-amz-date"] = amz_date
        out["x-amz-content-sha256"] = payload_hash
        if self.session_token:
            out["x-amz-security-token"] = self.session_token
        canon = {k.lower().strip(): " ".join(str(v).split()) for k, v in out.items()}
        signed_names = sorted(canon)
        query = dict(_split_query(parts.query))
        canonical_request = "\n".join(
            [
                method.upper(),
                parts.path or "/",
                self.canonical_query(query),
                "".join(f"{name}:{canon[name]}\n" for name in signed_names),
                ";".join(signed_names),
                payload_hash,
            ]
        )
        signature = self._signature(canonical_request, amz_date, datestamp)
        out["Authorization"] = (
            f"{_ALGORITHM} Credential={self.access_key_id}/{self._scope(datestamp)}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}"
        )
        out.pop("host")  # httpx sets Host itself from the URL (same value)
        return out

    def presign_url(
        self,
        method: str,
        url: str,
        *,
        expires_s: int = 3600,
        now: datetime | None = None,
    ) -> str:
        """Query-string authentication (presigned URL), host as the only signed header."""
        if not 1 <= expires_s <= 604800:
            raise ValueError("expires_s must be between 1 and 604800 seconds")
        amz_date, datestamp = _amz_dates(now)
        parts = urlsplit(url)
        query = dict(_split_query(parts.query))
        query.update(
            {
                "X-Amz-Algorithm": _ALGORITHM,
                "X-Amz-Credential": f"{self.access_key_id}/{self._scope(datestamp)}",
                "X-Amz-Date": amz_date,
                "X-Amz-Expires": str(expires_s),
                "X-Amz-SignedHeaders": "host",
            }
        )
        if self.session_token:
            query["X-Amz-Security-Token"] = self.session_token
        canonical_query = self.canonical_query(query)
        canonical_request = "\n".join(
            [method.upper(), parts.path or "/", canonical_query, f"host:{parts.netloc}\n", "host", UNSIGNED_PAYLOAD]
        )
        signature = self._signature(canonical_request, amz_date, datestamp)
        return f"{parts.scheme}://{parts.netloc}{parts.path}?{canonical_query}&X-Amz-Signature={signature}"


def _split_query(query: str) -> list[tuple[str, str]]:
    """Split an already-encoded query string back to raw (decoded) pairs."""
    out: list[tuple[str, str]] = []
    if not query:
        return out
    for item in query.split("&"):
        if not item:
            continue
        key, _, value = item.partition("=")
        out.append((unquote(key), unquote(value)))
    return out


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ElementTree.Element, name: str) -> str | None:
    for child in node:
        if _strip_ns(child.tag) == name:
            return child.text
    return None


class S3Client:
    """Path-style S3 client bound to one bucket.

    ``endpoint`` is the service root, e.g. ``https://<account>.r2.cloudflarestorage.com``.
    Objects are addressed as ``{endpoint}/{bucket}/{key}``.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        endpoint: str,
        bucket: str,
        signer: SigV4Signer,
        provider: str,
        public_base_url: str | None = None,
    ) -> None:
        self.http = http
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.signer = signer
        self.provider = provider
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None

    # ------------------------------------------------------------------ helpers
    def object_url(self, key: str = "", query: Mapping[str, str] | None = None) -> str:
        path = f"/{uri_encode(self.bucket)}"
        if key:
            path += "/" + uri_encode(key, encode_slash=False)
        url = self.endpoint + path
        if query:
            url += "?" + SigV4Signer.canonical_query(query)
        return url

    def public_url(self, key: str) -> str | None:
        if not self.public_base_url:
            return None
        return f"{self.public_base_url}/{uri_encode(key, encode_slash=False)}"

    async def _send(
        self,
        method: str,
        key: str = "",
        *,
        query: Mapping[str, str] | None = None,
        content: bytes = b"",
        headers: Mapping[str, str] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        url = self.object_url(key, query)
        signed = self.signer.sign_headers(
            method, url, headers=headers, payload_hash=sha256_hex(content) if content else EMPTY_SHA256
        )
        try:
            response = await self.http.request(method, url, content=content or None, headers=signed)
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.provider) from exc
        if allow_404 and response.status_code == 404:
            return response
        if response.status_code == 404:
            raise ProviderError(f"not found: {key or self.bucket}", provider=self.provider, status_code=404)
        raise_for_response(response, self.provider)
        return response

    def _object_from_headers(self, key: str, response: httpx.Response) -> StoredObject:
        h = response.headers
        metadata = {k[len(_META_PREFIX):]: v for k, v in h.items() if k.lower().startswith(_META_PREFIX)}
        size = h.get("content-length")
        return StoredObject(
            key=key,
            size=int(size) if size and size.isdigit() else 0,
            content_type=h.get("content-type"),
            etag=(h.get("etag") or "").strip('"') or None,
            last_modified=_parse_http_date(h.get("last-modified")),
            backend=self.provider,
            url=self.public_url(key),
            metadata=metadata,
        )

    # ------------------------------------------------------------------ operations
    async def put_object(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        headers: dict[str, str] = {"content-type": content_type or "application/octet-stream"}
        for name, value in (metadata or {}).items():
            headers[_META_PREFIX + name.lower()] = value
        response = await self._send("PUT", key, content=data, headers=headers)
        return StoredObject(
            key=key,
            size=len(data),
            content_type=headers["content-type"],
            etag=(response.headers.get("etag") or "").strip('"') or None,
            last_modified=datetime.now(timezone.utc),
            backend=self.provider,
            url=self.public_url(key),
            metadata={k.lower(): v for k, v in (metadata or {}).items()},
        )

    async def get_object(self, key: str) -> bytes:
        response = await self._send("GET", key)
        return response.content

    async def head_object(self, key: str) -> StoredObject | None:
        response = await self._send("HEAD", key, allow_404=True)
        if response.status_code == 404:
            return None
        return self._object_from_headers(key, response)

    async def delete_object(self, key: str) -> None:
        # S3 DELETE is idempotent: a missing key returns 204, never an error.
        await self._send("DELETE", key, allow_404=True)

    async def list_objects(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        out: list[StoredObject] = []
        token: str | None = None
        while len(out) < limit:
            query: dict[str, str] = {"list-type": "2", "max-keys": str(min(1000, limit - len(out)))}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            response = await self._send("GET", query=query)
            objects, token = self._parse_list(response.content)
            out.extend(objects)
            if not token:
                break
        return out[:limit]

    async def total_size(self, prefix: str = "") -> tuple[int, int]:
        """(bytes, object count) of the whole bucket (or prefix), following pagination."""
        total = count = 0
        token: str | None = None
        while True:
            query: dict[str, str] = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            response = await self._send("GET", query=query)
            objects, token = self._parse_list(response.content)
            total += sum(o.size for o in objects)
            count += len(objects)
            if not token:
                return total, count

    async def head_bucket(self) -> None:
        await self._send("HEAD")

    def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        return self.signer.presign_url(method, self.object_url(key), expires_s=expires_s)

    def _parse_list(self, body: bytes) -> tuple[list[StoredObject], str | None]:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise ProviderError(f"invalid ListObjectsV2 XML: {exc}", provider=self.provider) from exc
        objects: list[StoredObject] = []
        for node in root:
            if _strip_ns(node.tag) != "Contents":
                continue
            key = _child_text(node, "Key") or ""
            size = _child_text(node, "Size") or "0"
            objects.append(
                StoredObject(
                    key=key,
                    size=int(size) if size.isdigit() else 0,
                    etag=(_child_text(node, "ETag") or "").strip('"') or None,
                    last_modified=_parse_iso(_child_text(node, "LastModified")),
                    backend=self.provider,
                    url=self.public_url(key),
                )
            )
        truncated = (_child_text(root, "IsTruncated") or "false").lower() == "true"
        token = _child_text(root, "NextContinuationToken") if truncated else None
        return objects, token

