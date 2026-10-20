from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core.config import Settings
from core.vault import PASSPHRASE_ENV, Vault, VaultError, main

FAST = {"n": 2**10}


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".env.vault"
    v = Vault.init(path, "pw", kdf_params=FAST)
    v.set("GROQ_API_KEY", "gsk_123")
    v.set("MULTI", "line1\nline2 'quoted'")
    v.save()
    raw = path.read_text(encoding="utf-8")
    assert "gsk_123" not in raw and json.loads(raw)["cipher"] == "AES-256-GCM"
    loaded = Vault.load(path, "pw")
    assert loaded.get("GROQ_API_KEY") == "gsk_123"
    assert loaded.items() == [("GROQ_API_KEY", "gsk_123"), ("MULTI", "line1\nline2 'quoted'")]
    assert loaded.delete("MULTI") and not loaded.delete("MULTI")
    loaded.save()
    assert Vault.load(path, "pw").keys() == ["GROQ_API_KEY"]
    # fresh salt + nonce on every save
    first = json.loads(raw)
    second = json.loads(path.read_text(encoding="utf-8"))
    assert first["kdf"]["salt"] != second["kdf"]["salt"] and first["nonce"] != second["nonce"]


def test_wrong_passphrase_tamper_and_missing(tmp_path: Path) -> None:
    path = tmp_path / ".env.vault"
    v = Vault.init(path, "pw", kdf_params=FAST)
    v.set("A", "1")
    v.save()
    with pytest.raises(VaultError):
        Vault.load(path, "nope")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["kdf"]["n"] = 2**11  # header is authenticated
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(VaultError):
        Vault.load(path, "pw")
    doc["kdf"]["n"] = 2**30  # absurd cost is refused before scrypt runs
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(VaultError, match="out of range"):
        Vault.load(path, "pw")
    assert len(Vault.load(tmp_path / "absent.vault", "pw")) == 0
    with pytest.raises(VaultError):
        Vault.load(tmp_path / "absent.vault", "pw", missing_ok=False)
    with pytest.raises(VaultError):
        Vault.init(path, "pw")
    with pytest.raises(VaultError):
        Vault(tmp_path / "x.vault", passphrase="").save()
    with pytest.raises(VaultError):
        v.set("BAD KEY", "x")


def test_import_export_env(tmp_path: Path) -> None:
    v = Vault(tmp_path / "v", "pw", kdf_params=FAST)
    n = v.import_env("# comment\nexport A=1\nB='two words'\nC=\"x=y\"\n\nnot a pair\n")
    assert n == 3 and v.get("B") == "two words" and v.get("C") == "x=y"
    assert v.export_env() == "A=1\nB='two words'\nC=x=y\n"


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(tmp_path / ".env.vault")
    monkeypatch.setenv(PASSPHRASE_ENV, "cli-pass")
    assert main(["--path", path, "init"]) == 0
    assert main(["--path", path, "set", "GROQ_API_KEY", "gsk_cli"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin\n"))
    assert main(["--path", path, "set", "OTHER"]) == 0
    capsys.readouterr()
    assert main(["--path", path, "get", "OTHER"]) == 0
    assert capsys.readouterr().out == "from-stdin\n"
    assert main(["--path", path, "list"]) == 0
    assert capsys.readouterr().out.split() == ["GROQ_API_KEY", "OTHER"]
    assert main(["--path", path, "export-env"]) == 0
    assert "GROQ_API_KEY=gsk_cli" in capsys.readouterr().out
    assert main(["--path", path, "get", "NOPE"]) == 1
    assert main(["--path", path, "delete", "OTHER"]) == 0
    monkeypatch.setenv(PASSPHRASE_ENV, "wrong")
    assert main(["--path", path, "list"]) == 2


def test_settings_merge_env_over_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v = Vault.init(tmp_path / ".env.vault", "pw", kdf_params=FAST)
    v.set("ONLY_IN_VAULT", "vault-value")
    v.set("IN_BOTH", "vault-value")
    v.save()
    monkeypatch.setenv("IN_BOTH", "env-value")
    monkeypatch.delenv("ONLY_IN_VAULT", raising=False)
    settings = Settings(root_dir=tmp_path, vault_passphrase="pw", _env_file=None)
    assert settings.secret("ONLY_IN_VAULT") == "vault-value"
    assert settings.secret("IN_BOTH") == "env-value"
    assert settings.secret("MISSING") is None
    assert settings.quota_db_path == tmp_path / "data" / "quota.db"
    no_pass = Settings(root_dir=tmp_path, _env_file=None)
    monkeypatch.delenv(PASSPHRASE_ENV, raising=False)
    assert no_pass.secret("ONLY_IN_VAULT") is None
    bad = Settings(root_dir=tmp_path, vault_passphrase="wrong", vault_required=True, _env_file=None)
    with pytest.raises(VaultError):
        bad.secret("ONLY_IN_VAULT")


def test_csv_env_lists_parse_without_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIFREE_CORS_ORIGINS / OMNIFREE_WS_DEFAULT_TYPES are comma-separated, not JSON (deploys set them so)."""
    from core.config import Settings
    from hub_api.settings import HubSettings

    monkeypatch.setenv("OMNIFREE_CORS_ORIGINS", "https://a.dev, https://b.dev")
    monkeypatch.setenv("OMNIFREE_WS_DEFAULT_TYPES", "llm.*,drive.*")
    assert Settings().cors_origins == ["https://a.dev", "https://b.dev"]
    assert HubSettings().ws_default_types == ["llm.*", "drive.*"]
