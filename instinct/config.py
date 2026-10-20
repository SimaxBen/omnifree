"""Instinct settings (env prefix ``INSTINCT_``). Secrets are resolved through core.config."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.config import get_settings

__all__ = ["InstinctSettings", "get_instinct_settings", "resolve_secret"]


class InstinctSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INSTINCT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8090

    # LLM: FreeLLMAPI (OpenAI-compatible) on Oracle ARM; optional fallback (e.g. local Ollama /v1).
    llm_base_url: str = "http://oracle-arm:3001/v1"
    llm_model: str = "auto"
    llm_api_key_name: str = "FREELLMAPI_API_KEY"  # vault/env key; empty = no auth
    llm_routed_via_header: str | None = None  # upstream header exposed by FreeLLMAPI, if any
    llm_timeout_s: float = 90.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 700
    llm_json_mode: bool = False  # send response_format={"type": "json_object"}
    fallback_base_url: str = ""  # e.g. http://localhost:11434/v1 (Ollama); empty disables
    fallback_model: str = "qwen2.5:7b"

    # Agent loop.
    max_steps: int = 20
    hard_max_steps: int = 60
    task_timeout_s: float = 900.0
    history_steps: int = 12  # previous steps summarised in each prompt
    observation_elements: int = 60
    page_text_chars: int = 2000
    extract_max_chars: int = 4000

    # Browser.
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    jpeg_quality: int = Field(default=60, ge=10, le=100)
    action_timeout_ms: int = 15000
    navigation_timeout_ms: int = 30000
    user_agent: str | None = None
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])

    # Service.
    max_concurrent_tasks: int = 1  # the ARM A1 VM also hosts FreeLLMAPI + Ollama
    max_tasks_kept: int = 100
    api_token_name: str = "INSTINCT_API_TOKEN"  # bearer token required by this service

    # hub_api callback (POST {hub_url}/agent/events); empty disables pushing.
    hub_url: str = ""
    hub_token_name: str = "HUB_API_TOKEN"
    callback_timeout_s: float = 10.0
    callback_queue: int = 500


@lru_cache(maxsize=1)
def get_instinct_settings() -> InstinctSettings:
    return InstinctSettings()


def resolve_secret(name: str) -> str | None:
    """Env var first, then the encrypted vault (core.config)."""
    if not name:
        return None
    return get_settings().secret(name)
