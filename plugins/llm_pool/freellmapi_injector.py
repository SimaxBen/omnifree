"""Push OmniFree vault keys into a self-hosted FreeLLMAPI.

What FreeLLMAPI actually supports (verified in github.com/tashfeenahmed/freellmapi, 2026-10):

1. **Declarative startup config** (primary mechanism, used here by ``render``).
   ``FREEAPI_CONFIG_PATH`` (file) or ``FREEAPI_CONFIG_JSON`` (inline, takes precedence) is applied
   idempotently after migrations on *every boot* (``server/src/services/declarative-config.ts``).
   Strict top-level schema: ``admin {email, password}``, ``license``,
   ``keys [{platform, key, label?, baseUrl?, enabled?}]``,
   ``customProviders [{baseUrl, apiKey, label?, models: [str | {model, displayName?, supportsTools?, ...}]}]``,
   ``models [...]``, ``fallback [...]``, ``routing {strategy, weights?, keySelectionStrategy?}``.
   Catalog keys are matched on ``platform + label`` (re-applied keys are updated, never duplicated);
   custom providers are matched on ``baseUrl``. Nothing absent from the file is deleted. ``admin``
   only creates the first dashboard account while none exists. The file holds plaintext secrets.
2. **Dashboard admin REST API** (used by ``push`` for a running instance without restart), the same
   calls as the official CLI ``npx freellmapi keys ...`` (``cli/src/keys.ts``):
   ``GET /api/keys/providers``, ``GET /api/keys``, ``POST /api/keys {platform, key}``,
   ``DELETE /api/keys/{id}``, ``POST /api/health/check/{id}``, authenticated with
   ``Authorization: Bearer <dashboard session token>`` (vault key ``FREELLMAPI_DASHBOARD_TOKEN``,
   copied from the browser's ``localStorage.freellmapi_dashboard_token`` after sign-in; the unified
   ``freellmapi-...`` API key is *not* accepted there). Custom endpoints cannot be created this way.
3. There is no per-provider env var for upstream keys (``.env`` only carries server settings such as
   ``ENCRYPTION_KEY``, ``PORT``, ``HOST_BIND``), so ``render`` also writes that ``.env``.

Platform ids are FreeLLMAPI's own (``server/src/providers``): ``groq``, ``google``, ``mistral``,
``openrouter``, ``cerebras``, ``nvidia``, ``cohere``, ``cloudflare`` (key format
``account_id:api_token``), ``zhipu`` (defaults to the *domestic* open.bigmodel.cn console). SambaNova
is not a built-in platform, and a global Z.AI key needs ``api.z.ai``, so both are injected as
``customProviders`` built from the OmniFree plugin's ``base_url``/``models``. The local Ollama
container is injected as a custom provider too (FreeLLMAPI's ``ollama`` platform is Ollama Cloud).

Optional overrides live in providers.yaml under ``plugins.freellmapi.settings.injector``:
``label`` (default ``omnifree``), ``zai_platform`` (``custom`` | ``zhipu``), ``include_ollama`` (true),
``ollama_url`` (``http://ollama:11434/v1``), ``routing_strategy`` (e.g. ``balanced``),
``config_path_in_container`` (``/run/omnifree/freellmapi.config.json``), ``host_bind``.

Extra vault keys read here (beyond the per-plugin keys of providers.yaml):
``FREELLMAPI_ENCRYPTION_KEY`` (64 hex chars, required for ``render --env-out``),
``FREELLMAPI_ADMIN_EMAIL`` / ``FREELLMAPI_ADMIN_PASSWORD`` (optional first account),
``FREELLMAPI_DASHBOARD_TOKEN`` (``push`` only), ``FREELLMAPI_LICENSE`` (optional).

CLI (run from the project root, ``OMNIFREE_VAULT_PASSPHRASE`` set)::

    python -m plugins.llm_pool.freellmapi_injector plan
    python -m plugins.llm_pool.freellmapi_injector render \
        --out deploy/oracle-arm/secrets/freellmapi.config.json --env-out deploy/oracle-arm/.env \
        --host-bind 100.x.y.z
    python -m plugins.llm_pool.freellmapi_injector push --url http://oracle-arm:3001 [--replace]
    python -m plugins.llm_pool.freellmapi_injector genkey      # store a new FREELLMAPI_ENCRYPTION_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets as pysecrets
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.interfaces import (
    NotConfigured,
    ProviderError,
    raise_for_response,
    translate_transport_error,
)
from core.registry import PluginRegistry

__all__ = [
    "CatalogSource",
    "CustomSource",
    "CATALOG_SOURCES",
    "CUSTOM_SOURCES",
    "InjectionPlan",
    "build_plan",
    "render_env",
    "write_private_file",
    "FreeLLMAPIAdminClient",
    "PushResult",
    "push_keys",
    "main",
]

SecretLookup = Callable[[str], "str | None"]

DEFAULT_LABEL = "omnifree"
DEFAULT_CONTAINER_CONFIG_PATH = "/run/omnifree/freellmapi.config.json"
DEFAULT_OLLAMA_URL = "http://ollama:11434/v1"
ENCRYPTION_KEY_VAULT = "FREELLMAPI_ENCRYPTION_KEY"
DASHBOARD_TOKEN_VAULT = "FREELLMAPI_DASHBOARD_TOKEN"
ADMIN_EMAIL_VAULT = "FREELLMAPI_ADMIN_EMAIL"
ADMIN_PASSWORD_VAULT = "FREELLMAPI_ADMIN_PASSWORD"
LICENSE_VAULT = "FREELLMAPI_LICENSE"


@dataclass(frozen=True)
class CatalogSource:
    """A FreeLLMAPI built-in platform fed from one OmniFree plugin's secrets."""

    platform: str
    plugin: str
    aliases: tuple[str, ...] = ("api_key",)
    key_format: str = "{api_key}"


