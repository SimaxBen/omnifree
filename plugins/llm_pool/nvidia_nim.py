"""NVIDIA NIM hosted endpoints (build.nvidia.com), OpenAI-compatible ``integrate.api.nvidia.com/v1``."""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter


class NvidiaNIMAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="NVIDIA NIM",
        description="NVIDIA-hosted NIM microservices, OpenAI-compatible API.",
        required_secrets=("api_key",),
        docs_url="https://docs.api.nvidia.com/nim/reference/llm-apis",
    )
