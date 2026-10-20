"""Google AI Studio (Gemini API) through its OpenAI-compatible endpoint
(``https://generativelanguage.googleapis.com/v1beta/openai``).

* The API key is sent as ``Authorization: Bearer`` (accepted by the OpenAI-compatible surface).
* ``/models`` returns ids prefixed with ``models/``; they are stripped so they match
  ``settings.models`` and what ``/chat/completions`` accepts.
* Free-tier exhaustion arrives as 429 ``RESOURCE_EXHAUSTED`` (sometimes wrapped in a JSON list);
  the ``retryDelay`` hint in the body becomes ``QuotaExhausted.retry_after``.
* Models are fetched live (``GET /models``); ``settings.models`` is only the fallback when that call
  fails. Google no longer publishes per-model free-tier limits, and several models (3.1 Pro preview,
  image/Veo/Lyria) have no free tier, so a live id can still answer 429 on the free tier.
"""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import Capability, ModelInfo, PluginMeta
from plugins.llm_pool._base import PooledOpenAIAdapter


class GeminiAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Google Gemini (AI Studio)",
        description="Gemini / Gemma models via the Gemini API OpenAI-compatible endpoint.",
        required_secrets=("api_key",),
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
    )
    capabilities: ClassVar[frozenset[str]] = PooledOpenAIAdapter.capabilities | {
        Capability.EMBED,
        Capability.VISION,
    }

    @staticmethod
    def _strip(model_id: str) -> str:
        return model_id[len("models/"):] if model_id.startswith("models/") else model_id

    def model_for(self, request_model: str | None) -> str:
        return self._strip(super().model_for(request_model))

    async def list_models(self) -> list[ModelInfo]:
        models = await super().list_models()
        return [m.model_copy(update={"id": self._strip(m.id)}) for m in models]
