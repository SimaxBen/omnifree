"""SambaNova Cloud (OpenAI-compatible ``/v1``)."""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter


class SambaNovaAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="SambaNova Cloud",
        description="SambaNova RDU inference, OpenAI-compatible API.",
        required_secrets=("api_key",),
        docs_url="https://docs.sambanova.ai/cloud/docs/get-started/rate-limits",
    )
