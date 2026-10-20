"""Encrypted secret vault (.env.vault).

File format (JSON, UTF-8)::

    {"format": "omnifree-vault", "version": 1,
     "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": "<b64>"},
     "cipher": "AES-256-GCM", "nonce": "<b64>", "ciphertext": "<b64>"}

The plaintext is a JSON object ``{KEY: value}``. The key is derived from the
passphrase with scrypt; every save uses a fresh salt and nonce. The header
(everything except the ciphertext) is bound as AES-GCM associated data.

CLI::

    python -m core.vault init
    python -m core.vault set GROQ_API_KEY            # value read from stdin / prompt
    python -m core.vault set GROQ_API_KEY gsk_...    # value as argument (lands in shell history!)
    python -m core.vault get GROQ_API_KEY
    python -m core.vault list
    python -m core.vault delete GROQ_API_KEY
    python -m core.vault export-env                  # KEY='value' lines
    python -m core.vault import-env path/to/.env

The passphrase comes from OMNIFREE_VAULT_PASSPHRASE (or an interactive prompt).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import secrets as pysecrets
import shlex
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

__all__ = ["Vault", "VaultError", "PASSPHRASE_ENV", "DEFAULT_VAULT_PATH", "main"]

PASSPHRASE_ENV = "OMNIFREE_VAULT_PASSPHRASE"
VAULT_PATH_ENV = "OMNIFREE_VAULT_PATH"
DEFAULT_VAULT_PATH = Path(__file__).resolve().parent.parent / ".env.vault"
_FORMAT = "omnifree-vault"
_DEFAULT_KDF = {"n": 2**15, "r": 8, "p": 1}
_MAX_KDF_N = 2**20


class VaultError(Exception):
    """Wrong passphrase, corrupt file, or missing passphrase."""


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _aad(header: dict[str, Any]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Vault:
    def __init__(
        self,
        path: str | Path | None = None,
        passphrase: str | None = None,
        *,
        kdf_params: dict[str, int] | None = None,
    ) -> None:
        self.path = Path(path or os.environ.get(VAULT_PATH_ENV) or DEFAULT_VAULT_PATH)
        self._passphrase = passphrase if passphrase is not None else os.environ.get(PASSPHRASE_ENV)
        self.kdf_params = {**_DEFAULT_KDF, **(kdf_params or {})}
        self._data: dict[str, str] = {}
        self.dirty = False

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        passphrase: str | None = None,
        *,
        missing_ok: bool = True,
        kdf_params: dict[str, int] | None = None,
    ) -> Vault:
        """Open and decrypt a vault. A missing file yields an empty vault if ``missing_ok``."""
        vault = cls(path, passphrase, kdf_params=kdf_params)
        if vault.path.exists():
            vault._decrypt_file()
        elif not missing_ok:
            raise VaultError(f"vault file {vault.path} does not exist (run: python -m core.vault init)")
        return vault

    @classmethod
    def init(
        cls,
        path: str | Path | None = None,
        passphrase: str | None = None,
        *,
        overwrite: bool = False,
        kdf_params: dict[str, int] | None = None,
    ) -> Vault:
        vault = cls(path, passphrase, kdf_params=kdf_params)
        if vault.path.exists() and not overwrite:
            raise VaultError(f"{vault.path} already exists")
        vault.save()
        return vault

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _require_passphrase(self) -> str:
        if not self._passphrase:
            raise VaultError(f"no vault passphrase: set {PASSPHRASE_ENV}")
        return self._passphrase

    def _decrypt_file(self) -> None:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if doc.get("format") != _FORMAT:
                raise VaultError(f"{self.path} is not an omnifree vault")
            kdf = doc["kdf"]
            header = {k: v for k, v in doc.items() if k != "ciphertext"}
            n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
            if kdf.get("name") != "scrypt" or doc.get("cipher") != "AES-256-GCM":
                raise VaultError(f"unsupported vault algorithms in {self.path}")
            # The header is only authenticated after derivation: bound the cost first so a
            # tampered file cannot make scrypt allocate gigabytes (n * r * 128 bytes).
            if not (2 <= n <= _MAX_KDF_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
                raise VaultError(f"vault KDF parameters out of range in {self.path}")
            key = _derive(self._require_passphrase(), _b64d(kdf["salt"]), n, r, p)
            plaintext = AESGCM(key).decrypt(_b64d(doc["nonce"]), _b64d(doc["ciphertext"]), _aad(header))
        except InvalidTag as exc:
            raise VaultError("wrong passphrase or tampered vault") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise VaultError(f"corrupt vault file {self.path}: {exc}") from exc
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict):
            raise VaultError("vault payload is not an object")
        self._data = {str(k): str(v) for k, v in data.items()}
        self.kdf_params = {"n": int(kdf["n"]), "r": int(kdf["r"]), "p": int(kdf["p"])}
        self.dirty = False

    # ------------------------------------------------------------------ mapping API
    def get(self, key: str, default: str | None = None) -> str | None:
        return self._data.get(key, default)

    def set(self, key: str, value: str) -> None:
        if not key or any(c.isspace() for c in key) or "=" in key:
            raise VaultError(f"invalid key {key!r}")
        self._data[key] = str(value)
        self.dirty = True

    def delete(self, key: str) -> bool:
        existed = self._data.pop(key, None) is not None
        self.dirty = self.dirty or existed
        return existed

    def items(self) -> list[tuple[str, str]]:
        return sorted(self._data.items())

    def keys(self) -> list[str]:
        return sorted(self._data)

    def as_dict(self) -> dict[str, str]:
        return dict(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        """Encrypt with a fresh salt/nonce and write atomically."""
        passphrase = self._require_passphrase()
        salt = pysecrets.token_bytes(16)
        nonce = pysecrets.token_bytes(12)
        header: dict[str, Any] = {
            "format": _FORMAT,
            "version": 1,
            "kdf": {"name": "scrypt", **self.kdf_params, "salt": _b64e(salt)},
            "cipher": "AES-256-GCM",
            "nonce": _b64e(nonce),
        }
        key = _derive(passphrase, salt, self.kdf_params["n"], self.kdf_params["r"], self.kdf_params["p"])
        plaintext = json.dumps(self._data, sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(header))
        doc = {**header, "ciphertext": _b64e(ciphertext)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".vault.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self.dirty = False

    def export_env(self) -> str:
        return "".join(f"{k}={shlex.quote(v)}\n" for k, v in self.items())

    def import_env(self, text: str) -> int:
        count = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            self.set(key.strip(), value)
            count += 1
        return count


# ---------------------------------------------------------------------- CLI


def _passphrase_for_cli(confirm: bool = False) -> str:
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env
    if not sys.stdin.isatty():
        raise VaultError(f"set {PASSPHRASE_ENV} (no TTY for a passphrase prompt)")
    first = getpass.getpass("Vault passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != first:
        raise VaultError("passphrases do not match")
    return first


def _read_value(key: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {key}: ")
    return sys.stdin.read().rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.vault", description="OmniFree encrypted vault")
    parser.add_argument("--path", help=f"vault file (default: ${VAULT_PATH_ENV} or {DEFAULT_VAULT_PATH.name})")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create an empty vault")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing vault")
    p_set = sub.add_parser("set", help="set KEY (value from argument, stdin or prompt)")
    p_set.add_argument("key")
    p_set.add_argument("value", nargs="?")
    p_get = sub.add_parser("get", help="print the value of KEY")
    p_get.add_argument("key")
    p_del = sub.add_parser("delete", help="remove KEY")
    p_del.add_argument("key")
    sub.add_parser("list", help="list key names")
    sub.add_parser("export-env", help="print KEY='value' lines")
    p_imp = sub.add_parser("import-env", help="import KEY=value lines from a .env file")
    p_imp.add_argument("file")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "init":
            Vault.init(args.path, _passphrase_for_cli(confirm=True), overwrite=args.force)
            print(f"initialised {Vault(args.path).path}", file=sys.stderr)
            return 0
        vault = Vault.load(args.path, _passphrase_for_cli(), missing_ok=args.cmd in ("set", "import-env"))
        if args.cmd == "set":
            value = args.value if args.value is not None else _read_value(args.key)
            vault.set(args.key, value)
            vault.save()
            print(f"set {args.key}", file=sys.stderr)
        elif args.cmd == "get":
            value = vault.get(args.key)
            if value is None:
                print(f"{args.key} not found", file=sys.stderr)
                return 1
            print(value)
        elif args.cmd == "delete":
            if not vault.delete(args.key):
                print(f"{args.key} not found", file=sys.stderr)
                return 1
            vault.save()
        elif args.cmd == "list":
            for key in vault.keys():
                print(key)
        elif args.cmd == "export-env":
            sys.stdout.write(vault.export_env())
        elif args.cmd == "import-env":
            count = vault.import_env(Path(args.file).read_text(encoding="utf-8"))
            vault.save()
            print(f"imported {count} keys", file=sys.stderr)
    except VaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
