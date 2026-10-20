"""OmniFree port definitions (hexagonal architecture).

Every provider plugin implements exactly one of the adapter ABCs below
(LLMAdapter, StorageAdapter, DBAdapter, ComputeAdapter, DNSAdapter,
CronAdapter), all of which derive from :class:`ProviderAdapter`.

Optional operations are never stubbed with ``NotImplementedError``: an
adapter advertises them through its ``capabilities`` class attribute and the
router only calls an optional method on adapters that declare it. The base
implementation of an optional method raises :class:`CapabilityNotSupported`
so a misuse is reported as a typed provider error.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ProviderKind",
    "Capability",
    "QuotaLimit",
    "PluginMeta",
    "PluginContext",
    "HealthStatus",
    "QuotaInfo",
    "ChatMessage",
    "ChatRequest",
    "Usage",
    "ChatResponse",
    "ChatChunk",
    "ModelInfo",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "RerankRequest",
    "RerankResult",
    "RerankResponse",
    "TranscriptionRequest",
    "TranscriptionResponse",
    "StoredObject",
    "DirectUpload",
    "ColumnInfo",
    "TableInfo",
    "QueryResult",
    "VMInstance",
    "DNSRecord",
    "DNSStatus",
    "DNSQueryLogEntry",
    "CronJob",
    "ProviderError",
    "QuotaExhausted",
    "ProviderUnavailable",
    "ProviderTimeout",
    "NotConfigured",
    "ProviderAuthError",
    "PluginLoadError",
    "CapabilityNotSupported",
    "NoProviderAvailable",
    "ProviderAdapter",
    "LLMAdapter",
    "StorageAdapter",
    "DBAdapter",
    "ComputeAdapter",
    "DNSAdapter",
    "CronAdapter",
    "ADAPTER_BASES",
    "adapter_catalog",
    "raise_for_response",
    "translate_transport_error",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- kinds


class ProviderKind(StrEnum):
    LLM = "llm"
    STORAGE = "storage"
    DB = "db"
    COMPUTE = "compute"
    DNS = "dns"
    CRON = "cron"


class Capability(StrEnum):
    # LLM
    CHAT = "chat"
    STREAM = "stream"
    LIST_MODELS = "list_models"
    EMBED = "embed"
    RERANK = "rerank"
    TRANSCRIBE = "transcribe"
    TOOLS = "tools"
    VISION = "vision"
    # Storage
    PRESIGN = "presign"
    DIRECT_UPLOAD = "direct_upload"  # upload_target(): client uploads straight to the backend
    # DB
    SQL = "sql"  # query()/execute() accept SQL text (otherwise a provider-native command/JSON)
    # Compute
    POWER = "power"  # start()/stop() are supported
    # DNS
    DNS_RECORDS = "dns_records"
    # Cron
    CRON_TRIGGER = "cron_trigger"


# --------------------------------------------------------------------------- plugin plumbing


class QuotaLimit(BaseModel):
    """One configured free-tier limit, e.g. 1000 requests per day."""

    model_config = ConfigDict(frozen=True)

    metric: str  # requests | tokens | neurons | bytes | calls | messages | minutes | rows_read ...
    window: Literal["minute", "hour", "day", "month", "total"]
    limit: float


class PluginMeta(BaseModel):
    """Static, self-declared metadata of an adapter class (``ClassVar meta``)."""

    display_name: str
    description: str = ""
    required_secrets: tuple[str, ...] = ()  # secret aliases the adapter needs
    optional_secrets: tuple[str, ...] = ()
    docs_url: str | None = None


@dataclass
class PluginContext:
    """Everything an adapter receives at construction time.

    ``secrets`` maps *aliases* (e.g. ``api_key``) to resolved values; the
    alias -> vault key mapping lives in providers.yaml (``secret_names``).
    Aliases whose vault key is missing are simply absent.
    """

    name: str
    kind: ProviderKind
    settings: dict[str, Any] = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict)
    secret_names: Mapping[str, str] = field(default_factory=dict)
    limits: tuple[QuotaLimit, ...] = ()
    timeout_s: float = 30.0
    # Injected by tests (httpx.MockTransport) or special deployments.
    http_transport: httpx.AsyncBaseTransport | None = None


# --------------------------------------------------------------------------- common models


class HealthStatus(BaseModel):
    provider: str
    ok: bool
    latency_ms: float | None = None
    detail: str = ""
    checked_at: datetime = Field(default_factory=utcnow)


class QuotaInfo(BaseModel):
    provider: str
    used: float = 0.0
    limit: float | None = None  # None = unknown / unlimited
    unit: str = "requests"
    window: str | None = None  # minute | hour | day | month | total
    reset_at: datetime | None = None
    source: Literal["provider", "router", "config"] = "provider"
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def remaining(self) -> float | None:
        return None if self.limit is None else max(self.limit - self.used, 0.0)

    @property
    def fraction_used(self) -> float | None:
        if not self.limit:
            return None
        return min(self.used / self.limit, 1.0)


# --------------------------------------------------------------------------- LLM models


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None  # None/"auto" = provider default_model
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False
    user: str | None = None
    provider: str | None = None  # pin to a provider name from providers.yaml
    allow_fallback: bool = True  # with provider pinned: fall back to chain if it fails
    extra: dict[str, Any] = Field(default_factory=dict)  # provider-specific passthrough body fields


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    id: str = ""
    model: str
    content: str | None = None
    role: str = "assistant"
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    usage: Usage = Field(default_factory=Usage)
    routed_via: str = ""  # provider name, optionally "provider:upstream" (e.g. "freellmapi:groq")
    attempts: list[str] = Field(default_factory=list)  # providers tried before success, with reasons
    latency_ms: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ChatChunk(BaseModel):
    delta: str = ""
    model: str = ""
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    usage: Usage | None = None  # usually only on the last chunk
    routed_via: str = ""


class ModelInfo(BaseModel):
    id: str
    provider: str
    owned_by: str | None = None
    context_window: int | None = None
    capabilities: list[str] = Field(default_factory=list)


class EmbeddingRequest(BaseModel):
    input: list[str]
    model: str | None = None
    provider: str | None = None
    input_type: str | None = None  # e.g. Cohere search_document / search_query


class EmbeddingResponse(BaseModel):
    model: str
    vectors: list[list[float]]
    usage: Usage = Field(default_factory=Usage)
    routed_via: str = ""


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    model: str | None = None
    top_n: int | None = None
    provider: str | None = None


class RerankResult(BaseModel):
    index: int
    relevance_score: float


class RerankResponse(BaseModel):
    model: str
    results: list[RerankResult]
    routed_via: str = ""


class TranscriptionRequest(BaseModel):
    audio: bytes
    filename: str = "audio.wav"
    model: str | None = None
    language: str | None = None
    prompt: str | None = None  # optional context/spelling hint (Whisper ``prompt``)
    provider: str | None = None


class TranscriptionResponse(BaseModel):
    model: str
    text: str
    routed_via: str = ""


# --------------------------------------------------------------------------- storage / db / compute / dns / cron models


class StoredObject(BaseModel):
    key: str
    size: int = 0
    content_type: str | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    backend: str = ""  # provider name holding the object
    url: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class DirectUpload(BaseModel):
    """Instructions for a client to upload bytes straight to a storage backend.

    The client sends the file body with ``method`` to ``url`` adding exactly ``headers``
    (they may carry a short-lived upload credential, never a long-lived secret).
    """

    provider: str
    key: str
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ColumnInfo(BaseModel):
    name: str
    type: str = ""
    nullable: bool = True
    primary_key: bool = False
    default: str | None = None


class TableInfo(BaseModel):
    name: str
    schema_name: str | None = None
    kind: Literal["table", "view", "collection", "keyspace", "topic", "index"] = "table"
    row_count: int | None = None
    size_bytes: int | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)


class QueryResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    affected_rows: int | None = None
    elapsed_ms: float | None = None
    backend: str = ""
    truncated: bool = False


class VMInstance(BaseModel):
    id: str
    name: str
    provider: str = ""
    state: Literal[
        "running", "stopped", "starting", "stopping", "provisioning", "terminated",
        "sleeping", "deploying", "error", "online", "offline", "unknown",
    ] = "unknown"
    region: str | None = None
    shape: str | None = None
    public_ip: str | None = None
    private_ip: str | None = None
    tailscale_ip: str | None = None
    url: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class DNSRecord(BaseModel):
    name: str
    type: str
    value: str
    ttl: int | None = None


class DNSStatus(BaseModel):
    provider: str = ""
    blocking_enabled: bool
    blocking_resumes_at: datetime | None = None
    queries_today: int | None = None
    blocked_today: int | None = None
    percent_blocked: float | None = None
    domains_on_blocklist: int | None = None


class DNSQueryLogEntry(BaseModel):
    timestamp: datetime
    domain: str
    client: str | None = None
    query_type: str | None = None
    status: str | None = None
    blocked: bool = False


class CronJob(BaseModel):
    id: str = ""
    name: str
    schedule: str  # 5-field cron expression (UTC)
    target: str  # URL (QStash) or workflow file name (GitHub Actions)
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    enabled: bool = True
    provider: str = ""
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


# --------------------------------------------------------------------------- exceptions


class ProviderError(Exception):
    """Base class for every provider-side failure."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after

    def __str__(self) -> str:
        prefix = f"[{self.provider}] " if self.provider else ""
        code = f" (HTTP {self.status_code})" if self.status_code else ""
        return f"{prefix}{self.message}{code}"


