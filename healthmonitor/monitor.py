"""OmniFree health monitor / uptime checker / background worker.

Runs on the GCP e2-micro (primary, inside the tailnet) or, optionally, on Koyeb (public targets only;
Koyeb has no free tier for new signups since 2026).

* **Checks** (``targets``): HTTP probes with expected status codes, optional body
  substring, per-target timeout. A target goes DOWN after ``failure_threshold``
  consecutive failures and UP again after one success; each transition sends one alert.
* **Jobs** (``jobs``): periodic HTTP calls (for example the Supabase keep-alive so the free
  project does not pause after a week idle, or a Cloud Run warm-up ping).
* **Heartbeat**: optional dead-man's-switch URL pinged after every sweep
  (``HEALTHMONITOR_HEARTBEAT_URL``, e.g. healthchecks.io).
* **Alerts**: ``HEALTHMONITOR_ALERT_WEBHOOK_URL``: Discord, Slack, ntfy or any JSON webhook;
  optional Telegram (``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``).

Config: YAML (default ``healthmonitor/targets.yaml``, override with ``OMNIFREE_MONITOR_CONFIG``).
Strings may reference ``${ENV_VAR}`` (plain settings such as ``HUB_URL``) and
``${secret:NAME}`` (resolved through ``core.config.Settings.secret``: env first, then the
vault). A target/job whose URL still has an unresolved ``${...}`` is disabled, not failed.
Targets marked ``tailnet: true`` only run when ``OMNIFREE_MONITOR_TAILNET`` is true.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

__all__ = [
    "TargetSpec",
    "JobSpec",
    "MonitorConfig",
    "TargetState",
    "Alerter",
    "HealthMonitor",
    "load_config",
    "expand",
    "UnresolvedReference",
]

log = logging.getLogger("omnifree.healthmonitor")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "targets.yaml"
_REF = re.compile(r"\$\{(secret:)?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

SecretLookup = Callable[[str], str | None]


class UnresolvedReference(ValueError):
    """A ``${VAR}`` / ``${secret:NAME}`` reference has no value (the target is disabled, not failed)."""


def expand(value: str, secret_lookup: SecretLookup, env: dict[str, str] | None = None) -> str:
    """Replace ``${VAR}``, ``${VAR:-default}`` and ``${secret:NAME}``; raise when unresolved."""
    env = os.environ if env is None else env

    def repl(m: re.Match[str]) -> str:
        is_secret, name, default = m.group(1), m.group(2), m.group(3)
        resolved = secret_lookup(name) if is_secret else env.get(name)
        if resolved:
            return resolved
        if default is not None:
            return default
        raise UnresolvedReference(name)

    return _REF.sub(repl, value)


@dataclass
class TargetSpec:
    name: str
    url: str
    method: str = "GET"
    expect_status: list[int] = field(default_factory=lambda: list(range(200, 400)))
    contains: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float | None = None
    tailnet: bool = False


@dataclass
class JobSpec:
    name: str
    url: str
    every_s: float
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    tailnet: bool = False


@dataclass
class MonitorConfig:
    interval_s: float = 60.0
    timeout_s: float = 10.0
    failure_threshold: int = 3
    targets: list[TargetSpec] = field(default_factory=list)
    jobs: list[JobSpec] = field(default_factory=list)


def load_config(path: Path | None = None) -> MonitorConfig:
    path = path or Path(os.environ.get("OMNIFREE_MONITOR_CONFIG") or DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets = []
    for t in raw.get("targets") or []:
        exp = t.get("expect_status")
        targets.append(TargetSpec(
            name=str(t["name"]), url=str(t["url"]), method=str(t.get("method", "GET")).upper(),
            expect_status=[int(x) for x in exp] if exp else list(range(200, 400)),
            contains=t.get("contains"), headers={str(k): str(v) for k, v in (t.get("headers") or {}).items()},
            timeout_s=float(t["timeout_s"]) if t.get("timeout_s") else None, tailnet=bool(t.get("tailnet", False)),
        ))
    jobs = [
        JobSpec(name=str(j["name"]), url=str(j["url"]), every_s=float(j["every_s"]),
                method=str(j.get("method", "GET")).upper(),
                headers={str(k): str(v) for k, v in (j.get("headers") or {}).items()},
                body=j.get("body"), tailnet=bool(j.get("tailnet", False)))
        for j in raw.get("jobs") or []
    ]
    return MonitorConfig(
        interval_s=float(raw.get("interval_s", 60)), timeout_s=float(raw.get("timeout_s", 10)),
        failure_threshold=int(raw.get("failure_threshold", 3)), targets=targets, jobs=jobs,
    )


@dataclass
class TargetState:
    name: str
    status: str = "unknown"  # unknown | up | down | disabled
    consecutive_failures: int = 0
    last_checked: str | None = None
    last_ok: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    checks: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Alerter:
    """Sends a plain-text alert to every configured channel; never raises."""

    def __init__(self, client: httpx.AsyncClient, secret_lookup: SecretLookup) -> None:
        self.client = client
        self.secret_lookup = secret_lookup

    async def send(self, text: str) -> int:
        sent = 0
        webhook = self.secret_lookup("HEALTHMONITOR_ALERT_WEBHOOK_URL")
        if webhook:
            host = urlparse(webhook).netloc.lower()
            try:
                if "discord.com" in host or "discordapp.com" in host:
                    resp = await self.client.post(webhook, json={"content": text[:1900]})
                elif "hooks.slack.com" in host:
                    resp = await self.client.post(webhook, json={"text": text})
                elif host.startswith("ntfy.") or "ntfy" in host:
                    resp = await self.client.post(webhook, content=text.encode("utf-8"),
                                                  headers={"Title": "OmniFree monitor"})
                else:
                    resp = await self.client.post(webhook, json={"text": text, "source": "omnifree-healthmonitor"})
                if resp.status_code < 400:
                    sent += 1
                else:
                    log.warning("alert webhook returned %s", resp.status_code)
            except httpx.HTTPError as exc:
                log.warning("alert webhook failed: %r", exc)
        bot, chat = self.secret_lookup("TELEGRAM_BOT_TOKEN"), self.secret_lookup("TELEGRAM_CHAT_ID")
        if bot and chat:
            try:
                resp = await self.client.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                                              json={"chat_id": chat, "text": text})
                if resp.status_code < 400:
                    sent += 1
            except httpx.HTTPError as exc:
                log.warning("telegram alert failed: %r", exc)
        return sent


class HealthMonitor:
    def __init__(
        self,
        config: MonitorConfig,
        *,
        secret_lookup: SecretLookup,
        client: httpx.AsyncClient | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.secret_lookup = secret_lookup
        self.env = dict(os.environ) if env is None else env
        self.client = client or httpx.AsyncClient(timeout=config.timeout_s, follow_redirects=True)
        self._own_client = client is None
        self.alerter = Alerter(self.client, secret_lookup)
        self.clock = clock
        self.tailnet = self.env.get("OMNIFREE_MONITOR_TAILNET", "").lower() in ("1", "true", "yes")
        self.states: dict[str, TargetState] = {t.name: TargetState(t.name) for t in config.targets}
        self.job_last_run: dict[str, float] = {}
        self.job_results: dict[str, dict[str, Any]] = {}
        self.sweeps = 0
        self.started_at = _now()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ helpers
    def _resolve(self, url: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
        return (expand(url, self.secret_lookup, self.env),
                {k: expand(v, self.secret_lookup, self.env) for k, v in headers.items()})

    def _enabled(self, tailnet_only: bool) -> bool:
        return self.tailnet or not tailnet_only

    # ------------------------------------------------------------------ checks
    async def check(self, target: TargetSpec) -> TargetState:
        state = self.states.setdefault(target.name, TargetState(target.name))
        if not self._enabled(target.tailnet):
            state.status, state.last_error = "disabled", "tailnet-only target (OMNIFREE_MONITOR_TAILNET not set)"
            return state
        try:
            url, headers = self._resolve(target.url, target.headers)
        except UnresolvedReference as exc:
            state.status, state.last_error = "disabled", f"unresolved ${{{exc}}}"
            return state
        previous = state.status
        state.checks += 1
        state.last_checked = _now()
        t0 = time.perf_counter()
        error: str | None = None
        try:
            resp = await self.client.request(target.method, url, headers=headers,
                                             timeout=target.timeout_s or self.config.timeout_s)
            state.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            if resp.status_code not in target.expect_status:
                error = f"HTTP {resp.status_code}"
            elif target.contains and target.contains not in resp.text:
                error = f"body does not contain {target.contains!r}"
        except httpx.HTTPError as exc:
            state.latency_ms = None
            error = f"{type(exc).__name__}: {exc}"[:300]
        if error is None:
            state.consecutive_failures = 0
            state.last_ok = state.last_checked
            state.last_error = None
            state.status = "up"
            if previous == "down":
                await self.alerter.send(f"[OmniFree] RECOVERED {target.name} ({target.url}) in {state.latency_ms} ms")
        else:
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error = error
            if state.consecutive_failures >= self.config.failure_threshold:
                state.status = "down"
                if previous != "down":
                    await self.alerter.send(
                        f"[OmniFree] DOWN {target.name} ({target.url}): {error} "
                        f"after {state.consecutive_failures} consecutive failures"
                    )
            elif previous in ("unknown", "disabled"):
                state.status = "unknown"
        return state

    async def run_job(self, job: JobSpec) -> dict[str, Any]:
        result: dict[str, Any] = {"ran_at": _now()}
        try:
            url, headers = self._resolve(job.url, job.headers)
            resp = await self.client.request(job.method, url, headers=headers,
                                             content=job.body.encode("utf-8") if job.body else None)
            result.update(ok=resp.status_code < 400, status_code=resp.status_code)
        except UnresolvedReference as exc:
            result.update(ok=False, skipped=True, error=f"unresolved ${{{exc}}}")
        except httpx.HTTPError as exc:
            result.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        self.job_results[job.name] = result
        return result

    async def sweep(self) -> None:
        await asyncio.gather(*(self.check(t) for t in self.config.targets))
        now = self.clock()
        due = [j for j in self.config.jobs
               if self._enabled(j.tailnet) and now - self.job_last_run.get(j.name, float("-inf")) >= j.every_s]
        for job in due:
            self.job_last_run[job.name] = now
        if due:
            await asyncio.gather(*(self.run_job(j) for j in due))
        self.sweeps += 1
        await self._heartbeat()

    async def _heartbeat(self) -> None:
        url = self.secret_lookup("HEALTHMONITOR_HEARTBEAT_URL")
        if not url:
            return
        down = [s.name for s in self.states.values() if s.status == "down"]
        try:
            await self.client.post(url + ("/fail" if down and "hc-ping.com" in url else ""),
                                   content=(", ".join(down) or "ok").encode("utf-8"))
        except httpx.HTTPError as exc:
            log.warning("heartbeat failed: %r", exc)

    # ------------------------------------------------------------------ loop
    async def run_forever(self) -> None:
        log.info("health monitor started: %d targets, %d jobs, tailnet=%s",
                 len(self.config.targets), len(self.config.jobs), self.tailnet)
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:  # a sweep must never kill the worker
                log.exception("sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.interval_s)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        if self._own_client:
            await self.client.aclose()

    def public_snapshot(self) -> dict[str, Any]:
        """Status without error strings, latencies or job details (safe for an unauthenticated port)."""
        return {
            "started_at": self.started_at,
            "sweeps": self.sweeps,
            "down": sorted(s.name for s in self.states.values() if s.status == "down"),
            "targets": {n: {"status": s.status} for n, s in sorted(self.states.items())},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "sweeps": self.sweeps,
            "tailnet": self.tailnet,
            "down": sorted(s.name for s in self.states.values() if s.status == "down"),
            "targets": {n: s.as_dict() for n, s in sorted(self.states.items())},
            "jobs": self.job_results,
        }