@dataclass(frozen=True)
class CustomSource:
    """An OpenAI-compatible endpoint injected as a FreeLLMAPI ``customProviders`` entry."""

    plugin: str
    label: str
    alias: str | None = "api_key"


CATALOG_SOURCES: tuple[CatalogSource, ...] = (
    CatalogSource("groq", "groq"),
    CatalogSource("google", "gemini"),
    CatalogSource("mistral", "mistral"),
    CatalogSource("openrouter", "openrouter"),
    CatalogSource("cerebras", "cerebras"),
    CatalogSource("nvidia", "nvidia_nim"),
    CatalogSource("cohere", "cohere"),
    CatalogSource("cloudflare", "cloudflare_ai", ("account_id", "api_key"), "{account_id}:{api_key}"),
)

CUSTOM_SOURCES: tuple[CustomSource, ...] = (
    CustomSource("sambanova", "SambaNova"),
    CustomSource("zai", "Z.AI"),
)


@dataclass
class InjectionPlan:
    keys: list[dict[str, Any]] = field(default_factory=list)
    custom_providers: list[dict[str, Any]] = field(default_factory=list)
    admin: dict[str, str] | None = None
    license: str | None = None
    routing: dict[str, Any] | None = None
    skipped: list[str] = field(default_factory=list)

    def to_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if self.admin:
            config["admin"] = dict(self.admin)
        if self.license:
            config["license"] = self.license
        if self.keys:
            config["keys"] = [dict(k) for k in self.keys]
        if self.custom_providers:
            config["customProviders"] = [dict(c) for c in self.custom_providers]
        if self.routing:
            config["routing"] = dict(self.routing)
        return config

    def summary(self) -> dict[str, Any]:
        """Secret-free description for logs/CLI output."""
        return {
            "keys": [k["platform"] for k in self.keys],
            "customProviders": [c["baseUrl"] for c in self.custom_providers],
            "admin": bool(self.admin),
            "license": bool(self.license),
            "routing": self.routing,
            "skipped": list(self.skipped),
        }


