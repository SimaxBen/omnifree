"""Dynamic plugin registry backed by config/providers.yaml.

* Parses and validates every plugin entry (module path, kind, priority, limits,
  secret aliases, settings).
* Imports adapter classes lazily (``importlib``) on first use and caches one
  instance per plugin name.
* Supports runtime enable/disable/reorder/settings changes, persisted back to
  the YAML file atomically (temp file + ``os.replace``).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.events import EventBus, EventType
from core.interfaces import (
    ADAPTER_BASES,
    NotConfigured,
    PluginContext,
    PluginLoadError,
    ProviderAdapter,
    ProviderKind,
    QuotaLimit,
)

__all__ = [
    "CircuitBreakerDefaults",
    "RegistryDefaults",
    "PluginSpec",
    "RegistryConfig",
    "PluginRegistry",
    "SecretLookup",
]

SecretLookup = Callable[[str], str | None]

_MODULE_RE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*:[A-Za-z_][\w]*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

YAML_HEADER = (
    "# OmniFree provider registry. Schema + plugin table: see CONTRACT.md.\n"
    "# This file is rewritten atomically by core.registry when plugins are\n"
    "# enabled/disabled/reordered at runtime: keep documentation in the\n"
    "# 'description' fields (YAML comments are not preserved).\n"
)


class CircuitBreakerDefaults(BaseModel):
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    max_cooldown_s: float = 600.0


class RegistryDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    timeout_s: float = 30.0
    quota_cooldown_s: float = 300.0  # used when a 429 carries no Retry-After
    storage_strategy: str = "most_free"  # most_free | priority
    llm_model_fallback: bool = True  # requested model unavailable everywhere -> other providers' default model
    circuit_breaker: CircuitBreakerDefaults = Field(default_factory=CircuitBreakerDefaults)


class PluginSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    module: str  # "plugins.<group>.<file>:ClassName"
    kind: ProviderKind
    group: str
    enabled: bool = True
    priority: int = 100  # lower = tried first
    description: str = ""
    limits: list[QuotaLimit] = Field(default_factory=list)
    secrets: dict[str, str] = Field(default_factory=dict)  # alias -> vault key (required)
    optional_secrets: dict[str, str] = Field(default_factory=dict)  # alias -> vault key
    settings: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"plugin name {v!r} must match {_NAME_RE.pattern}")
        return v

    @field_validator("module")
    @classmethod
    def _check_module(cls, v: str) -> str:
        if not _MODULE_RE.match(v):
            raise ValueError(f"module {v!r} must look like 'package.module:ClassName'")
        return v

    @field_validator("secrets", "optional_secrets", mode="before")
    @classmethod
    def _normalise_secrets(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, list):  # list form: alias == vault key
            return {str(k): str(k) for k in v}
        return v

    @property
    def module_path(self) -> str:
        return self.module.split(":", 1)[0]

    @property
    def class_name(self) -> str:
        return self.module.split(":", 1)[1]

    def to_yaml_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"name"}, exclude_none=True)
        for key in ("limits", "optional_secrets", "description"):
            if not data.get(key):
                data.pop(key, None)
        return data


class RegistryConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    defaults: RegistryDefaults = Field(default_factory=RegistryDefaults)
    plugins: dict[str, PluginSpec] = Field(default_factory=dict)

    @field_validator("plugins", mode="before")
    @classmethod
    def _inject_names(cls, v: Any) -> Any:
        if not isinstance(v, Mapping):
            raise ValueError("'plugins' must be a mapping of name -> entry")
        out = {}
        for name, entry in v.items():
            entry = dict(entry or {})
            entry.setdefault("name", name)
            if entry["name"] != name:
                raise ValueError(f"plugin key {name!r} != name {entry['name']!r}")
            out[name] = entry
        return out


class PluginRegistry:
    def __init__(
        self,
        path: str | Path,
        *,
        secret_lookup: SecretLookup | None = None,
        bus: EventBus | None = None,
        transport_factory: Callable[[PluginSpec], httpx.AsyncBaseTransport | None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.secret_lookup: SecretLookup = secret_lookup or (lambda _key: None)
        self.bus = bus or EventBus()
        self.transport_factory = transport_factory
        self.config = RegistryConfig()
        self._instances: dict[str, ProviderAdapter] = {}
        self._load_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._instance_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> PluginRegistry:
        reg = cls(path, **kwargs)
        reg.load()
        return reg

    def load(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        try:
            self.config = RegistryConfig.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid {self.path}: {exc}") from exc

    async def reload(self) -> None:
        async with self._lock:
            await self._close_instances()
            self.load()
            self._load_errors.clear()
        self.bus.publish(EventType.REGISTRY_RELOADED, count=len(self.config.plugins))

    # ------------------------------------------------------------------ queries
    @property
    def defaults(self) -> RegistryDefaults:
        return self.config.defaults

    @property
    def specs(self) -> dict[str, PluginSpec]:
        return self.config.plugins

    def spec(self, name: str) -> PluginSpec:
        try:
            return self.config.plugins[name]
        except KeyError:
            raise NotConfigured(f"unknown plugin '{name}'", provider=name) from None

    def list(
        self, kind: ProviderKind | str | None = None, *, enabled_only: bool = False
    ) -> list[PluginSpec]:
        specs = [
            s
            for s in self.config.plugins.values()
            if (kind is None or s.kind == ProviderKind(kind)) and (s.enabled or not enabled_only)
        ]
        return sorted(specs, key=lambda s: (s.priority, s.name))

    def chain(self, kind: ProviderKind | str) -> list[str]:
        """Enabled plugin names of ``kind`` in fallback order (priority asc, then name)."""
        return [s.name for s in self.list(kind, enabled_only=True)]

    def timeout_for(self, name: str) -> float:
        spec = self.spec(name)
        return spec.timeout_s or float(spec.settings.get("timeout_s") or self.defaults.timeout_s)

    def missing_secrets(self, name: str) -> list[str]:
        """Vault keys of required secrets that are not resolvable right now."""
        spec = self.spec(name)
        return [key for key in spec.secrets.values() if not self.secret_lookup(key)]

    def load_error(self, name: str) -> str | None:
        return self._load_errors.get(name)

    # ------------------------------------------------------------------ instantiation
    def load_class(self, spec: PluginSpec) -> type[ProviderAdapter]:
        try:
            module = importlib.import_module(spec.module_path)
        except ImportError as exc:
            raise PluginLoadError(
                f"cannot import {spec.module_path}: {exc}", provider=spec.name
            ) from exc
        cls = getattr(module, spec.class_name, None)
        if cls is None:
            raise PluginLoadError(
                f"{spec.module_path} has no class {spec.class_name}", provider=spec.name
            )
        base = ADAPTER_BASES[spec.kind]
        if not (isinstance(cls, type) and issubclass(cls, base)):
            raise PluginLoadError(
                f"{spec.module} is not a {base.__name__} subclass", provider=spec.name
            )
        if getattr(cls, "__abstractmethods__", None):
            missing = ", ".join(sorted(cls.__abstractmethods__))
            raise PluginLoadError(
                f"{spec.module} is abstract (missing: {missing})", provider=spec.name
            )
        return cls

    def build_context(self, spec: PluginSpec) -> PluginContext:
        secrets: dict[str, str] = {}
        names: dict[str, str] = {}
        for alias, key in {**spec.optional_secrets, **spec.secrets}.items():
            names[alias] = key
            value = self.secret_lookup(key)
            if value:
                secrets[alias] = value
        transport = self.transport_factory(spec) if self.transport_factory else None
        return PluginContext(
            name=spec.name,
            kind=spec.kind,
            settings=dict(spec.settings),
            secrets=secrets,
            secret_names=names,
            limits=tuple(spec.limits),
            timeout_s=self.timeout_for(spec.name),
            http_transport=transport,
        )

    async def get(self, name: str, *, allow_disabled: bool = False) -> ProviderAdapter:
        """Return the (cached) adapter instance for ``name``.

        Raises NotConfigured if the plugin is unknown/disabled and
        PluginLoadError if its class cannot be imported.
        """
        spec = self.spec(name)
        if not spec.enabled and not allow_disabled:
            raise NotConfigured(f"plugin '{name}' is disabled", provider=name)
        inst = self._instances.get(name)
        if inst is not None:
            return inst
        lock = self._instance_locks.setdefault(name, asyncio.Lock())
        async with lock:
            inst = self._instances.get(name)
            if inst is not None:
                return inst
            try:
                cls = self.load_class(spec)
                inst = cls(self.build_context(spec))
            except PluginLoadError as exc:
                self._load_errors[name] = str(exc)
                self.bus.publish(
                    EventType.PLUGIN_LOAD_FAILED, provider=name, kind=spec.kind, error=str(exc)
                )
                raise
            except Exception as exc:
                self._load_errors[name] = f"{type(exc).__name__}: {exc}"
                self.bus.publish(
                    EventType.PLUGIN_LOAD_FAILED, provider=name, kind=spec.kind, error=str(exc)
                )
                raise PluginLoadError(f"cannot construct {spec.module}: {exc}", provider=name) from exc
            self._load_errors.pop(name, None)
            self._instances[name] = inst
        self.bus.publish(EventType.PLUGIN_LOADED, provider=name, kind=spec.kind, module=spec.module)
        return inst

    def class_meta(self, name: str) -> dict[str, Any]:
        """Self-declared PluginMeta/capabilities of the class (imports it)."""
        spec = self.spec(name)
        try:
            cls = self.load_class(spec)
        except PluginLoadError as exc:
            return {"available": False, "error": str(exc)}
        meta = cls.meta.model_dump() if cls.meta else {}
        return {
            "available": True,
            "class": f"{cls.__module__}:{cls.__qualname__}",
            "capabilities": sorted(str(c) for c in cls.capabilities),
            **meta,
        }

    def describe(self, name: str) -> dict[str, Any]:
        spec = self.spec(name)
        return {
            **spec.model_dump(mode="json"),
            "missing_secrets": self.missing_secrets(name),
            "load_error": self._load_errors.get(name),
            "loaded": name in self._instances,
        }

    # ------------------------------------------------------------------ mutations
    async def set_enabled(self, name: str, enabled: bool, *, persist: bool = True) -> PluginSpec:
        async with self._lock:
            spec = self.spec(name)
            spec.enabled = enabled
            if not enabled:
                await self._drop_instance(name)
            if persist:
                self.save()
        self.bus.publish(
            EventType.PLUGIN_ENABLED if enabled else EventType.PLUGIN_DISABLED,
            provider=name,
            kind=spec.kind,
        )
        return spec

    async def set_priority(self, name: str, priority: int, *, persist: bool = True) -> PluginSpec:
        async with self._lock:
            spec = self.spec(name)
            spec.priority = int(priority)
            if persist:
                self.save()
        self.bus.publish(EventType.CHAIN_REORDERED, kind=spec.kind, chain=self.chain(spec.kind))
        return spec

    async def reorder(
        self, kind: ProviderKind | str, names: list[str], *, persist: bool = True
    ) -> list[str]:
        """Put ``names`` first (in that order); the rest keep their relative order.

        Priorities of every plugin of ``kind`` are rewritten as 10, 20, 30...
        """
        kind = ProviderKind(kind)
        async with self._lock:
            all_specs = self.list(kind)
            by_name = {s.name: s for s in all_specs}
            unknown = [n for n in names if n not in by_name]
            if unknown:
                raise NotConfigured(f"not {kind} plugins: {unknown}")
            ordered = [by_name[n] for n in dict.fromkeys(names)]
            ordered += [s for s in all_specs if s.name not in names]
            for idx, spec in enumerate(ordered, start=1):
                spec.priority = idx * 10
            if persist:
                self.save()
        chain = self.chain(kind)
        self.bus.publish(EventType.CHAIN_REORDERED, kind=kind, chain=chain)
        return chain

    async def update_settings(
        self, name: str, patch: Mapping[str, Any], *, persist: bool = True
    ) -> PluginSpec:
        """Shallow-merge ``patch`` into settings (None deletes a key); re-instantiates."""
        async with self._lock:
            spec = self.spec(name)
            for key, value in patch.items():
                if value is None:
                    spec.settings.pop(key, None)
                else:
                    spec.settings[key] = value
            await self._drop_instance(name)
            if persist:
                self.save()
        self.bus.publish(EventType.PLUGIN_UPDATED, provider=name, kind=spec.kind)
        return spec

    def save(self) -> None:
        """Atomically write the current config back to ``self.path``."""
        extra = {k: v for k, v in (self.config.model_extra or {}).items()}
        doc: dict[str, Any] = {
            "version": self.config.version,
            "defaults": self.config.defaults.model_dump(mode="json"),
            **extra,
            "plugins": {n: s.to_yaml_dict() for n, s in self.config.plugins.items()},
        }
        text = YAML_HEADER + yaml.safe_dump(
            doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".providers.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------ teardown
    async def _drop_instance(self, name: str) -> None:
        inst = self._instances.pop(name, None)
        if inst is not None:
            await inst.close()

    async def _close_instances(self) -> None:
        for name in list(self._instances):
            await self._drop_instance(name)

    async def close(self) -> None:
        async with self._lock:
            await self._close_instances()