class QuotaExhausted(ProviderError):
    """HTTP 429 / 402 or a locally tracked free-tier limit was hit."""


class ProviderUnavailable(ProviderError):
    """Network failure, 5xx, maintenance: transient, trips the circuit breaker."""


class ProviderTimeout(ProviderUnavailable):
    """The provider did not answer within the configured timeout."""


class NotConfigured(ProviderError):
    """Missing secret/setting: the provider is skipped until reconfigured."""


class ProviderAuthError(NotConfigured):
    """Credentials were rejected (401/403)."""


class PluginLoadError(NotConfigured):
    """The plugin module/class could not be imported or is invalid."""


class CapabilityNotSupported(ProviderError):
    """An optional operation was requested from an adapter that lacks the capability."""


class NoProviderAvailable(ProviderError):
    """Router-level: every candidate in the fallback chain failed or was skipped."""

    def __init__(self, message: str, *, kind: str, attempts: list[str]) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    if value.strip().replace(".", "", 1).isdigit():
        return max(float(value), 0.0)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - utcnow()).total_seconds(), 0.0)


def raise_for_response(response: httpx.Response, provider: str) -> None:
    """Map a non-2xx HTTP response to the OmniFree exception hierarchy."""
    if response.is_success:
        return
    code = response.status_code
    try:
        body = response.text[:500]
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        body = ""
    retry_after = _parse_retry_after(response.headers.get("retry-after"))
    msg = body or response.reason_phrase or "error"
    if code in (402, 429):
        raise QuotaExhausted(msg, provider=provider, status_code=code, retry_after=retry_after)
    if code in (401, 403):
        raise ProviderAuthError(msg, provider=provider, status_code=code)
    if code == 408 or code >= 500:
        raise ProviderUnavailable(msg, provider=provider, status_code=code, retry_after=retry_after)
    raise ProviderError(msg, provider=provider, status_code=code)


