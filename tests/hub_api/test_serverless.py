from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.config import get_settings


def test_serverless_app_opens_router_lazily_without_lifespan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIFREE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIFREE_QUOTA_DB", str(tmp_path / "data" / "quota.db"))
    monkeypatch.setenv("OMNIFREE_HUB_API_TOKEN", "t0ken")
    monkeypatch.setenv("OMNIFREE_VAULT_PATH", str(tmp_path / "missing.vault"))
    # serverless.py setdefaults OMNIFREE_STATE_BACKEND=upstash: register it so monkeypatch removes it again,
    # and leave the Upstash secrets unset to exercise the graceful fallback to local state.
    for var in ("OMNIFREE_STATE_BACKEND", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    sys.modules.pop("hub_api.serverless", None)
    get_settings.cache_clear()  # settings are cached process-wide; earlier tests built them without a token
    serverless = importlib.import_module("hub_api.serverless")
    client = TestClient(serverless.app)  # no `with`: the lifespan never runs, like a Vercel function
    assert client.get("/healthz").json()["ok"] is True
    r = client.get("/llm/quotas", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200, r.text
    assert (tmp_path / "data" / "quota.db").is_file()
    assert get_settings().state_backend == "upstash"
    assert serverless.app.state.hub.shared_state is None  # no secrets: per-instance fallback
    get_settings.cache_clear()
