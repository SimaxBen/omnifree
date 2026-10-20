"""SmartRouter: per-kind fallback chains, quota tracking, circuit breaking, failover.

Usage::

    bus = EventBus()
    registry = PluginRegistry.from_file("config/providers.yaml", secret_lookup=settings.secret, bus=bus)
    router = SmartRouter(registry, QuotaStore(settings.quota_db_path))
    await router.start()
    resp = await router.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    resp.routed_via  # -> "groq"
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, Field

from core.events import EventBus, EventType
from core.interfaces import (
    Capability,
    CapabilityNotSupported,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    DirectUpload,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    LLMAdapter,
    ModelInfo,
    NoProviderAvailable,
    NotConfigured,
    ProviderAdapter,
    ProviderError,
    ProviderKind,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaInfo,
    RerankRequest,
    RerankResponse,
    StoredObject,
    TranscriptionRequest,
    TranscriptionResponse,
    Usage,
    translate_transport_error,
)
from core.quota import QuotaStore, window_reset_at
from core.registry import PluginRegistry

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "Attempt",
    "Routed",
    "ProviderRuntime",
    "ProviderStatus",
    "SmartRouter",
    "REQUEST_METRIC",
    "TOKEN_METRIC",
    "BYTES_METRIC",
]

log = logging.getLogger("omnifree.router")

T = TypeVar("T")

REQUEST_METRIC = "requests"
TOKEN_METRIC = "tokens"
BYTES_METRIC = "bytes"
_METRIC_ALIASES = {"calls": REQUEST_METRIC}
# Capacity metrics are checked only for writes (storage_put), never as a call gate.
_CAPACITY_METRICS = frozenset({BYTES_METRIC, "storage_bytes", "rows"})
_ALWAYS_WINDOWS = ("day", "month")
_MODEL_LIST_KEYS = ("models", "embedding_models", "rerank_models", "transcription_models")


def _metric(name: str) -> str:
    return _METRIC_ALIASES.get(name, name)


# --------------------------------------------------------------------------- circuit breaker


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Consecutive-failure breaker with exponential cooldown and a single half-open trial."""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        max_cooldown_s: float = 600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.base_cooldown_s = cooldown_s
        self.max_cooldown_s = max(max_cooldown_s, cooldown_s)
        self.clock = clock
        self.failures = 0
        self.cooldown_s = cooldown_s
        self.opened_until = 0.0
        self._open = False
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if not self._open:
            return BreakerState.CLOSED
        if self.clock() >= self.opened_until:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> bool:
        """Returns True when this closes a previously open breaker."""
        was_open = self._open
        self._open = False
        self._trial_in_flight = False
        self.failures = 0
        self.cooldown_s = self.base_cooldown_s
        return was_open

    def record_failure(self) -> bool:
        """Returns True when this (re)opens the breaker."""
        if self._open:  # failed half-open trial (or failure while open): back off harder
            self._trial_in_flight = False
            self.cooldown_s = min(self.cooldown_s * 2, self.max_cooldown_s)
            self.opened_until = self.clock() + self.cooldown_s
            return True
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._open = True
            self.cooldown_s = self.base_cooldown_s
            self.opened_until = self.clock() + self.cooldown_s
            return True
        return False

    def release(self) -> None:
        """End a half-open trial without a verdict (e.g. provider skipped as not configured)."""
        self._trial_in_flight = False

    def reset(self) -> None:
        self.record_success()


# --------------------------------------------------------------------------- results / status


@dataclass
class Attempt:
    provider: str
    outcome: str  # ok | quota | unavailable | timeout | error | not_configured | unsupported | circuit_open
    detail: str = ""
    latency_ms: float | None = None

    def __str__(self) -> str:
        return f"{self.provider}:{self.outcome}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Routed(Generic[T]):
    result: T
    provider: str
    attempts: list[Attempt]
    latency_ms: float

    @property
    def failed_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if a.outcome != "ok"]


@dataclass
class ProviderRuntime:
    breaker: CircuitBreaker
    exhausted_until: float = 0.0
    last_error: str | None = None
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    successes: int = 0
    failures: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_health: HealthStatus | None = None

    def observe_latency(self, ms: float) -> None:
        self.last_latency_ms = ms
        self.avg_latency_ms = ms if self.avg_latency_ms is None else 0.8 * self.avg_latency_ms + 0.2 * ms