def _injector_settings(registry: PluginRegistry, plugin: str = "freellmapi") -> dict[str, Any]:
    if plugin not in registry.specs:
        return {}
    value = registry.specs[plugin].settings.get("injector") or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _resolve_alias(registry: PluginRegistry, plugin: str, alias: str, lookup: SecretLookup) -> str | None:
    spec = registry.specs[plugin]
    vault_key = spec.secrets.get(alias) or spec.optional_secrets.get(alias)
    if not vault_key:
        return None
    value = lookup(vault_key)
    return value.strip() if value and value.strip() else None


def _model_entries(models: Sequence[Any]) -> list[Any]:
    return [str(m) for m in models if str(m).strip()]


def build_plan(
    registry: PluginRegistry,
    lookup: SecretLookup,
    *,
    only_enabled: bool = False,
    overrides: Mapping[str, Any] | None = None,
) -> InjectionPlan:
    """Resolve vault keys (through providers.yaml aliases) into a FreeLLMAPI declarative config."""
    opts = {**_injector_settings(registry), **(overrides or {})}
    label = str(opts.get("label") or DEFAULT_LABEL)
    zai_platform = str(opts.get("zai_platform") or "custom")
    plan = InjectionPlan()

    def usable(plugin: str) -> bool:
        if plugin not in registry.specs:
            plan.skipped.append(f"{plugin}: not in providers.yaml")
            return False
        if only_enabled and not registry.specs[plugin].enabled:
            plan.skipped.append(f"{plugin}: disabled")
            return False
        return True

    catalog = list(CATALOG_SOURCES)
    customs = list(CUSTOM_SOURCES)
    if zai_platform == "zhipu":
        catalog.append(CatalogSource("zhipu", "zai"))
        customs = [c for c in customs if c.plugin != "zai"]

    for source in catalog:
        if not usable(source.plugin):
            continue
        values: dict[str, str] = {}
        for alias in source.aliases:
            value = _resolve_alias(registry, source.plugin, alias, lookup)
            if value is None:
                break
            values[alias] = value
        if len(values) != len(source.aliases):
            missing = [a for a in source.aliases if a not in values]
            plan.skipped.append(f"{source.plugin}: missing secret(s) {', '.join(missing)}")
            continue
        plan.keys.append(
            {"platform": source.platform, "key": source.key_format.format(**values), "label": label, "enabled": True}
        )

    for custom in customs:
        if not usable(custom.plugin):
            continue
        spec = registry.specs[custom.plugin]
        base_url = str(spec.settings.get("base_url") or "").rstrip("/")
        if not base_url:
            plan.skipped.append(f"{custom.plugin}: settings.base_url unset")
            continue
        key = _resolve_alias(registry, custom.plugin, custom.alias, lookup) if custom.alias else None
        if not key:
            plan.skipped.append(f"{custom.plugin}: missing secret {custom.alias}")
            continue
        plan.custom_providers.append(
            {
                "baseUrl": base_url,
                "apiKey": key,
                "label": custom.label,
                "models": _model_entries(spec.settings.get("models") or []),
            }
        )

    if opts.get("include_ollama", True) and "ollama" in registry.specs and usable("ollama"):
        models = registry.specs["ollama"].settings.get("models") or []
        plan.custom_providers.append(
            {
                "baseUrl": str(opts.get("ollama_url") or DEFAULT_OLLAMA_URL).rstrip("/"),
                # FreeLLMAPI refuses custom providers without a key; Ollama ignores it.
                "apiKey": "ollama",
                "label": "Ollama (local)",
                "models": _model_entries(models),
            }
        )

    email, password = lookup(ADMIN_EMAIL_VAULT), lookup(ADMIN_PASSWORD_VAULT)
    if email and password:
        if len(password) < 8:
            raise NotConfigured(f"{ADMIN_PASSWORD_VAULT} must be at least 8 characters", provider="freellmapi")
        plan.admin = {"email": email, "password": password}
    plan.license = lookup(LICENSE_VAULT) or None
    if opts.get("routing_strategy"):
        plan.routing = {"strategy": str(opts["routing_strategy"])}
    return plan


