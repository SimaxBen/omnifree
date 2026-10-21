"""Push notifications through ntfy (https://ntfy.sh): free, no account, an app on Android and iOS.

The owner subscribes the ntfy app to a long random **secret** topic; anyone who knows the topic can read
and post to it on the public server, so it lives in the vault like a password:

* ``NTFY_TOPIC``: the topic name (required);
* ``NTFY_SERVER``: the server origin (optional, default ``https://ntfy.sh``);
* ``NTFY_TOKEN``: an access token for a self-hosted or protected server (optional).

Messages go through ntfy's JSON publish API (``POST {server}`` with ``{"topic", "title", "message", ...}``),
which carries UTF-8 titles without the RFC 2047 header encoding the header API needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "NtfyConfig", "NotifyError", "load_config", "publish", "build_payload",
    "TOPIC_KEY", "SERVER_KEY", "TOKEN_KEY", "DEFAULT_SERVER",
]

TOPIC_KEY = "NTFY_TOPIC"
SERVER_KEY = "NTFY_SERVER"
TOKEN_KEY = "NTFY_TOKEN"
DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_S = 10.0

# Injected by tests (httpx.MockTransport); None = real network.
transport: httpx.AsyncBaseTransport | None = None


class NotifyError(RuntimeError):
    """ntfy refused the message or could not be reached."""


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str | None
    token: str | None

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    @property
    def subscribe_url(self) -> str | None:
        return f"{self.server}/{self.topic}" if self.topic else None


def load_config(secret: Any) -> NtfyConfig:
    """``secret`` is ``Settings.secret`` (env first, then the vault)."""
    server = (secret(SERVER_KEY) or DEFAULT_SERVER).strip().rstrip("/") or DEFAULT_SERVER
    topic = (secret(TOPIC_KEY) or "").strip().strip("/") or None
    token = (secret(TOKEN_KEY) or "").strip() or None
    return NtfyConfig(server=server, topic=topic, token=token)


def build_payload(
    topic: str,
    message: str,
    *,
    title: str | None = None,
    priority: int = 3,
    tags: list[str] | None = None,
    click: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The JSON body of one ntfy publish (empty fields left out)."""
    payload: dict[str, Any] = {"topic": topic, "message": message[:4000] or " "}
    if title:
        payload["title"] = title[:250]
    payload["priority"] = min(max(int(priority), 1), 5)
    if tags:
        payload["tags"] = tags[:5]
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]  # ntfy accepts at most 3 actions
    return payload


async def publish(cfg: NtfyConfig, message: str, **kwargs: Any) -> dict[str, Any]:
    """Send one push; returns ntfy's answer (``id``, ``time`` ...). Raises :class:`NotifyError`."""
    if not cfg.topic:
        raise NotifyError(f"ntfy is not configured (vault key {TOPIC_KEY})")
    payload = build_payload(cfg.topic, message, **kwargs)
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
            resp = await client.post(cfg.server, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise NotifyError(f"ntfy unreachable: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        detail = resp.text[:200].strip()
        raise NotifyError(f"ntfy answered {resp.status_code}{': ' + detail if detail else ''}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}
