"""Groq (LPU cloud) through its OpenAI-compatible API, plus Whisper transcription.

Groq documents ``x-ratelimit-*-requests`` as the per-day budget (RPD) and ``x-ratelimit-*-tokens``
as the per-minute budget (TPM); override with ``settings.rate_limit_header_windows`` if that changes.
Transcription uses ``settings.default_transcription_model`` or the first ``transcription_models`` entry.
"""

from __future__ import annotations

from typing import ClassVar

from core.interfaces import (
    Capability,
    NotConfigured,
    PluginMeta,
    ProviderError,
    TranscriptionRequest,
    TranscriptionResponse,
)
from plugins.llm_pool._base import PooledOpenAIAdapter


class GroqAdapter(PooledOpenAIAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Groq",
        description="Groq LPU inference (OpenAI-compatible) with Whisper transcription.",
        required_secrets=("api_key",),
        docs_url="https://console.groq.com/docs/rate-limits",
    )
    capabilities: ClassVar[frozenset[str]] = PooledOpenAIAdapter.capabilities | {Capability.TRANSCRIBE}
    default_rate_limit_windows: ClassVar[dict[str, str]] = {"requests": "day", "tokens": "minute"}

    def transcription_model(self, requested: str | None) -> str:
        if requested:
            return requested
        model = self.setting("default_transcription_model") or next(
            iter(self.setting("transcription_models") or []), None
        )
        if not model:
            raise NotConfigured("no transcription model configured", provider=self.name)
        return str(model)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        model = self.transcription_model(request.model)

        async def call() -> TranscriptionResponse:
            data: dict[str, str] = {"model": model, "response_format": "json"}
            if request.language:
                data["language"] = request.language
            if request.prompt:
                data["prompt"] = request.prompt
            headers = {k: v for k, v in self.headers().items() if k.lower() != "content-type"}
            response = await self.request(
                "POST",
                self.url("transcriptions_path", "/audio/transcriptions"),
                data=data,
                files={"file": (request.filename, request.audio)},
                headers=headers,
            )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(f"invalid JSON from provider: {exc}", provider=self.name) from exc
            return TranscriptionResponse(model=model, text=str(body.get("text", "")), routed_via=self.routed_via_from(response))

        return await self.guarded(call)
