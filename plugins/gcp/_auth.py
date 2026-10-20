"""Service-account OAuth2 for the GCP plugins (JWT bearer grant, RFC 7523).

The JWT is signed with ``google-auth`` (``google.auth.crypt`` / ``google.auth.jwt``), imported
lazily. When google-auth is not installed, the same RS256 signature is produced with
``cryptography`` (a core dependency), so the plugins keep working on a minimal install.
The token exchange itself goes through the adapter's ``httpx.AsyncClient`` (async, and
test transports apply).

Secret alias: ``service_account_json`` holds the full service-account key JSON
(raw JSON, or the same JSON base64-encoded).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any

from core.interfaces import NotConfigured, ProviderAdapter, ProviderAuthError, ProviderError

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def parse_service_account(raw: str, provider: str) -> dict[str, Any]:
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise NotConfigured("service_account_json is neither JSON nor base64 JSON", provider=provider) from exc
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotConfigured(f"service_account_json is not valid JSON: {exc}", provider=provider) from exc
    for field in ("client_email", "private_key"):
        if not info.get(field):
            raise NotConfigured(f"service_account_json lacks '{field}'", provider=provider)
    return info


def rsa_sign(info: dict[str, Any], message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 / SHA-256 signature with the service-account private key."""
    try:
        from google.auth import crypt  # type: ignore[import-not-found]
    except ImportError:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_private_key(info["private_key"].encode("utf-8"), password=None)
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    signer = crypt.RSASigner.from_service_account_info(info)
    return signer.sign(message)


def build_jwt(info: dict[str, Any], claims: dict[str, Any]) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    try:
        from google.auth import crypt, jwt  # type: ignore[import-not-found]
    except ImportError:
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        return signing_input + "." + _b64url(rsa_sign(info, signing_input.encode("ascii")))
    signer = crypt.RSASigner.from_service_account_info(info)
    token = jwt.encode(signer, claims)
    return token.decode("ascii") if isinstance(token, bytes) else token


class GCPAuthMixin:
    """Adds ``gcp_headers()``, ``project_id()`` and ``service_account()`` to a ProviderAdapter."""

    _gcp_token: tuple[str, float] | None = None
    _gcp_lock: asyncio.Lock | None = None
    _gcp_info: dict[str, Any] | None = None

    def service_account(self: ProviderAdapter) -> dict[str, Any]:  # type: ignore[misc]
        info = getattr(self, "_gcp_info", None)
        if info is None:
            info = parse_service_account(self.require_secret("service_account_json"), self.name)
            self._gcp_info = info  # type: ignore[attr-defined]
        return info

    def project_id(self: ProviderAdapter) -> str:  # type: ignore[misc]
        project = self.setting("project_id") or self.service_account().get("project_id")  # type: ignore[attr-defined]
        if not project:
            raise NotConfigured("setting 'project_id' is required", provider=self.name)
        return str(project)

    async def access_token(self: ProviderAdapter) -> str:  # type: ignore[misc]
        cached = getattr(self, "_gcp_token", None)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        if getattr(self, "_gcp_lock", None) is None:
            self._gcp_lock = asyncio.Lock()  # type: ignore[attr-defined]
        async with self._gcp_lock:  # type: ignore[attr-defined]
            cached = getattr(self, "_gcp_token", None)
            if cached and cached[1] - 60 > time.time():
                return cached[0]
            info = self.service_account()  # type: ignore[attr-defined]
            token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
            now = int(time.time())
            claims = {
                "iss": info["client_email"],
                "scope": str(self.setting("oauth_scope", CLOUD_PLATFORM_SCOPE)),
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            }
            assertion = build_jwt(info, claims)
            try:
                response = await self.request(
                    "POST", token_uri, data={"grant_type": _JWT_GRANT, "assertion": assertion}
                )
            except ProviderError as exc:
                if exc.status_code in (400, 401) and not isinstance(exc, ProviderAuthError):
                    raise ProviderAuthError(f"token exchange failed: {exc.message}", provider=self.name,
                                            status_code=exc.status_code) from exc
                raise
            body = response.json()
            token = body.get("access_token")
            if not token:
                raise ProviderAuthError("token endpoint returned no access_token", provider=self.name)
            self._gcp_token = (token, time.time() + float(body.get("expires_in", 3600)))  # type: ignore[attr-defined]
            return token

    async def gcp_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token()}"}  # type: ignore[attr-defined]

    async def gcp_call(self: ProviderAdapter, method: str, url: str, **kwargs: Any) -> Any:  # type: ignore[misc]
        """Authorized JSON call; returns the decoded body ({} for empty responses)."""
        headers = {**(await self.gcp_headers()), **kwargs.pop("headers", {})}  # type: ignore[attr-defined]
        response = await self.request(method, url, headers=headers, **kwargs)
        if not response.content:
            return {}
        return response.json()
