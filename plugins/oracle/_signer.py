"""OCI API request signing (HTTP Signatures draft-cavage-08, RSA-SHA256).

Implements https://docs.oracle.com/en-us/iaas/Content/API/Concepts/signingrequests.htm with
``cryptography`` so OCI REST calls run on ``httpx.AsyncClient`` without the (sync) ``oci`` SDK.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Any

import httpx

_GENERIC = ("date", "(request-target)", "host")
_BODY = ("content-length", "content-type", "x-content-sha256")


def normalize_pem(value: str) -> str:
    """Accept PEM with real newlines, escaped ``\\n`` sequences, or base64 of the PEM."""
    text = value.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if not text.startswith("-----BEGIN"):
        try:
            decoded = base64.b64decode(text, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return text
        if decoded.strip().startswith("-----BEGIN"):
            return decoded.strip() + "\n"
    return text + ("\n" if not text.endswith("\n") else "")


@dataclass
class OCISigner:
    tenancy_ocid: str
    user_ocid: str
    fingerprint: str
    private_key_pem: str
    passphrase: str | None = None
    _key: Any = field(default=None, init=False, repr=False)

    @property
    def key_id(self) -> str:
        return f"{self.tenancy_ocid}/{self.user_ocid}/{self.fingerprint}"

    def _private_key(self) -> Any:
        if self._key is None:
            from cryptography.hazmat.primitives import serialization

            self._key = serialization.load_pem_private_key(
                normalize_pem(self.private_key_pem).encode("utf-8"),
                password=self.passphrase.encode("utf-8") if self.passphrase else None,
            )
        return self._key

    def sign(self, method: str, url: str, body: bytes | None = None, *, date: str | None = None,
             content_type: str = "application/json") -> dict[str, str]:
        """Return the headers to send (date, host, body headers, authorization)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        parsed = httpx.URL(url)
        method_l = method.lower()
        headers: dict[str, str] = {
            "date": date or formatdate(usegmt=True),
            "host": parsed.netloc.decode("ascii"),
        }
        names = list(_GENERIC)
        if method_l in ("post", "put", "patch"):
            payload = body or b""
            headers["content-length"] = str(len(payload))
            headers["content-type"] = content_type
            headers["x-content-sha256"] = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
            names += list(_BODY)
        target = f"{method_l} {parsed.raw_path.decode('ascii')}"
        lines = [f"(request-target): {target}" if n == "(request-target)" else f"{n}: {headers[n]}" for n in names]
        signature = self._private_key().sign("\n".join(lines).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        headers["authorization"] = (
            f'Signature version="1",keyId="{self.key_id}",algorithm="rsa-sha256",'
            f'headers="{" ".join(names)}",signature="{base64.b64encode(signature).decode("ascii")}"'
        )
        return headers
