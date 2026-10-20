"""Pi-hole v6 DNS adapter (REST API under ``/api``).

Authentication: ``POST /api/auth {"password": ...}`` returns a session id (``sid``) sent as
``X-FTL-SID``; the session is cached for its ``validity`` and renewed once on 401. ``close()``
logs the session out (Pi-hole limits concurrent API seats).

* ``status()``: ``GET /api/dns/blocking`` + ``GET /api/stats/summary``.
* ``toggle_blocking(enabled, duration_s)``: ``POST /api/dns/blocking {"blocking", "timer"}``.
* ``query_log(limit, blocked_only)``: ``GET /api/queries?length=N``; blocked entries are those
  whose status is in ``settings.blocked_statuses`` (default: Pi-hole's gravity/regex/denylist
  and upstream-blocked statuses), filtered client side.
* ``list_records()``: local DNS hosts and CNAMEs from ``GET /api/config/dns``.

Settings: ``base_url`` (e.g. ``http://amd-vm1`` over Tailscale), ``api_version`` (must be 6),
``verify_tls`` (default true).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import httpx

from core.interfaces import (
    Capability,
    DNSAdapter,
    DNSQueryLogEntry,
    DNSRecord,
    DNSStatus,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderAuthError,
    ProviderError,
)

DEFAULT_BLOCKED = (
    "GRAVITY", "REGEX", "DENYLIST", "GRAVITY_CNAME", "REGEX_CNAME", "DENYLIST_CNAME",
    "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXRA", "EXTERNAL_BLOCKED_EDE15",
    "SPECIAL_DOMAIN",
)


class PiHoleDNSAdapter(DNSAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.DNS_RECORDS})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Pi-hole",
        description="Pi-hole v6 REST API: blocking toggle, statistics and query log.",
        required_secrets=("password",),
        docs_url="https://docs.pi-hole.net/api/",
    )

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._sid: str | None = None
        self._csrf: str | None = None
        self._sid_expires = 0.0
        self._authenticated = False
        self._lock = asyncio.Lock()

    def build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.context.timeout_s,
            transport=self.context.http_transport,
            follow_redirects=True,
            verify=bool(self.setting("verify_tls", True)),
        )

    def _api(self, path: str) -> str:
        if int(self.setting("api_version", 6)) != 6:
            raise NotConfigured("only the Pi-hole v6 API is supported (settings.api_version: 6)",
                                provider=self.name)
        return str(self.require_setting("base_url")).rstrip("/") + "/api" + path

    # ------------------------------------------------------------------ session
    async def _login(self, force: bool = False) -> dict[str, str]:
        async with self._lock:
            if not force and self._authenticated and time.time() < self._sid_expires - 10:
                return self._session_headers()
            response = await self.request("POST", self._api("/auth"),
                                          json={"password": self.require_secret("password")})
            session = response.json().get("session") or {}
            if not session.get("valid"):
                raise ProviderAuthError(str(session.get("message") or "Pi-hole rejected the password"),
                                        provider=self.name, status_code=401)
            self._sid = session.get("sid")
            self._csrf = session.get("csrf")
            self._sid_expires = time.time() + float(session.get("validity") or 300)
            self._authenticated = True
            return self._session_headers()

    def _session_headers(self) -> dict[str, str]:
        headers = {}
        if self._sid:
            headers["X-FTL-SID"] = self._sid
        if self._csrf:
            headers["X-FTL-CSRF"] = self._csrf
        return headers

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in (0, 1):
            headers = await self._login(force=attempt == 1)
            try:
                response = await self.request(method, self._api(path), headers=headers, **kwargs)
            except ProviderAuthError:
                if attempt == 0:
                    continue
                raise
            return response.json() if response.content else {}
        raise ProviderAuthError("Pi-hole session could not be established", provider=self.name)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/dns/blocking"))

    def _status(self, blocking: dict[str, Any], summary: dict[str, Any] | None) -> DNSStatus:
        timer = blocking.get("timer")
        queries = (summary or {}).get("queries") or {}
        gravity = (summary or {}).get("gravity") or {}
        return DNSStatus(
            provider=self.name,
            blocking_enabled=blocking.get("blocking") == "enabled",
            blocking_resumes_at=(datetime.now(timezone.utc) + timedelta(seconds=float(timer))) if timer else None,
            queries_today=queries.get("total"),
            blocked_today=queries.get("blocked"),
            percent_blocked=queries.get("percent_blocked"),
            domains_on_blocklist=gravity.get("domains_being_blocked"),
        )

    async def status(self) -> DNSStatus:
        blocking = await self._call("GET", "/dns/blocking")
        try:
            summary = await self._call("GET", "/stats/summary")
        except ProviderError as exc:
            if isinstance(exc, ProviderAuthError):
                raise
            summary = None
        return self._status(blocking, summary)

    async def toggle_blocking(self, enabled: bool, duration_s: int | None = None) -> DNSStatus:
        body = {"blocking": bool(enabled), "timer": int(duration_s) if duration_s else None}
        blocking = await self._call("POST", "/dns/blocking", json=body)
        try:
            summary = await self._call("GET", "/stats/summary")
        except ProviderError as exc:
            if isinstance(exc, ProviderAuthError):
                raise
            summary = None
        return self._status(blocking, summary)

    async def query_log(self, limit: int = 100, blocked_only: bool = False) -> list[DNSQueryLogEntry]:
        blocked_statuses = {str(s).upper() for s in self.setting("blocked_statuses", DEFAULT_BLOCKED)}
        length = min(limit * 10, 5000) if blocked_only else limit
        payload = await self._call("GET", "/queries", params={"length": length})
        entries = []
        for item in payload.get("queries") or []:
            status = str(item.get("status") or "").upper()
            blocked = status in blocked_statuses
            if blocked_only and not blocked:
                continue
            client = item.get("client") or {}
            entries.append(DNSQueryLogEntry(
                timestamp=datetime.fromtimestamp(float(item.get("time") or 0), tz=timezone.utc),
                domain=str(item.get("domain") or ""),
                client=(client.get("name") or client.get("ip")) if isinstance(client, dict) else str(client),
                query_type=item.get("type"),
                status=status or None,
                blocked=blocked,
            ))
            if len(entries) >= limit:
                break
        return entries

    async def list_records(self) -> list[DNSRecord]:
        payload = await self._call("GET", "/config/dns")
        dns = ((payload.get("config") or {}).get("dns")) or {}
        records = []
        for line in dns.get("hosts") or []:
            parts = str(line).split()
            if len(parts) >= 2:
                ip = parts[0]
                for host in parts[1:]:
                    records.append(DNSRecord(name=host, type="AAAA" if ":" in ip else "A", value=ip))
        for line in dns.get("cnameRecords") or []:
            parts = [p.strip() for p in str(line).split(",")]
            if len(parts) >= 2:
                ttl = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                records.append(DNSRecord(name=parts[0], type="CNAME", value=parts[1], ttl=ttl))
        return records

    async def close(self) -> None:
        if self._authenticated and self._sid:
            # Logout is best effort: the session expires on its own anyway.
            with contextlib.suppress(httpx.HTTPError, ProviderError):
                await self.http.request("DELETE", self._api("/auth"), headers=self._session_headers())
        self._authenticated = False
        self._sid = None
        await super().close()
