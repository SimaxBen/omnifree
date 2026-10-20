"""Application settings: environment (+ optional .env) merged with the encrypted vault.

Precedence for secrets (``Settings.secret(name)``):
    1. process environment variable ``name``
    2. the encrypted vault (.env.vault), decrypted with OMNIFREE_VAULT_PASSPHRASE
Plain settings use the ``OMNIFREE_`` env prefix (e.g. OMNIFREE_DATA_DIR).
"""

from __future__ import annotations

import logging
import os
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from core.interfaces import NotConfigured
from core.vault import Vault, VaultError

__all__ = ["PROJECT_ROOT", "Settings", "get_settings"]

log = logging.getLogger("omnifree.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNIFREE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    root_dir: Path = PROJECT_ROOT
    providers_file: Path = Path("config/providers.yaml")
    data_dir: Path = Path("data")
    quota_db: Path = Path("data/quota.db")
    vault_path: Path = Path(".env.vault")
    vault_passphrase: SecretStr | None = None  # OMNIFREE_VAULT_PASSPHRASE
    vault_required: bool = False  # fail hard when the vault cannot be decrypted

    environment: str = "development"  # development | production
    log_level: str = "INFO"
    hub_host: str = "0.0.0.0"
    hub_port: int = 8080
    hub_api_token: SecretStr | None = None  # bearer token for hub_api (or vault key HUB_API_TOKEN)
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])  # CSV in env
    request_timeout_s: float = 30.0
    health_interval_s: float = 120.0

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # ------------------------------------------------------------------ paths
    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else (self.root_dir / path)

    @property
    def providers_path(self) -> Path:
        return self.resolve(self.providers_file)

    @property
    def data_path(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def quota_db_path(self) -> Path:
        return self.resolve(self.quota_db)

    @property
    def vault_file(self) -> Path:
        return self.resolve(self.vault_path)

    # ------------------------------------------------------------------ secrets
    @cached_property
    def vault(self) -> Vault | None:
        """Decrypted vault, or None when no vault/passphrase is available."""
        passphrase = self.vault_passphrase.get_secret_value() if self.vault_passphrase else None
        if not self.vault_file.exists():
            return None
        if not passphrase:
            if self.vault_required:
                raise VaultError("vault exists but OMNIFREE_VAULT_PASSPHRASE is not set")
            log.warning("vault %s present but no passphrase; secrets come from env only", self.vault_file)
            return None
        try:
            return Vault.load(self.vault_file, passphrase)
        except VaultError:
            if self.vault_required:
                raise
            log.error("could not decrypt vault %s; secrets come from env only", self.vault_file)
            return None

    def secret(self, name: str) -> str | None:
        """Env var ``name`` first, then the vault. Empty strings count as missing."""
        value = os.environ.get(name)
        if value:
            return value
        vault = self.vault
        if vault is not None:
            return vault.get(name) or None
        return None

    def require_secret(self, name: str) -> str:
        value = self.secret(name)
        if not value:
            raise NotConfigured(f"secret {name} is not set (env or vault)")
        return value

    def hub_token(self) -> str | None:
        if self.hub_api_token:
            return self.hub_api_token.get_secret_value()
        return self.secret("HUB_API_TOKEN")

    def reload_vault(self) -> None:
        self.__dict__.pop("vault", None)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
