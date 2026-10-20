"""hub_api-specific settings (env prefix ``OMNIFREE_``), layered on top of core.config.Settings.

core.config.Settings ignores unknown env vars, so the hub keeps its own small
settings model for knobs that only the control plane needs. Secrets are never
stored here: they are resolved through ``core.config.Settings.secret``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["HubSettings", "INSTINCT_TOKEN_KEY", "HUB_TOKEN_FALLBACK_KEY"]

# Vault/env key holding the bearer token hub_api presents to the Instinct service.
INSTINCT_TOKEN_KEY = "INSTINCT_API_TOKEN"
# Alternative name for the hub bearer token (task spec wording); core.config's
# Settings.hub_token() (OMNIFREE_HUB_API_TOKEN / vault HUB_API_TOKEN) wins when set.
HUB_TOKEN_FALLBACK_KEY = "OMNIFREE_HUB_TOKEN"


class HubSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNIFREE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Instinct agent service (Oracle ARM, Tailscale-only).
    instinct_url: str = "http://oracle-arm:8090"
    instinct_timeout_s: float = 30.0

    # Drive: Cloud Run caps HTTP/1 request bodies at 32 MiB.
    max_upload_bytes: int = 32 * 1024 * 1024

    # DB explorer.
    db_max_rows: int = 1000
    db_timeout_s: float = 30.0

    # Network page: name of the compute plugin listing Tailscale nodes.
    tailscale_provider: str = "tailscale"

    # WebSocket event stream.
    ws_default_types: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])  # CSV in env
    ws_max_replay: int = 200
    ws_ping_interval_s: float = 25.0

    # Agent screenshots kept in memory per task (latest frame served over HTTP).
    agent_task_cache: int = 100

    # Allow PATCH /plugins/{name}/settings to change endpoint/TLS keys (base_url, verify_tls ...).
    allow_endpoint_settings: bool = False

    # Run a background health sweep every core Settings.health_interval_s (0 disables).
    background_health: bool = True

    @field_validator("ws_default_types", mode="before")
    @classmethod
    def _split_types(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v