def render_env(
    lookup: SecretLookup,
    *,
    host_bind: str,
    port: int = 3001,
    config_path_in_container: str = DEFAULT_CONTAINER_CONFIG_PATH,
) -> str:
    """Build the docker-compose ``.env`` for deploy/oracle-arm (server settings only)."""
    key = (lookup(ENCRYPTION_KEY_VAULT) or "").strip()
    if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
        raise NotConfigured(
            f"{ENCRYPTION_KEY_VAULT} must be 64 hex chars (run: python -m plugins.llm_pool.freellmapi_injector genkey)",
            provider="freellmapi",
        )
    if not host_bind:
        raise NotConfigured("host_bind (Tailscale IP of the Oracle ARM host) is required", provider="freellmapi")
    lines = [
        "# Generated by plugins/llm_pool/freellmapi_injector.py. Contains secrets: chmod 600, never commit.",
        "NODE_ENV=production",
        f"ENCRYPTION_KEY={key}",
        f"PORT={int(port)}",
        f"HOST_BIND={host_bind}",
        f"FREEAPI_CONFIG_PATH={config_path_in_container}",
        "",
    ]
    return "\n".join(lines)


def write_private_file(path: str | Path, text: str) -> Path:
    """Atomic write with 0600 permissions (best effort on Windows)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


# --------------------------------------------------------------------------- admin REST API


class FreeLLMAPIAdminClient:
    """Minimal client for FreeLLMAPI's dashboard key-management API (same calls as its CLI)."""

    provider = "freellmapi"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        if not token:
            raise NotConfigured(f"{DASHBOARD_TOKEN_VAULT} is not set", provider=self.provider)
        self.root = root
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def __aenter__(self) -> FreeLLMAPIAdminClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, self.root + path, json=body)
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.provider) from exc
        if not response.is_success:
            # never echo bodies: they may reflect submitted keys
            raise_for_response(httpx.Response(response.status_code, headers=response.headers), self.provider)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from {path}", provider=self.provider) from exc

    async def providers(self) -> dict[str, bool]:
        data = await self._call("GET", "/api/keys/providers") or {}
        return {str(p["platform"]): bool(p.get("keyless")) for p in data.get("providers") or [] if p.get("platform")}

    async def list_keys(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/api/keys")
        return list(data or [])

    async def add_key(self, platform: str, key: str) -> int:
        data = await self._call("POST", "/api/keys", {"platform": platform, "key": key.strip()}) or {}
        key_id = data.get("id")
        if not isinstance(key_id, int) or key_id <= 0:
            raise ProviderError("POST /api/keys returned no id", provider=self.provider)
        return key_id

    async def delete_key(self, key_id: int) -> None:
        await self._call("DELETE", f"/api/keys/{int(key_id)}")

    async def check_key(self, key_id: int) -> str:
        data = await self._call("POST", f"/api/health/check/{int(key_id)}") or {}
        status = str(data.get("status") or "unknown")
        return status if status in {"healthy", "invalid", "unknown", "rate_limited", "error"} else "unknown"


@dataclass
class PushResult:
    platform: str
    action: str  # added | replaced | exists | unsupported | skipped
    key_id: int | None = None
    status: str | None = None
    detail: str = ""


async def push_keys(
    client: FreeLLMAPIAdminClient,
    plan: InjectionPlan,
    *,
    replace: bool = False,
    check: bool = True,
) -> list[PushResult]:
    """Add the plan's catalog keys to a running FreeLLMAPI.

    Without ``replace`` a platform that already has a key is left alone (the API exposes no key
    material to compare); with ``replace`` its existing catalog rows are deleted first.
    Custom providers are reported as ``skipped`` (the API cannot create them; use ``render``).
    """
    supported = await client.providers()
    existing: dict[str, list[int]] = {}
    for row in await client.list_keys():
        existing.setdefault(str(row.get("platform")), []).append(int(row["id"]))
    results: list[PushResult] = []
    for entry in plan.keys:
        platform = entry["platform"]
        if supported and platform not in supported:
            results.append(PushResult(platform, "unsupported", detail="platform not offered by this FreeLLMAPI build"))
            continue
        action = "added"
        if existing.get(platform):
            if not replace:
                results.append(PushResult(platform, "exists", key_id=existing[platform][0]))
                continue
            for key_id in existing[platform]:
                await client.delete_key(key_id)
            action = "replaced"
        key_id = await client.add_key(platform, entry["key"])
        status = await client.check_key(key_id) if check else None
        results.append(PushResult(platform, action, key_id=key_id, status=status))
    for custom in plan.custom_providers:
        results.append(
            PushResult("custom", "skipped", detail=f"{custom['baseUrl']}: custom endpoints need the config file (render)")
        )
    return results


# --------------------------------------------------------------------------- CLI


def _load_registry() -> tuple[PluginRegistry, SecretLookup, Any]:
    from core.config import get_settings

    settings = get_settings()
    registry = PluginRegistry.from_file(settings.providers_path, secret_lookup=settings.secret)
    return registry, settings.secret, settings


def _genkey(settings: Any, force: bool) -> int:
    from core.vault import Vault

    vault = Vault.load(settings.vault_file, missing_ok=False)
    if vault.get(ENCRYPTION_KEY_VAULT) and not force:
        print(f"{ENCRYPTION_KEY_VAULT} already set (use --force to rotate; rotating orphans stored keys)")
        return 1
    vault.set(ENCRYPTION_KEY_VAULT, pysecrets.token_hex(32))
    vault.save()
    print(f"stored new {ENCRYPTION_KEY_VAULT} in {settings.vault_file}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plugins.llm_pool.freellmapi_injector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="show which keys would be injected (no secrets printed)")
    p_render = sub.add_parser("render", help="write the declarative config (and optionally the compose .env)")
    p_render.add_argument("--out", required=True, help="path of freellmapi.config.json")
    p_render.add_argument("--env-out", help="path of the docker-compose .env")
    p_render.add_argument("--host-bind", help="Tailscale IP to publish ports on (or settings.injector.host_bind)")
    p_push = sub.add_parser("push", help="add keys to a running instance through the dashboard API")
    p_push.add_argument("--url", help="FreeLLMAPI root (default: freellmapi settings.base_url)")
    p_push.add_argument("--replace", action="store_true", help="delete existing rows of each platform first")
    p_push.add_argument("--no-check", action="store_true", help="skip the per-key health check")
    p_gen = sub.add_parser("genkey", help=f"generate {ENCRYPTION_KEY_VAULT} into the vault")
    p_gen.add_argument("--force", action="store_true")
    for p in (p_plan, p_render, p_push):
        p.add_argument("--only-enabled", action="store_true", help="skip plugins disabled in providers.yaml")
    args = parser.parse_args(argv)

    registry, lookup, settings = _load_registry()
    if args.cmd == "genkey":
        return _genkey(settings, args.force)
    try:
        plan = build_plan(registry, lookup, only_enabled=args.only_enabled)
        if args.cmd == "plan":
            print(json.dumps(plan.summary(), indent=2))
            return 0
        if args.cmd == "render":
            opts = _injector_settings(registry)
            write_private_file(args.out, json.dumps(plan.to_config(), indent=2) + "\n")
            print(f"wrote {args.out}: {json.dumps(plan.summary())}")
            if args.env_out:
                host_bind = args.host_bind or str(opts.get("host_bind") or "")
                port = int(registry.specs["freellmapi"].settings.get("port", 3001)) if "freellmapi" in registry.specs else 3001
                env = render_env(
                    lookup,
                    host_bind=host_bind,
                    port=port,
                    config_path_in_container=str(opts.get("config_path_in_container") or DEFAULT_CONTAINER_CONFIG_PATH),
                )
                write_private_file(args.env_out, env)
                print(f"wrote {args.env_out}")
            return 0
        url = args.url or str(registry.specs["freellmapi"].settings.get("base_url", ""))
        token = lookup(DASHBOARD_TOKEN_VAULT) or ""

        async def run() -> list[PushResult]:
            async with FreeLLMAPIAdminClient(url, token) as client:
                return await push_keys(client, plan, replace=args.replace, check=not args.no_check)

        for result in asyncio.run(run()):
            print(f"{result.platform:12} {result.action:11} id={result.key_id} status={result.status} {result.detail}")
        return 0
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
