from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.interfaces import PluginContext, ProviderKind, QuotaLimit
from tests.plugins.clouds.helpers import Recorder


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_pem(rsa_key: rsa.RSAPrivateKey) -> str:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="session")
def service_account(rsa_pem: str) -> dict[str, Any]:
    return {
        "type": "service_account",
        "project_id": "omni-proj",
        "private_key_id": "kid123",
        "private_key": rsa_pem,
        "client_email": "omnifree@omni-proj.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture
def make_ctx() -> Callable[..., PluginContext]:
    def _make(
        name: str,
        kind: ProviderKind,
        *,
        settings: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        limits: list[QuotaLimit] | None = None,
        handler: Recorder | None = None,
    ) -> PluginContext:
        return PluginContext(
            name=name,
            kind=kind,
            settings=settings or {},
            secrets=secrets or {},
            secret_names={k: k.upper() for k in (secrets or {})},
            limits=tuple(limits or ()),
            timeout_s=5.0,
            http_transport=handler.transport if handler else None,
        )

    return _make
