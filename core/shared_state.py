"""Optional shared-state backend (Upstash Redis over its REST API) for stateless/serverless hubs.

Selected by ``Settings.state_backend`` (env ``OMNIFREE_STATE_BACKEND``):

* ``local`` (default): every piece of state stays where it always was (SQLite quota DB,
  providers.yaml rewrites, in-memory event history); :func:`shared_state_from_settings` returns None.
* ``upstash``: quota counters, registry overrides and the recent-events list live in Redis so that
  short-lived instances share them. Needs the ``UPSTASH_REDIS_REST_URL`` / ``UPSTASH_REDIS_REST_TOKEN``
  secrets (env or vault); when they are missing a warning is logged and the hub falls back to local.

Every key is namespaced under ``omnifree:``. The client is deliberately tiny (one command or one
``/pipeline`` call per HTTP request) and injectable with an ``httpx`` transport for tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

from core.interfaces import ProviderUnavailable

if TYPE_CHECKING:
    from core.config import Settings

__all__ = [
    "KEY_PREFIX",
    "URL_SECRET",
    "TOKEN_SECRET",
    "SharedStateError",
    "UpstashRest",
    "RedisOverridesStore",
    "shared_state_from_settings",
]

log = logging.getLogger("omnifree.state")

KEY_PREFIX = "omnifree"
URL_SECRET = "UPSTASH_REDIS_REST_URL"
TOKEN_SECRET = "UPSTASH_REDIS_REST_TOKEN"
STATE_PROVIDER = "state"


class SharedStateError(ProviderUnavailable):
    """The shared-state store could not be reached or rejected a command (HTTP 503 in the hub)."""


class UpstashRest:
    """Minimal async Upstash Redis REST client (``POST /`` for one command, ``POST /pipeline`` for many)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def _http(self) -> httpx.AsyncClient:
        # One pooled client per event loop: serverless runtimes and test clients may swap loops.
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._client_loop = loop
        return self._client

    async def _post(self, path: str, body: Any) -> Any:
        try:
            response = await self._http().post(self.url + path, json=body)
        except httpx.HTTPError as exc:
            raise SharedStateError(f"upstash unreachable: {exc}", provider=STATE_PROVIDER) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400 or payload is None:
            detail = payload.get("error") if isinstance(payload, dict) else response.text[:200]
            raise SharedStateError(
                f"upstash HTTP {response.status_code}: {detail}",
                provider=STATE_PROVIDER,
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    def _args(args: list[Any] | tuple[Any, ...]) -> list[str]:
        return [a if isinstance(a, str) else str(a) for a in args]

    @staticmethod
    def _result(item: Any) -> Any:
        if isinstance(item, dict):
            if "error" in item:
                raise SharedStateError(f"upstash error: {item['error']}", provider=STATE_PROVIDER)
            return item.get("result")
        return item

    async def command(self, *args: Any) -> Any:
        """Run one Redis command and return its ``result``."""
        return self._result(await self._post("", self._args(args)))

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        """Run several commands in one HTTP round trip; any per-command error raises."""
        if not commands:
            return []
        payload = await self._post("/pipeline", [self._args(cmd) for cmd in commands])
        if not isinstance(payload, list):
            raise SharedStateError("upstash pipeline answered a non-list", provider=STATE_PROVIDER)
        return [self._result(item) for item in payload]

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._client_loop = None


class RedisOverridesStore:
    """Registry overrides document (enabled/priority/settings patches) stored as one JSON string."""

    def __init__(self, client: UpstashRest, key: str = f"{KEY_PREFIX}:registry:overrides") -> None:
        self.client = client
        self.key = key

    async def load(self) -> dict[str, Any]:
        raw = await self.client.command("GET", self.key)
        if not raw:
            return {}
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("ignoring malformed registry overrides at %s", self.key)
            return {}
        return doc if isinstance(doc, dict) else {}

    async def save(self, doc: dict[str, Any]) -> None:
        await self.client.command("SET", self.key, json.dumps(doc, separators=(",", ":"), sort_keys=True))


def shared_state_from_settings(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> UpstashRest | None:
    """The configured shared-state client, or None for the local backend (incl. graceful fallback)."""
    backend = settings.state_backend
    if backend == "local":
        return None
    url, token = settings.secret(URL_SECRET), settings.secret(TOKEN_SECRET)
    if not url or not token:
        log.warning(
            "state_backend=%s but %s/%s are not set: falling back to local (per-instance) state",
            backend, URL_SECRET, TOKEN_SECRET,
        )
        return None
    return UpstashRest(url, token, transport=transport)
