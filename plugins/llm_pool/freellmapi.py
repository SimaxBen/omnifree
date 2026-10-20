"""Self-hosted FreeLLMAPI proxy (https://github.com/tashfeenahmed/freellmapi) on Oracle ARM.

Verified against the upstream README / sources (2026-10):

* OpenAI-compatible base URL ``http://<host>:3001/v1`` (``/v1/chat/completions``, ``/v1/models``,
  ``/v1/embeddings``, ``/v1/audio/transcriptions``); ``model: "auto"`` lets FreeLLMAPI route.
* Clients authenticate with the *unified key* (``Authorization: Bearer freellmapi-...``) shown on
  the dashboard Keys page; it is stored in the vault as ``FREELLMAPI_API_KEY``.
* Every response carries ``X-Routed-Via: <platform>/<model>``. ``routed_via`` therefore becomes
  ``"freellmapi:<platform>"`` (``settings.routed_via_format: full`` keeps ``<platform>/<model>``).
  The header name defaults to ``X-Routed-Via`` when ``settings.routed_via_header`` is unset.
* ``GET /api/ping`` is the unauthenticated liveness endpoint used by the container healthcheck.

Upstream keys are pushed into FreeLLMAPI by :mod:`plugins.llm_pool.freellmapi_injector`.
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from core.interfaces import Capability, HealthStatus, PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter

DEFAULT_ROUTED_VIA_HEADER = "X-Routed-Via"


class FreeLLMAPIAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="FreeLLMAPI (self-hosted pool)",
        description="OpenAI-compatible proxy pooling free upstream keys with its own failover.",
        required_secrets=("api_key",),
        docs_url="https://github.com/tashfeenahmed/freellmapi",
    )
    capabilities: ClassVar[frozenset[str]] = PooledOpenAIAdapter.capabilities | {
        Capability.EMBED,
        Capability.VISION,
    }

    def routed_via_from(self, response: httpx.Response) -> str:
        header = str(self.setting("routed_via_header", DEFAULT_ROUTED_VIA_HEADER))
        value = (response.headers.get(header) or "").strip()
        if not value:
            return self.name
        if str(self.setting("routed_via_format", "platform")) == "full":
            return f"{self.name}:{value}"
        return f"{self.name}:{value.split('/', 1)[0]}"

    def root_url(self) -> str:
        base = self.resolve_base_url()
        return base[: -len("/v1")] if base.endswith("/v1") else base

    async def _check_key(self) -> None:
        self.require_secret("api_key")

    async def ping(self) -> None:
        await self.request("GET", self.root_url() + str(self.setting("ping_path", "/api/ping")))

    async def health(self) -> HealthStatus:
        # Without the unified key the pool is unusable: report NotConfigured without I/O.
        missing = await self.probe(self._check_key)
        if not missing.ok:
            return missing
        status = await self.probe(self.ping, detail="ping ok")
        if not status.ok:
            return status
        # The proxy is up; also prove the unified key works.
        return await self.probe(self.list_models, detail="ping ok, unified key accepted")
