"""Local Ollama on the Oracle ARM host, the no-quota last resort of the LLM chain.

``settings.base_url`` is the Ollama root (``http://oracle-arm:11434``); chat goes to Ollama's
OpenAI-compatible ``/v1/chat/completions`` and the model list comes from the native
``/api/tags`` (which lists only pulled models, so the router never picks a missing one).
A trailing ``/v1`` in ``base_url`` is tolerated. ``settings.auth: none`` disables the bearer header;
set ``auth: bearer`` plus an ``api_key`` alias when Ollama sits behind an authenticating proxy.
"""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import ModelInfo, PluginMeta, ProviderError
from plugins.llm_pool._base import PooledOpenAIAdapter


class OllamaAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Ollama (local)",
        description="Self-hosted Ollama (CPU) on Oracle ARM; qwen2.5:7b and llama3:8b.",
        required_secrets=(),
        docs_url="https://github.com/ollama/ollama/blob/main/docs/openai.md",
    )

    def root_url(self) -> str:
        base = super().resolve_base_url()
        return base[: -len("/v1")] if base.endswith("/v1") else base

    def resolve_base_url(self) -> str:
        return self.root_url() + "/v1"

    async def list_models(self) -> list[ModelInfo]:
        response = await self.request(
            "GET", self.root_url() + str(self.setting("tags_path", "/api/tags")), headers=self.headers()
        )
        try:
            data = response.json() or {}
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc
        out: list[ModelInfo] = []
        for item in data.get("models") or []:
            model_id = item.get("name") or item.get("model")
            if model_id:
                details = item.get("details") or {}
                out.append(
                    ModelInfo(id=str(model_id), provider=self.name, owned_by=details.get("family") or "ollama")
                )
        return out