def translate_transport_error(exc: BaseException, provider: str) -> ProviderError:
    """Convert httpx/asyncio transport exceptions to ProviderUnavailable/ProviderTimeout."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return ProviderTimeout(f"timeout: {exc!r}", provider=provider)
    return ProviderUnavailable(f"transport error: {exc!r}", provider=provider)


# --------------------------------------------------------------------------- adapter bases

_ADAPTER_CLASSES: list[type[ProviderAdapter]] = []


def adapter_catalog() -> dict[str, type[ProviderAdapter]]:
    """All concrete adapter classes imported so far, keyed by ``module:QualName``."""
    out: dict[str, type[ProviderAdapter]] = {}
    for cls in _ADAPTER_CLASSES:
        if getattr(cls, "__abstractmethods__", None):
            continue
        out[f"{cls.__module__}:{cls.__qualname__}"] = cls
    return out


class ProviderAdapter(ABC):
    """Common base for every provider adapter.

    Constructor contract: ``Adapter(context: PluginContext)``. Constructors
    must not perform I/O; validate lazily and raise :class:`NotConfigured`
    from the first operation that needs a missing secret/setting.
    """

    kind: ClassVar[ProviderKind]
    capabilities: ClassVar[frozenset[str]] = frozenset()
    meta: ClassVar[PluginMeta | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _ADAPTER_CLASSES.append(cls)

    def __init__(self, context: PluginContext) -> None:
        self.context = context
        self.name = context.name
        self._http: httpx.AsyncClient | None = None

    # ---- config helpers
    @property
    def settings(self) -> dict[str, Any]:
        return self.context.settings

    def setting(self, key: str, default: Any = None) -> Any:
        value = self.context.settings.get(key)
        return default if value is None else value

    def require_setting(self, key: str) -> Any:
        value = self.context.settings.get(key)
        if value in (None, ""):
            raise NotConfigured(f"setting '{key}' is required", provider=self.name)
        return value

    def secret(self, alias: str, default: str | None = None) -> str | None:
        value = self.context.secrets.get(alias)
        return default if not value else value

    def require_secret(self, alias: str) -> str:
        value = self.context.secrets.get(alias)
        if not value:
            vault_key = self.context.secret_names.get(alias, alias)
            raise NotConfigured(
                f"secret '{alias}' (vault key {vault_key}) is not set", provider=self.name
            )
        return value

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # ---- HTTP helpers
    def build_http_client(self) -> httpx.AsyncClient:
        """Override to add base_url/auth headers; always honour context.http_transport."""
        return httpx.AsyncClient(
            timeout=self.context.timeout_s,
            transport=self.context.http_transport,
            follow_redirects=True,
        )

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = self.build_http_client()
        return self._http

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """``self.http.request`` + transport-error translation + raise_for_response."""
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.name) from exc
        raise_for_response(response, self.name)
        return response

    async def probe(self, check: Callable[[], Awaitable[Any]], detail: str = "ok") -> HealthStatus:
        """Run ``check`` and wrap the outcome in a HealthStatus with latency."""
        start = time.perf_counter()
        try:
            await check()
        except Exception as exc:  # health must never raise
            return HealthStatus(
                provider=self.name,
                ok=False,
                latency_ms=(time.perf_counter() - start) * 1000,
                detail=f"{type(exc).__name__}: {exc}"[:500],
            )
        return HealthStatus(
            provider=self.name, ok=True, latency_ms=(time.perf_counter() - start) * 1000, detail=detail
        )

    # ---- lifecycle / observability
    @abstractmethod
    async def health(self) -> HealthStatus:
        """Cheap liveness probe. Must not raise: return ok=False instead (see probe())."""

    async def quota(self) -> QuotaInfo:
        """Provider-reported usage. Adapters whose provider exposes usage override this.

        The default reports "unknown" so the router falls back to its own counters.
        """
        return QuotaInfo(provider=self.name, used=0.0, limit=None, unit="unknown", source="router")

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    async def __aenter__(self) -> ProviderAdapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _unsupported(self, capability: str) -> CapabilityNotSupported:
        return CapabilityNotSupported(
            f"capability '{capability}' not supported", provider=self.name
        )


class LLMAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.LLM
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {Capability.CHAT, Capability.STREAM, Capability.LIST_MODELS}
    )

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Implement as ``async def stream(...)`` with ``yield`` (async generator)."""

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Optional: requires Capability.EMBED."""
        raise self._unsupported(Capability.EMBED)

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        """Optional: requires Capability.RERANK."""
        raise self._unsupported(Capability.RERANK)

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """Optional: requires Capability.TRANSCRIBE."""
        raise self._unsupported(Capability.TRANSCRIBE)


class StorageAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.STORAGE

    @abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject: ...

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Raise ProviderError(status_code=404) when the key does not exist."""

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]: ...

    @abstractmethod
    async def stat(self, key: str) -> StoredObject | None:
        """Return None when the key does not exist."""

    async def presign(self, key: str, expires_s: int = 3600, method: str = "GET") -> str:
        """Optional: requires Capability.PRESIGN."""
        raise self._unsupported(Capability.PRESIGN)

    async def upload_target(
        self, key: str, content_type: str, size: int | None = None, expires_s: int = 3600
    ) -> DirectUpload:
        """Optional: requires Capability.DIRECT_UPLOAD (presigned PUT, B2 upload URL ...)."""
        raise self._unsupported(Capability.DIRECT_UPLOAD)

    async def quota(self) -> QuotaInfo:
        """Storage adapters SHOULD override with unit='bytes' (used/limit capacity)."""
        limit = next(
            (lim.limit for lim in self.context.limits if lim.metric == "bytes"), None
        )
        return QuotaInfo(
            provider=self.name, used=0.0, limit=limit, unit="bytes", window="total", source="config"
        )


class DBAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.DB

    @abstractmethod
    async def list_tables(self) -> list[TableInfo]: ...

    @abstractmethod
    async def describe(self, table: str) -> TableInfo: ...

    @abstractmethod
    async def query(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 500
    ) -> QueryResult:
        """Read-only statement (SQL, or provider-native JSON/command string)."""

    @abstractmethod
    async def execute(
        self, statement: str, params: list[Any] | dict[str, Any] | None = None
    ) -> QueryResult:
        """Mutating statement; fill affected_rows when known."""

    @abstractmethod
    async def usage(self) -> QuotaInfo:
        """Storage used vs. free-tier limit (unit usually 'bytes')."""


class ComputeAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.COMPUTE

    @abstractmethod
    async def list_instances(self) -> list[VMInstance]: ...

    @abstractmethod
    async def status(self, instance_id: str) -> VMInstance: ...

    async def start(self, instance_id: str) -> VMInstance:
        """Optional: requires Capability.POWER."""
        raise self._unsupported(Capability.POWER)

    async def stop(self, instance_id: str) -> VMInstance:
        """Optional: requires Capability.POWER."""
        raise self._unsupported(Capability.POWER)


class DNSAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.DNS

    @abstractmethod
    async def toggle_blocking(self, enabled: bool, duration_s: int | None = None) -> DNSStatus: ...

    @abstractmethod
    async def status(self) -> DNSStatus: ...

    @abstractmethod
    async def query_log(self, limit: int = 100, blocked_only: bool = False) -> list[DNSQueryLogEntry]: ...

    async def list_records(self) -> list[DNSRecord]:
        """Optional: requires Capability.DNS_RECORDS (local DNS records)."""
        raise self._unsupported(Capability.DNS_RECORDS)


class CronAdapter(ProviderAdapter):
    kind: ClassVar[ProviderKind] = ProviderKind.CRON
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.CRON_TRIGGER})

    @abstractmethod
    async def create(self, job: CronJob) -> CronJob: ...

    @abstractmethod
    async def list(self) -> list[CronJob]: ...

    @abstractmethod
    async def delete(self, job_id: str) -> None: ...

    @abstractmethod
    async def trigger(self, job_id: str) -> None:
        """Run the job now (QStash publish / GitHub workflow_dispatch)."""


ADAPTER_BASES: dict[ProviderKind, type[ProviderAdapter]] = {
    ProviderKind.LLM: LLMAdapter,
    ProviderKind.STORAGE: StorageAdapter,
    ProviderKind.DB: DBAdapter,
    ProviderKind.COMPUTE: ComputeAdapter,
    ProviderKind.DNS: DNSAdapter,
    ProviderKind.CRON: CronAdapter,
}
