"""Cerebras inference (OpenAI-compatible). Rate-limit headers carry explicit windows
(``x-ratelimit-remaining-requests-day``, ``...-tokens-minute``), parsed into ``quota()``."""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter


class CerebrasAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Cerebras",
        description="Cerebras wafer-scale inference, OpenAI-compatible API.",
        required_secrets=("api_key",),
        docs_url="https://inference-docs.cerebras.ai/support/rate-limits",
    )
