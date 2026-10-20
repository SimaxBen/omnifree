"""Upstash QStash schedules as a CronAdapter (QStash API v2).

* ``create(job)``: ``POST /v2/schedules/<target URL>`` with ``Upstash-Cron``,
  ``Upstash-Method``, ``Upstash-Forward-*`` headers (``job.headers``) and the body. The job
  name travels as the forwarded header ``X-Omnifree-Job`` so ``list()`` can show it.
  ``job.enabled = False`` pauses the schedule right after creation.
* ``list()`` / ``delete(id)``: ``GET`` / ``DELETE /v2/schedules[/<id>]``.
* ``trigger(id)``: publishes one message to the schedule's destination now
  (``POST /v2/publish/<destination>``) with the schedule's method, headers and body.
* ``verify_signature(...)``: validates the ``Upstash-Signature`` JWT that QStash sends to
  receivers, using the optional ``current_signing_key``/``next_signing_key`` secrets.

Settings: ``base_url`` (default ``https://qstash.upstash.io``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import quote

from core.interfaces import (
    CronAdapter,
    CronJob,
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderError,
)

NAME_HEADER = "X-Omnifree-Job"
_FORWARD = "Upstash-Forward-"


def _ms(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class QStashCronAdapter(CronAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Upstash QStash",
        description="QStash HTTP cron schedules; trigger() publishes a message now.",
        required_secrets=("token",),
        optional_secrets=("current_signing_key", "next_signing_key"),
        docs_url="https://upstash.com/docs/qstash/api/schedules/create",
    )

    def _base(self) -> str:
        return str(self.setting("base_url", "https://qstash.upstash.io")).rstrip("/")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.require_secret('token')}"}

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {**self._auth(), **kwargs.pop("headers", {})}
        response = await self.request(method, self._base() + path, headers=headers, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def _job(self, schedule: dict[str, Any]) -> CronJob:
        raw_headers = schedule.get("header") or {}
        headers = {str(k): (v[0] if isinstance(v, list) and v else str(v)) for k, v in raw_headers.items()}
        name = next((v for k, v in headers.items() if k.lower() == NAME_HEADER.lower()), None)
        headers = {k: v for k, v in headers.items() if k.lower() != NAME_HEADER.lower()}
        body = schedule.get("body")
        return CronJob(
            id=str(schedule.get("scheduleId")),
            name=name or str(schedule.get("scheduleId")),
            schedule=str(schedule.get("cron") or ""),
            target=str(schedule.get("destination") or ""),
            method=str(schedule.get("method") or "POST"),
            headers=headers,
            body=None if body in (None, "") else str(body),
            enabled=not schedule.get("isPaused", False),
            provider=self.name,
            last_run_at=_ms(schedule.get("lastScheduleTime")),
            next_run_at=_ms(schedule.get("nextScheduleTime")),
        )

    @staticmethod
    def _forward(headers: dict[str, str]) -> dict[str, str]:
        out = {}
        for name, value in headers.items():
            if name.lower() == "content-type":
                out["Content-Type"] = value
            else:
                out[f"{_FORWARD}{name}"] = value
        return out

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self._call("GET", "/v2/schedules"))

    async def create(self, job: CronJob) -> CronJob:
        if not job.target.startswith(("https://", "http://")):
            raise ProviderError("QStash schedules need an http(s) URL target", provider=self.name,
                                status_code=400)
        headers = {
            "Upstash-Cron": job.schedule,
            "Upstash-Method": job.method.upper(),
            **self._forward({**job.headers, NAME_HEADER: job.name}),
        }
        if job.id:
            headers["Upstash-Schedule-Id"] = job.id
        payload = await self._call("POST", f"/v2/schedules/{job.target}", headers=headers,
                                   content=(job.body or "").encode("utf-8"))
        schedule_id = str(payload.get("scheduleId") or job.id)
        if not job.enabled:
            await self.pause(schedule_id)
        return job.model_copy(update={"id": schedule_id, "provider": self.name})

    async def list(self) -> list[CronJob]:
        schedules = await self._call("GET", "/v2/schedules")
        return [self._job(s) for s in schedules or []]

    async def get(self, job_id: str) -> CronJob:
        return self._job(await self._call("GET", f"/v2/schedules/{quote(job_id, safe='')}"))

    async def delete(self, job_id: str) -> None:
        await self._call("DELETE", f"/v2/schedules/{quote(job_id, safe='')}")

    async def trigger(self, job_id: str) -> None:
        job = await self.get(job_id)
        headers = {"Upstash-Method": job.method.upper(),
                   **self._forward({**job.headers, NAME_HEADER: job.name})}
        await self._call("POST", f"/v2/publish/{job.target}", headers=headers,
                         content=(job.body or "").encode("utf-8"))

    # ------------------------------------------------------------------ extras
    async def pause(self, job_id: str) -> None:
        await self._call("POST", f"/v2/schedules/{quote(job_id, safe='')}/pause")

    async def resume(self, job_id: str) -> None:
        await self._call("POST", f"/v2/schedules/{quote(job_id, safe='')}/resume")

    def verify_signature(self, signature: str, body: bytes, url: str | None = None,
                         clock_tolerance_s: int = 0) -> bool:
        """Verify an ``Upstash-Signature`` JWT (HS256) against the current or next signing key."""
        keys = [k for k in (self.secret("current_signing_key"), self.secret("next_signing_key")) if k]
        if not keys:
            raise NotConfigured("QSTASH_CURRENT_SIGNING_KEY / QSTASH_NEXT_SIGNING_KEY are not set",
                                provider=self.name)
        try:
            header_b64, payload_b64, sig_b64 = signature.split(".")
            header = json.loads(_b64url_decode(header_b64))
            claims = json.loads(_b64url_decode(payload_b64))
            sig = _b64url_decode(sig_b64)
        except (ValueError, json.JSONDecodeError):
            return False
        if header.get("alg") != "HS256":
            return False
        signed = f"{header_b64}.{payload_b64}".encode("ascii")
        if not any(hmac.compare_digest(hmac.new(k.encode(), signed, hashlib.sha256).digest(), sig) for k in keys):
            return False
        now = time.time()
        if claims.get("iss") != "Upstash":
            return False
        if "exp" in claims and now > float(claims["exp"]) + clock_tolerance_s:
            return False
        if "nbf" in claims and now + clock_tolerance_s < float(claims["nbf"]):
            return False
        if url is not None and claims.get("sub") != url:
            return False
        expected = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
        return str(claims.get("body", "")).rstrip("=") == expected
