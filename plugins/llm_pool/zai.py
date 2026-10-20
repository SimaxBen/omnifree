"""Z.AI (Zhipu GLM, global console) through the OpenAI-compatible ``/api/paas/v4`` endpoint.

The models endpoint is not guaranteed on every Z.AI deployment: a 404 falls back to
``settings.models`` (see :meth:`PooledOpenAIAdapter.list_models`).
"""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter


class ZAIAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Z.AI (GLM)",
        description="Zhipu GLM flash models via Z.AI's OpenAI-compatible API.",
        required_secrets=("api_key",),
        docs_url="https://docs.z.ai/guides/overview/quick-start",
    )
