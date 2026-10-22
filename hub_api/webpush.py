"""Native Web Push (RFC 8030 + VAPID RFC 8292) to the OmniFree PWA, alongside ntfy (see :mod:`hub_api.notifier`).

Vault keys (generate them with ``python -m automation.vapid``):

* ``VAPID_PUBLIC_KEY``: the P-256 public key as **base64url (no padding) of the 65-byte uncompressed point**
  (``0x04 || X || Y``, 87 characters starting with ``B``). The PWA passes it as ``applicationServerKey``.
* ``VAPID_PRIVATE_KEY``: the private key as **base64url (no padding) of the raw 32-byte scalar** (43 characters).
  A PEM (``-----BEGIN PRIVATE KEY-----`` / ``-----BEGIN EC PRIVATE KEY-----``) or base64 DER is accepted too.
* ``VAPID_SUBJECT``: the ``sub`` claim, a ``mailto:`` or ``https:`` URL (optional, default
  ``https://omnifree.ilyasouhnine.tech``).

Only the private key is strictly needed: the public key is derived from it, and a ``VAPID_PUBLIC_KEY`` that does not
match is reported as a configuration error (the browsers would reject every push otherwise).

Sending uses ``pywebpush`` (encryption aes128gcm + VAPID JWT), imported lazily so the hub starts without it. Its
call is synchronous (``requests``) and runs in a worker thread with a short timeout.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PUBLIC_KEY", "PRIVATE_KEY", "SUBJECT_KEY", "DEFAULT_SUBJECT", "MAX_PAYLOAD_BYTES", "DEFAULT_TTL_S",
    "WebPushConfig", "SendResult", "load_config", "build_payload", "send", "b64url", "public_key_of",
    "parse_private_key", "raw_private_key",
]

log = logging.getLogger("omnifree.hub.webpush")

PUBLIC_KEY = "VAPID_PUBLIC_KEY"
PRIVATE_KEY = "VAPID_PRIVATE_KEY"
SUBJECT_KEY = "VAPID_SUBJECT"
DEFAULT_SUBJECT = "https://omnifree.ilyasouhnine.tech"
MAX_PAYLOAD_BYTES = 3000  # push services accept ~4 KB of ciphertext; stay well below
DEFAULT_TTL_S = 12 * 3600
TIMEOUT_S = 10.0


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    text = text.strip()
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + pad)


def parse_private_key(value: str) -> Any:
    """An ``EllipticCurvePrivateKey`` (P-256) from raw base64url, PEM or base64 DER. Raises ``ValueError``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    text = value.strip()
    if text.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
    else:
        raw = _b64decode("".join(text.split()))
        if len(raw) == 32:
            key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        else:
            key = serialization.load_der_private_key(raw, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("not a P-256 (prime256v1) private key")
    return key


def public_key_of(private_key: Any) -> str:
    """base64url of the uncompressed public point: what browsers expect as ``applicationServerKey``."""
    from cryptography.hazmat.primitives import serialization

    point = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64url(point)


def raw_private_key(private_key: Any) -> str:
    return b64url(private_key.private_numbers().private_value.to_bytes(32, "big"))


@dataclass(frozen=True)
class WebPushConfig:
    public_key: str | None
    private_key: Any  # EllipticCurvePrivateKey or None
    subject: str
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.private_key is not None and bool(self.public_key) and not self.error


def load_config(secret: Any) -> WebPushConfig:
    """``secret`` is ``Settings.secret`` (env first, then the vault). Never raises."""
    subject = (secret(SUBJECT_KEY) or "").strip() or DEFAULT_SUBJECT
    if not subject.startswith(("mailto:", "https://")):
        subject = f"mailto:{subject}" if "@" in subject else DEFAULT_SUBJECT
    priv_text = (secret(PRIVATE_KEY) or "").strip()
    pub_text = "".join((secret(PUBLIC_KEY) or "").split()).rstrip("=") or None
    if not priv_text:
        return WebPushConfig(public_key=pub_text, private_key=None, subject=subject,
                             error=f"vault key {PRIVATE_KEY} is not set")
    try:
        key = parse_private_key(priv_text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a config error
        return WebPushConfig(public_key=pub_text, private_key=None, subject=subject,
                             error=f"{PRIVATE_KEY} is not a valid P-256 key ({type(exc).__name__})")
    derived = public_key_of(key)
    if pub_text and pub_text.replace("+", "-").replace("/", "_") != derived:
        return WebPushConfig(public_key=derived, private_key=key, subject=subject,
                             error=f"{PUBLIC_KEY} does not match {PRIVATE_KEY}")
    return WebPushConfig(public_key=derived, private_key=key, subject=subject)


def build_payload(
    title: str,
    body: str,
    *,
    tag: str | None = None,
    url: str | None = None,
    actions: list[dict[str, str]] | None = None,
) -> str:
    """The JSON the service worker reads: ``{title, body, tag, url, actions}``, at most :data:`MAX_PAYLOAD_BYTES`."""
    data: dict[str, Any] = {"title": title[:200] or "OmniFree", "body": body, "tag": (tag or "")[:64] or None,
                            "url": url, "actions": (actions or [])[:2]}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    while len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        over = len(text.encode("utf-8")) - MAX_PAYLOAD_BYTES
        if len(data["body"]) > 40:
            data["body"] = data["body"][: max(40, len(data["body"]) - over - 4)].rstrip() + "…"
        elif data["actions"]:
            data["actions"] = data["actions"][:-1]
        else:
            data["title"] = data["title"][:60]
            data["body"] = data["body"][:20]
            data["url"] = None
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text


@dataclass
class SendResult:
    ok: bool
    status: int | None = None
    error: str | None = None

    @property
    def gone(self) -> bool:
        """404/410: the subscription expired or was revoked; it must be deleted."""
        return self.status in (404, 410)


def _send_sync(cfg: WebPushConfig, subscription: dict[str, Any], payload: str, ttl: int, urgency: str) -> SendResult:
    try:
        import pywebpush
        from py_vapid import Vapid01
    except ImportError:
        return SendResult(ok=False, error="pywebpush is not installed on the hub")
    try:
        resp = pywebpush.webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=Vapid01(cfg.private_key),
            vapid_claims={"sub": cfg.subject},
            ttl=ttl,
            timeout=TIMEOUT_S,
            headers={"Urgency": urgency},
        )
    except pywebpush.WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = str(getattr(exc, "message", "") or exc).splitlines()[0][:200]
        return SendResult(ok=False, status=status, error=detail or f"push service answered {status}")
    except Exception as exc:  # noqa: BLE001 - network errors from requests and anything else
        return SendResult(ok=False, error=f"push service unreachable: {type(exc).__name__}")
    return SendResult(ok=True, status=getattr(resp, "status_code", 201))


async def send(
    cfg: WebPushConfig,
    subscription: dict[str, Any],
    payload: str,
    *,
    ttl: int = DEFAULT_TTL_S,
    urgency: str = "normal",
) -> SendResult:
    """Encrypt and POST one push to ``subscription`` (``{endpoint, keys: {p256dh, auth}}``). Never raises."""
    if not cfg.configured:
        return SendResult(ok=False, error=cfg.error or "web push is not configured")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_send_sync, cfg, subscription, payload, ttl, urgency), TIMEOUT_S + 2
        )
    except TimeoutError:
        return SendResult(ok=False, error="push service timed out")
