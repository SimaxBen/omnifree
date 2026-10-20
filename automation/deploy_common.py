"""Shared plumbing for automation.deploy: config, state, subprocess runner, compose env files."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "PROJECT_ROOT",
    "DeployError",
    "DeployConfig",
    "DeployState",
    "CommandRunner",
    "SecretLookup",
    "compose_variables",
    "build_env_file",
    "render_cloud_init",
    "make_bundle",
    "dotenv_quote",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPLOY_CONFIG = PROJECT_ROOT / "deploy" / "deploy.yaml"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "deploy_state.json"

SecretLookup = Callable[[str], str | None]


class DeployError(RuntimeError):
    """A deployment step failed; the message says what to fix."""


# ====================================================================== config + state


@dataclass
class DeployConfig:
    deploy: dict[str, Any]
    providers: dict[str, Any]
    root: Path = PROJECT_ROOT

    @classmethod
    def load(cls, deploy_path: Path | None = None, providers_path: Path | None = None,
             root: Path = PROJECT_ROOT) -> DeployConfig:
        deploy_path = deploy_path or root / "deploy" / "deploy.yaml"
        providers_path = providers_path or root / "config" / "providers.yaml"
        deploy = yaml.safe_load(deploy_path.read_text(encoding="utf-8")) or {}
        providers = yaml.safe_load(providers_path.read_text(encoding="utf-8")) or {}
        return cls(deploy, providers, root)

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.deploy.get(name) or {})

    def plugin(self, name: str) -> dict[str, Any]:
        return dict((self.providers.get("plugins") or {}).get(name) or {})

    def plugin_settings(self, name: str) -> dict[str, Any]:
        return dict(self.plugin(name).get("settings") or {})

    def host_alias(self, role: str) -> str:
        """Tailscale MagicDNS name for a role key in providers.yaml ``hosts`` (e.g. oracle_arm)."""
        hosts = self.providers.get("hosts") or {}
        return str(hosts.get(role) or role.replace("_", "-"))

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def remote_dir(self) -> str:
        return str(self.deploy.get("remote_dir") or "/opt/omnifree")

    def llm_chain(self) -> list[str]:
        plugins = self.providers.get("plugins") or {}
        llms = [(int(v.get("priority", 100)), n) for n, v in plugins.items()
                if v.get("kind") == "llm" and v.get("enabled", True)]
        return [n for _, n in sorted(llms)]


class DeployState:
    """Outputs of earlier steps (instance ids/IPs, URLs), persisted as JSON (no secrets)."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH, data: dict[str, Any] | None = None) -> None:
        self.path = path
        self.data: dict[str, Any] = data if data is not None else {"instances": {}, "urls": {}}

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> DeployState:
        if path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def instances(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("instances", {})

    @property
    def urls(self) -> dict[str, str]:
        return self.data.setdefault("urls", {})

    def set_url(self, name: str, url: str) -> None:
        self.urls[name] = url.rstrip("/")
        self.save()


# ====================================================================== subprocess


@dataclass
class CommandRunner:
    """Runs CLI tools (gcloud, npx, koyeb). Echoes commands with secret values masked."""

    secrets_to_mask: list[str] = field(default_factory=list)
    echo: Callable[[str], None] = print
    dry_run: bool = False

    def mask(self, text: str) -> str:
        for s in sorted({s for s in self.secrets_to_mask if s and len(s) >= 4}, key=len, reverse=True):
            text = text.replace(s, "****")
        return text

    def add_secret(self, *values: str | None) -> None:
        self.secrets_to_mask.extend(v for v in values if v)

    @staticmethod
    def resolve(executable: str) -> str:
        found = shutil.which(executable)
        if found is None:
            raise DeployError(f"'{executable}' is not on PATH; install it first (see README.md).")
        return found

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(c) for c in cmd]
        self.echo("$ " + self.mask(" ".join(argv)))
        if self.dry_run:
            return subprocess.CompletedProcess(argv, 0, "", "")
        argv[0] = self.resolve(argv[0])
        full_env = {**os.environ, **(env or {})}
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=full_env, input=input_text,
                              capture_output=capture, text=True, check=False)
        if check and proc.returncode != 0:
            detail = self.mask((proc.stderr or proc.stdout or "").strip()[-2000:]) if capture else ""
            raise DeployError(f"command failed ({proc.returncode}): {self.mask(' '.join(cmd))}\n{detail}")
        return proc


# ====================================================================== compose env files

_COMPOSE_VAR = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")


@dataclass(frozen=True)
class ComposeVar:
    name: str
    required: bool  # ${X:?msg} / ${X?msg}
    has_default: bool  # ${X:-d} / ${X-d}


def compose_variables(text: str) -> dict[str, ComposeVar]:
    """Variables referenced as ``${NAME}``/``${NAME:-d}``/``${NAME:?err}`` in a compose file."""
    found: dict[str, ComposeVar] = {}
    for m in _COMPOSE_VAR.finditer(text):
        name, op = m.group(1), m.group(2) or ""
        prev = found.get(name)
        required = op.endswith("?") or (prev.required if prev else False)
        has_default = op.endswith("-") or (prev.has_default if prev else False)
        found[name] = ComposeVar(name, required, has_default)
    return found


def dotenv_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]*", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_env_file(
    compose_text: str,
    secret_lookup: SecretLookup,
    overrides: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Render a compose ``.env`` from overrides + secrets. Returns (text, optional names left unset).

    Raises DeployError when a variable without default (or marked required) cannot be resolved.
    """
    overrides = dict(overrides or {})
    lines = ["# Generated by automation.deploy. Contains secrets: keep mode 600, never commit."]
    missing_required: list[str] = []
    unset: list[str] = []
    for name, var in sorted(compose_variables(compose_text).items()):
        value = overrides.get(name)
        if value is None:
            value = secret_lookup(name)
        if value:
            lines.append(f"{name}={dotenv_quote(str(value))}")
        elif var.has_default and not var.required:
            unset.append(name)
        else:
            missing_required.append(name)
    for name, value in sorted(overrides.items()):
        if name not in compose_variables(compose_text):
            lines.append(f"{name}={dotenv_quote(str(value))}")
    if missing_required:
        raise DeployError("missing values for compose variables: " + ", ".join(missing_required)
                          + " (store them with `python -m core.vault set NAME` or the browser provisioner)")
    return "\n".join(lines) + "\n", unset


# ====================================================================== cloud-init + bundles


def render_cloud_init(path: Path) -> str:
    """Validate a cloud-config file and return it base64-encoded for OCI ``user_data``."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("#cloud-config"):
        raise DeployError(f"{path} must start with '#cloud-config'")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DeployError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise DeployError(f"{path} is not a cloud-config mapping")
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


_BUNDLE_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git", "data", "dist", ".venv"}
_BUNDLE_EXCLUDE_FILES = {".env", ".env.vault"}


def make_bundle(root: Path, entries: Iterable[str]) -> tuple[bytes, list[str]]:
    """tar.gz of project paths (files or directories). Returns (bytes, entries that were missing)."""
    buf = io.BytesIO()
    missing: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            src = root / entry
            if not src.exists():
                missing.append(entry)
                continue
            paths = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
            for p in paths:
                rel = p.relative_to(root)
                if any(part in _BUNDLE_EXCLUDE_DIRS for part in rel.parts) or p.name in _BUNDLE_EXCLUDE_FILES:
                    continue
                if p.suffix == ".pyc":
                    continue
                tar.add(p, arcname=rel.as_posix(), recursive=False)
    return buf.getvalue(), missing