class ProviderStatus(BaseModel):
    name: str
    kind: str
    group: str
    enabled: bool
    priority: int
    breaker: str
    exhausted_until: datetime | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    successes: int = 0
    failures: int = 0
    missing_secrets: list[str] = Field(default_factory=list)
    load_error: str | None = None
    last_health: HealthStatus | None = None
    usage: list[QuotaInfo] = Field(default_factory=list)


_FAILOVER_ERRORS = (ProviderError, httpx.HTTPError, TimeoutError, OSError)


# --------------------------------------------------------------------------- router


class SmartRouter:
    def __init__(
        self,
        registry: PluginRegistry,
        quota_store: QuotaStore,
        bus: EventBus | None = None,
        *,
        clock: Callable[[], float] = time.time,
        capacity_ttl_s: float = 60.0,
    ) -> None:
        self.registry = registry
        self.quota = quota_store
        self.bus = bus or registry.bus
        self.clock = clock
        self.capacity_ttl_s = capacity_ttl_s
        self._runtime: dict[str, ProviderRuntime] = {}
        self._capacity_cache: dict[str, tuple[float, QuotaInfo]] = {}
        # Model ids last reported live by each adapter's list_models(); they extend the static
        # settings.models lists (which are only fallbacks for providers whose catalog drifts).
        self._live_models: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> SmartRouter:
        await self.quota.open()
        return self

    async def close(self) -> None:
        await self.registry.close()
        await self.quota.close()

    async def __aenter__(self) -> SmartRouter:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ runtime state
    def runtime(self, name: str) -> ProviderRuntime:
        rt = self._runtime.get(name)
        if rt is None:
            cb = self.registry.defaults.circuit_breaker
            rt = ProviderRuntime(
                breaker=CircuitBreaker(cb.failure_threshold, cb.cooldown_s, cb.max_cooldown_s, self.clock)
            )
            self._runtime[name] = rt
        return rt

    def reset_provider(self, name: str) -> None:
        """Manually close the breaker and clear quota cooldown (switchboard 'reset')."""
        rt = self.runtime(name)
        rt.breaker.reset()
        rt.exhausted_until = 0.0
        rt.last_error = None

    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    # ------------------------------------------------------------------ usage accounting
    async def record_usage(self, name: str, metric: str, amount: float) -> None:
        if not amount:
            return
        metric = _metric(metric)
        spec = self.registry.spec(name)
        windows = set(_ALWAYS_WINDOWS)
        windows.update(lim.window for lim in spec.limits if _metric(lim.metric) == metric)
        await self.quota.add(name, metric, amount, windows, now=self._now_dt())
        self.bus.publish(
            EventType.QUOTA_UPDATED, provider=name, kind=spec.kind, metric=metric, amount=amount
        )

    async def usage(self, name: str) -> list[QuotaInfo]:
        """Router-side counters for ``name``: one entry per configured limit plus day/month totals."""
        spec = self.registry.spec(name)
        now = self._now_dt()
        out: list[QuotaInfo] = []
        seen: set[tuple[str, str]] = set()
        for lim in spec.limits:
            metric = _metric(lim.metric)
            used = await self.quota.used(name, metric, lim.window, now=now)
            seen.add((metric, lim.window))
            out.append(
                QuotaInfo(
                    provider=name, used=used, limit=lim.limit, unit=metric, window=lim.window,
                    reset_at=window_reset_at(lim.window, now), source="router",
                )
            )
        for row in await self.quota.snapshot(name, now=now):
            key = (str(row["metric"]), str(row["window"]))
            if key in seen:
                continue
            out.append(
                QuotaInfo(
                    provider=name, used=float(row["used"]), limit=None, unit=key[0], window=key[1],
                    reset_at=row["reset_at"], source="router",  # type: ignore[arg-type]
                )
            )
        return out

    async def provider_quota(self, name: str) -> QuotaInfo:
        """Ask the adapter itself (provider-reported usage)."""
        adapter = await self.registry.get(name)
        return await asyncio.wait_for(adapter.quota(), self.registry.timeout_for(name))

    async def quotas(self, kind: ProviderKind | str | None = None, *, include_provider: bool = True) -> dict[str, list[QuotaInfo]]:
        result: dict[str, list[QuotaInfo]] = {}
        specs = self.registry.list(kind, enabled_only=True)

        async def one(name: str) -> None:
            items = await self.usage(name)
            if include_provider:
                try:
                    q = await self.provider_quota(name)
                    if q.limit is not None or q.unit != "unknown":
                        items.insert(0, q)
                except Exception as exc:  # quota display must not fail the whole call
                    log.debug("provider quota for %s failed: %s", name, exc)
            result[name] = items

        await asyncio.gather(*(one(s.name) for s in specs))
        return {s.name: result[s.name] for s in specs}

    async def _local_quota_block(self, name: str) -> str | None:
        """Return a reason if a router-tracked limit is already reached."""
        spec = self.registry.spec(name)
        now = self._now_dt()
        for lim in spec.limits:
            metric = _metric(lim.metric)
            if metric in _CAPACITY_METRICS:
                continue
            used = await self.quota.used(name, metric, lim.window, now=now)
            if used >= lim.limit:
                reset = window_reset_at(lim.window, now)
                rt = self.runtime(name)
                until = reset.timestamp() if reset else math.inf
                if until > rt.exhausted_until:
                    rt.exhausted_until = until
                    self.bus.publish(
                        EventType.QUOTA_EXHAUSTED, provider=name, kind=spec.kind, metric=metric,
                        window=lim.window, used=used, limit=lim.limit,
                        reset_at=reset.isoformat() if reset else None, source="router",
                    )
                return f"{metric}/{lim.window} {used:g}>={lim.limit:g}"
        return None

    # ------------------------------------------------------------------ candidate resolution
    def _resolve_names(
        self,
        kind: ProviderKind,
        candidates: Iterable[str] | None,
        provider: str | None,
        allow_fallback: bool,
    ) -> list[str]:
        chain = list(candidates) if candidates is not None else self.registry.chain(kind)
        if provider:
            spec = self.registry.spec(provider)
            if spec.kind != kind:
                raise NotConfigured(f"plugin '{provider}' is {spec.kind}, not {kind}", provider=provider)
            if not allow_fallback:
                return [provider]
            full = chain if candidates is not None else self.registry.chain(kind)
            return [provider] + [n for n in full if n != provider]
        return chain

    def _models_of(self, name: str) -> set[str]:
        settings = self.registry.spec(name).settings
        models: set[str] = set()
        for key in _MODEL_LIST_KEYS:
            for m in settings.get(key) or []:
                models.add(m if isinstance(m, str) else str(m.get("id", "")))
        return models | self._live_models.get(name, set())

    def _model_hosts(self, model: str | None) -> tuple[list[str], set[str]]:
        """(ordered candidate chain, names that explicitly host ``model``)."""
        chain = self.registry.chain(ProviderKind.LLM)
        if not model or model == "auto":
            return chain, set(chain)
        hosts = [
            n for n in chain
            if model in self._models_of(n) or self.registry.spec(n).settings.get("accepts_any_model")
        ]
        if not hosts:
            return chain, set(chain)
        if self.registry.defaults.llm_model_fallback:
            return hosts + [n for n in chain if n not in hosts], set(hosts)
        return hosts, set(hosts)

    # ------------------------------------------------------------------ attempt machinery
    async def _prepare(
        self, name: str, kind: ProviderKind, capability: str | None, attempts: list[Attempt]
    ) -> ProviderAdapter | None:
        rt = self.runtime(name)
        now = self.clock()
        if rt.exhausted_until > now:
            until = "never" if math.isinf(rt.exhausted_until) else datetime.fromtimestamp(rt.exhausted_until, tz=timezone.utc).isoformat()
            attempts.append(Attempt(name, "quota", f"cooling down until {until}"))
            return None
        try:
            adapter = await self.registry.get(name)
        except NotConfigured as exc:
            attempts.append(Attempt(name, "not_configured", exc.message))
            return None
        if adapter.kind != kind:
            attempts.append(Attempt(name, "not_configured", f"adapter kind {adapter.kind} != {kind}"))
            return None
        if capability and not adapter.supports(capability):
            attempts.append(Attempt(name, "unsupported", str(capability)))
            return None
        reason = await self._local_quota_block(name)
        if reason:
            attempts.append(Attempt(name, "quota", reason))
            return None
        half_open = rt.breaker.state is BreakerState.HALF_OPEN
        if not rt.breaker.allow():
            attempts.append(Attempt(name, "circuit_open", f"state={rt.breaker.state}"))
            return None
        if half_open:
            self.bus.publish(EventType.BREAKER_HALF_OPEN, provider=name, kind=kind)
        return adapter

    async def _on_success(self, name: str, kind: ProviderKind, op: str, latency_ms: float, attempts: list[Attempt]) -> None:
        rt = self.runtime(name)
        rt.observe_latency(latency_ms)
        rt.successes += 1
        rt.last_success_at = self._now_dt()
        if rt.breaker.record_success():
            self.bus.publish(EventType.BREAKER_CLOSED, provider=name, kind=kind)
        attempts.append(Attempt(name, "ok", latency_ms=latency_ms))
        await self.record_usage(name, REQUEST_METRIC, 1)
        self.bus.publish(
            EventType.REQUEST_SUCCEEDED, provider=name, kind=kind, op=op,
            latency_ms=round(latency_ms, 1), attempts=[str(a) for a in attempts if a.outcome != "ok"],
        )

    async def _on_failure(
        self, name: str, kind: ProviderKind, op: str, exc: BaseException, latency_ms: float, attempts: list[Attempt]
    ) -> None:
        rt = self.runtime(name)
        err = translate_transport_error(exc, name) if not isinstance(exc, ProviderError) else exc
        if not isinstance(exc, _FAILOVER_ERRORS):  # programming error inside an adapter
            log.exception("adapter %s raised unexpected %s during %s", name, type(exc).__name__, op, exc_info=exc)
            err = ProviderUnavailable(f"unexpected {type(exc).__name__}: {exc}", provider=name)
        rt.last_error = str(err)[:500]
        rt.last_failure_at = self._now_dt()
        rt.failures += 1

        if isinstance(err, QuotaExhausted):
            outcome = "quota"
            rt.breaker.record_success()  # the provider answered: it is reachable
            cooldown = err.retry_after if err.retry_after is not None else self.registry.defaults.quota_cooldown_s
            rt.exhausted_until = max(rt.exhausted_until, self.clock() + cooldown)
            await self.record_usage(name, REQUEST_METRIC, 1)
            self.bus.publish(
                EventType.QUOTA_EXHAUSTED, provider=name, kind=kind, retry_after=cooldown,
                reset_at=datetime.fromtimestamp(rt.exhausted_until, tz=timezone.utc).isoformat(),
                source="provider", detail=err.message[:200],
            )
        elif isinstance(err, NotConfigured):
            outcome = "not_configured"
            rt.breaker.release()
        elif isinstance(err, ProviderUnavailable):
            outcome = "timeout" if isinstance(err, ProviderTimeout) else "unavailable"
            if rt.breaker.record_failure():
                self.bus.publish(
                    EventType.BREAKER_OPENED, provider=name, kind=kind,
                    cooldown_s=rt.breaker.cooldown_s, error=rt.last_error,
                )
        else:  # generic ProviderError (e.g. 400 unknown model): provider is up, try the next one
            outcome = "error"
            rt.breaker.record_success()
            await self.record_usage(name, REQUEST_METRIC, 1)
        attempts.append(Attempt(name, outcome, str(err)[:200], latency_ms))
        self.bus.publish(
            EventType.REQUEST_FAILED, provider=name, kind=kind, op=op, outcome=outcome,
            error=str(err)[:300], latency_ms=round(latency_ms, 1),
        )

    def _all_failed(self, kind: ProviderKind, op: str, attempts: list[Attempt]) -> NoProviderAvailable:
        trail = [str(a) for a in attempts]
        self.bus.publish(EventType.ALL_FAILED, kind=kind, op=op, attempts=trail)
        detail = "; ".join(trail) if trail else "no enabled providers"
        return NoProviderAvailable(f"no {kind} provider could serve '{op}': {detail}", kind=str(kind), attempts=trail)

    async def execute(
        self,
        kind: ProviderKind | str,
        op: Callable[[Any], Awaitable[T]],
        *,
        candidates: Iterable[str] | None = None,
        provider: str | None = None,
        allow_fallback: bool = True,
        capability: str | None = None,
        op_name: str = "call",
    ) -> Routed[T]:
        """Run ``op(adapter)`` against the fallback chain until one succeeds.

        Fails over on QuotaExhausted, ProviderUnavailable/ProviderTimeout,
        NotConfigured, generic ProviderError and httpx transport errors.
        Raises NoProviderAvailable (with the attempt trail) when all fail.
        """
        kind = ProviderKind(kind)
        names = self._resolve_names(kind, candidates, provider, allow_fallback)
        attempts: list[Attempt] = []
        started = time.perf_counter()
        previous_failure: str | None = None
        for name in names:
            adapter = await self._prepare(name, kind, capability, attempts)
            if adapter is None:
                continue
            if previous_failure is not None:
                self.bus.publish(EventType.FAILOVER, kind=kind, op=op_name, from_provider=previous_failure, to_provider=name)
            t0 = time.perf_counter()
            try:
                result = await asyncio.wait_for(op(adapter), self.registry.timeout_for(name))
            except asyncio.CancelledError:
                self.runtime(name).breaker.release()
                raise
            except BaseException as exc:  # noqa: BLE001 - classified in _on_failure
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                await self._on_failure(name, kind, op_name, exc, (time.perf_counter() - t0) * 1000, attempts)
                previous_failure = name
                continue
            latency = (time.perf_counter() - t0) * 1000
            await self._on_success(name, kind, op_name, latency, attempts)
            return Routed(result, name, attempts, (time.perf_counter() - started) * 1000)
        raise self._all_failed(kind, op_name, attempts)

    async def call(self, name: str, op: Callable[[Any], Awaitable[T]], *, op_name: str = "call", capability: str | None = None) -> T:
        """Run ``op`` on one specific provider (no failover) with breaker/quota/metrics."""
        kind = self.registry.spec(name).kind
        routed = await self.execute(
            kind, op, provider=name, allow_fallback=False, op_name=op_name, capability=capability
        )
        return routed.result

    async def adapter(self, name: str) -> ProviderAdapter:
        """Direct access to an enabled adapter instance (bypasses routing)."""
        return await self.registry.get(name)

    # ------------------------------------------------------------------ LLM
    @staticmethod
    def _routed_via(provider: str, reported: str) -> str:
        if not reported or reported == provider:
            return provider
        return reported if reported.startswith(f"{provider}:") else f"{provider}:{reported}"

    def _request_for(self, req: ChatRequest, name: str, hosts: set[str]) -> ChatRequest:
        if req.model and req.model != "auto" and name not in hosts:
            return req.model_copy(update={"model": None})  # fall back to that provider's default model
        return req

    async def _record_tokens(self, name: str, usage: Usage | None) -> None:
        if usage and usage.total_tokens:
            await self.record_usage(name, TOKEN_METRIC, usage.total_tokens)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        chain, hosts = self._model_hosts(request.model)
        routed = await self.execute(
            ProviderKind.LLM,
            lambda a: a.chat(self._request_for(request, a.name, hosts)),
            candidates=chain,
            provider=request.provider,
            allow_fallback=request.allow_fallback,
            capability=Capability.CHAT,
            op_name="chat",
        )
        resp: ChatResponse = routed.result
        resp.routed_via = self._routed_via(routed.provider, resp.routed_via)
        resp.attempts = [str(a) for a in routed.failed_attempts]
        resp.latency_ms = round(routed.latency_ms, 1)
        await self._record_tokens(routed.provider, resp.usage)
        return resp

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Stream with failover *before* the first chunk; mid-stream errors propagate."""
        kind = ProviderKind.LLM
        chain, hosts = self._model_hosts(request.model)
        names = self._resolve_names(kind, chain, request.provider, request.allow_fallback)
        attempts: list[Attempt] = []
        previous_failure: str | None = None
        for name in names:
            adapter = await self._prepare(name, kind, Capability.STREAM, attempts)
            if adapter is None:
                continue
            assert isinstance(adapter, LLMAdapter)
            if previous_failure is not None:
                self.bus.publish(EventType.FAILOVER, kind=kind, op="stream", from_provider=previous_failure, to_provider=name)
            timeout = self.registry.timeout_for(name)
            agen = adapter.stream(self._request_for(request, name, hosts)).__aiter__()
            t0 = time.perf_counter()
            first: ChatChunk | None = None
            try:
                first = await asyncio.wait_for(agen.__anext__(), timeout)
            except StopAsyncIteration:
                first = None
            except asyncio.CancelledError:
                self.runtime(name).breaker.release()
                await agen.aclose()
                raise
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                await agen.aclose()
                await self._on_failure(name, kind, "stream", exc, (time.perf_counter() - t0) * 1000, attempts)
                previous_failure = name
                continue
            await self._on_success(name, kind, "stream", (time.perf_counter() - t0) * 1000, attempts)
            routed_via = name
            usage: Usage | None = None
            try:
                if first is not None:
                    routed_via = self._routed_via(name, first.routed_via)
                    first.routed_via = routed_via
                    usage = first.usage or usage
                    yield first
                    while True:
                        try:
                            chunk = await asyncio.wait_for(agen.__anext__(), timeout)
                        except StopAsyncIteration:
                            break
                        chunk.routed_via = routed_via
                        usage = chunk.usage or usage
                        yield chunk
            except (ProviderError, httpx.HTTPError, TimeoutError) as exc:
                err = translate_transport_error(exc, name)
                self.runtime(name).last_error = str(err)[:500]
                raise err from exc
            finally:
                await agen.aclose()
                await self._record_tokens(name, usage)
            return
        raise self._all_failed(kind, "stream", attempts)

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        chain, _hosts = self._model_hosts(request.model)
        routed = await self.execute(
            ProviderKind.LLM, lambda a: a.embed(request), candidates=chain,
            provider=request.provider, capability=Capability.EMBED, op_name="embed",
        )
        resp: EmbeddingResponse = routed.result
        resp.routed_via = self._routed_via(routed.provider, resp.routed_via)
        await self._record_tokens(routed.provider, resp.usage)
        return resp

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        chain, _hosts = self._model_hosts(request.model)
        routed = await self.execute(
            ProviderKind.LLM, lambda a: a.rerank(request), candidates=chain,
            provider=request.provider, capability=Capability.RERANK, op_name="rerank",
        )
        resp: RerankResponse = routed.result
        resp.routed_via = self._routed_via(routed.provider, resp.routed_via)
        return resp

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        chain, _hosts = self._model_hosts(request.model)
        routed = await self.execute(
            ProviderKind.LLM, lambda a: a.transcribe(request), candidates=chain,
            provider=request.provider, capability=Capability.TRANSCRIBE, op_name="transcribe",
        )
        resp: TranscriptionResponse = routed.result
        resp.routed_via = self._routed_via(routed.provider, resp.routed_via)
        return resp

    async def list_models(self) -> dict[str, list[ModelInfo]]:
        """Models per enabled LLM provider; falls back to settings.models if listing fails.

        Live ids are remembered so later requests for a model that only the live list knows
        (e.g. a new OpenRouter ':free' or Gemini model) are routed to that provider first.
        """
        out: dict[str, list[ModelInfo]] = {}
        names = self.registry.chain(ProviderKind.LLM)

        async def one(name: str) -> None:
            configured = [ModelInfo(id=m, provider=name) for m in sorted(self._models_of(name))]
            try:
                adapter = await self.registry.get(name)
                models = await asyncio.wait_for(adapter.list_models(), self.registry.timeout_for(name))  # type: ignore[attr-defined]
                if models:
                    self._live_models[name] = {m.id for m in models}
                out[name] = models or configured
            except Exception as exc:
                log.debug("list_models(%s) failed: %s", name, exc)
                out[name] = configured

        await asyncio.gather(*(one(n) for n in names))
        return {n: out[n] for n in names}

    # ------------------------------------------------------------------ storage
    async def storage_capacity(self, name: str, *, refresh: bool = False) -> QuotaInfo:
        """Used/limit bytes for a storage provider (adapter-reported, cached; router counter fallback)."""
        cached = self._capacity_cache.get(name)
        if cached and not refresh and self.clock() - cached[0] < self.capacity_ttl_s:
            return cached[1]
        spec = self.registry.spec(name)
        config_limit = next((lim.limit for lim in spec.limits if lim.metric == BYTES_METRIC), None)
        info: QuotaInfo | None = None
        try:
            adapter = await self.registry.get(name)
            reported = await asyncio.wait_for(adapter.quota(), self.registry.timeout_for(name))
            if reported.unit == "bytes" and reported.source == "provider":
                info = reported if reported.limit is not None else reported.model_copy(update={"limit": config_limit})
        except Exception as exc:
            log.debug("capacity(%s) failed: %s", name, exc)
        if info is None:
            used = await self.quota.used(name, BYTES_METRIC, "total")
            info = QuotaInfo(provider=name, used=used, limit=config_limit, unit="bytes", window="total", source="router")
        self._capacity_cache[name] = (self.clock(), info)
        return info

    async def _storage_order(self, size: int) -> list[str]:
        chain = self.registry.chain(ProviderKind.STORAGE)
        if self.registry.defaults.storage_strategy != "most_free":
            return chain
        caps = await asyncio.gather(*(self.storage_capacity(n) for n in chain))
        known: list[tuple[float, int, str]] = []
        unknown: list[str] = []
        for idx, (name, cap) in enumerate(zip(chain, caps, strict=True)):
            free = cap.remaining
            if free is None:
                unknown.append(name)
            elif free >= size:
                known.append((-free, idx, name))
        return [n for _f, _i, n in sorted(known)] + unknown

    async def storage_put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
        *,
        provider: str | None = None,
        allow_fallback: bool = True,
    ) -> StoredObject:
        """Store an object on the backend with the most free capacity (or ``provider``)."""
        order = await self._storage_order(len(data))
        routed = await self.execute(
            ProviderKind.STORAGE,
            lambda a: a.put(key, data, content_type, metadata),
            candidates=order,
            provider=provider,
            allow_fallback=allow_fallback,
            op_name="put",
        )
        obj: StoredObject = routed.result
        obj.backend = obj.backend or routed.provider
        await self.record_usage(routed.provider, BYTES_METRIC, len(data))
        self._capacity_cache.pop(routed.provider, None)
        self.bus.publish(EventType.OBJECT_STORED, provider=routed.provider, kind=ProviderKind.STORAGE, key=key, size=len(data))
        return obj

    async def storage_upload_target(
        self,
        key: str,
        content_type: str,
        size: int | None = None,
        *,
        expires_s: int = 3600,
        provider: str | None = None,
    ) -> DirectUpload:
        """Pick a backend for a client-side direct upload and return its upload instructions.

        Same placement policy as :meth:`storage_put` (most free space first). Backends without
        ``Capability.DIRECT_UPLOAD`` are skipped, as are unconfigured ones. A pinned ``provider``
        is used alone and reports CapabilityNotSupported when it cannot do direct uploads.
        """
        if provider:
            spec = self.registry.spec(provider)
            if spec.kind != ProviderKind.STORAGE:
                raise NotConfigured(f"plugin '{provider}' is {spec.kind}, not {ProviderKind.STORAGE}", provider=provider)
            adapter = await self.registry.get(provider)
            if not adapter.supports(Capability.DIRECT_UPLOAD):
                raise CapabilityNotSupported(
                    f"capability '{Capability.DIRECT_UPLOAD}' not supported", provider=provider
                )
            order: list[str] | None = None
        else:
            order = await self._storage_order(max(size or 0, 0))
        routed = await self.execute(
            ProviderKind.STORAGE,
            lambda a: a.upload_target(key, content_type, size, expires_s),
            candidates=order,
            provider=provider,
            allow_fallback=provider is None,
            capability=Capability.DIRECT_UPLOAD,
            op_name="upload_target",
        )
        target: DirectUpload = routed.result
        target.provider = target.provider or routed.provider
        self._capacity_cache.pop(routed.provider, None)  # the client is about to add bytes
        return target

    async def storage_get(self, key: str, provider: str) -> bytes:
        return await self.call(provider, lambda a: a.get(key), op_name="get")

    async def storage_stat(self, key: str, provider: str) -> StoredObject | None:
        return await self.call(provider, lambda a: a.stat(key), op_name="stat")

    async def storage_presign(self, key: str, provider: str, expires_s: int = 3600, method: str = "GET") -> str:
        return await self.call(
            provider, lambda a: a.presign(key, expires_s, method), op_name="presign", capability=Capability.PRESIGN
        )

    async def storage_delete(self, key: str, provider: str) -> None:
        size = 0
        try:
            obj = await self.storage_stat(key, provider)
            size = obj.size if obj else 0
        except ProviderError as exc:
            log.debug("stat before delete failed on %s: %s", provider, exc)
        await self.call(provider, lambda a: a.delete(key), op_name="delete")
        if size:
            await self.record_usage(provider, BYTES_METRIC, -size)
        self._capacity_cache.pop(provider, None)
        self.bus.publish(EventType.OBJECT_DELETED, provider=provider, kind=ProviderKind.STORAGE, key=key)

    async def storage_list(
        self, prefix: str = "", *, providers: Iterable[str] | None = None, limit: int = 1000
    ) -> dict[str, list[StoredObject] | str]:
        """List across backends concurrently. Value is the object list or an error string."""
        names = list(providers) if providers is not None else self.registry.chain(ProviderKind.STORAGE)
        out: dict[str, list[StoredObject] | str] = {}

        async def one(name: str) -> None:
            try:
                objs = await self.call(name, lambda a: a.list(prefix, limit), op_name="list")
                for o in objs:
                    o.backend = o.backend or name
                out[name] = objs
            except ProviderError as exc:
                out[name] = str(exc)

        await asyncio.gather(*(one(n) for n in names))
        return {n: out[n] for n in names}

    async def storage_locate(self, key: str) -> StoredObject | None:
        """Find which backend holds ``key`` (first match in chain order)."""
        for name in self.registry.chain(ProviderKind.STORAGE):
            try:
                obj = await self.storage_stat(key, name)
            except ProviderError:
                continue
            if obj is not None:
                obj.backend = obj.backend or name
                return obj
        return None

    # ------------------------------------------------------------------ health / status
    async def health_check(self, name: str) -> HealthStatus:
        spec = self.registry.spec(name)
        try:
            adapter = await self.registry.get(name, allow_disabled=True)
            status = await asyncio.wait_for(adapter.health(), self.registry.timeout_for(name))
        except Exception as exc:
            status = HealthStatus(provider=name, ok=False, detail=f"{type(exc).__name__}: {exc}"[:500])
        self.runtime(name).last_health = status
        self.bus.publish(
            EventType.HEALTH_CHECKED, provider=name, kind=spec.kind, ok=status.ok,
            latency_ms=status.latency_ms, detail=status.detail[:200],
        )
        return status

    async def health_all(self, kind: ProviderKind | str | None = None, *, enabled_only: bool = True) -> list[HealthStatus]:
        specs = self.registry.list(kind, enabled_only=enabled_only)
        return list(await asyncio.gather(*(self.health_check(s.name) for s in specs)))

    async def status(self, kind: ProviderKind | str | None = None) -> list[ProviderStatus]:
        out: list[ProviderStatus] = []
        for spec in self.registry.list(kind):
            rt = self.runtime(spec.name)
            exhausted = None
            if rt.exhausted_until > self.clock() and not math.isinf(rt.exhausted_until):
                exhausted = datetime.fromtimestamp(rt.exhausted_until, tz=timezone.utc)
            out.append(
                ProviderStatus(
                    name=spec.name, kind=spec.kind, group=spec.group, enabled=spec.enabled,
                    priority=spec.priority, breaker=rt.breaker.state, exhausted_until=exhausted,
                    last_error=rt.last_error, last_latency_ms=rt.last_latency_ms,
                    avg_latency_ms=rt.avg_latency_ms, successes=rt.successes, failures=rt.failures,
                    missing_secrets=self.registry.missing_secrets(spec.name),
                    load_error=self.registry.load_error(spec.name), last_health=rt.last_health,
                    usage=await self.usage(spec.name),
                )
            )
        return out

