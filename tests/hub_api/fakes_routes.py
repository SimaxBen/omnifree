"""Fakes for the cron / rerank / transcribe / direct-upload hub routes (test code only)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import ClassVar

from core.interfaces import (
    Capability,
    CronAdapter,
    CronJob,
    DirectUpload,
    HealthStatus,
    ProviderError,
    RerankRequest,
    RerankResponse,
    RerankResult,
    TranscriptionRequest,
    TranscriptionResponse,
)
from tests.hub_api.fakes import FakeLLM, FakeStorage, _fail

JOBS: dict[str, dict[str, CronJob]] = defaultdict(dict)
TRIGGERS: list[tuple[str, str]] = []
TRANSCRIBED: list[TranscriptionRequest] = []
UPLOAD_TARGETS: list[tuple[str, str, str, int | None]] = []


def reset() -> None:
    JOBS.clear()
    TRIGGERS.clear()
    TRANSCRIBED.clear()
    UPLOAD_TARGETS.clear()


class FakeCron(CronAdapter):
    async def create(self, job: CronJob) -> CronJob:
        _fail(self.name, self.setting("behaviour"))
        stored = job.model_copy(update={"id": f"{self.name}-{len(JOBS[self.name]) + 1}", "provider": self.name})
        JOBS[self.name][stored.id] = stored
        return stored

    async def list(self) -> list[CronJob]:
        _fail(self.name, self.setting("behaviour"))
        return list(JOBS[self.name].values())

    async def delete(self, job_id: str) -> None:
        if job_id not in JOBS[self.name]:
            raise ProviderError(f"job {job_id} not found", provider=self.name, status_code=404)
        del JOBS[self.name][job_id]

    async def trigger(self, job_id: str) -> None:
        if job_id not in JOBS[self.name]:
            raise ProviderError(f"job {job_id} not found", provider=self.name, status_code=404)
        TRIGGERS.append((self.name, job_id))

    async def health(self) -> HealthStatus:
        return HealthStatus(provider=self.name, ok=self.setting("behaviour") is None)


class FakeNoTriggerCron(FakeCron):
    capabilities: ClassVar[frozenset[str]] = frozenset()


class FakeAudioLLM(FakeLLM):
    """LLM with rerank + transcribe (like Cohere / Groq Whisper)."""

    capabilities: ClassVar[frozenset[str]] = FakeLLM.capabilities | {Capability.RERANK, Capability.TRANSCRIBE}

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        _fail(self.name, self.setting("behaviour"))
        q = request.query.lower()
        scored = sorted(
            ((i, float(sum(w in d.lower() for w in q.split()))) for i, d in enumerate(request.documents)),
            key=lambda t: -t[1],
        )
        top = scored[: request.top_n] if request.top_n else scored
        return RerankResponse(
            model=request.model or "rerank-fake",
            results=[RerankResult(index=i, relevance_score=s) for i, s in top],
            routed_via=self._routed(),
        )

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        _fail(self.name, self.setting("behaviour"))
        TRANSCRIBED.append(request)
        return TranscriptionResponse(
            model=request.model or "whisper-fake", text=f"{len(request.audio)} bytes", routed_via=self._routed()
        )


class FakeDirectStorage(FakeStorage):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.DIRECT_UPLOAD})

    async def upload_target(
        self, key: str, content_type: str, size: int | None = None, expires_s: int = 3600
    ) -> DirectUpload:
        _fail(self.name, self.setting("behaviour"))
        UPLOAD_TARGETS.append((self.name, key, content_type, size))
        return DirectUpload(
            provider=self.name, key=key, method="PUT", url=f"https://{self.name}.test/{key}?sig=x",
            headers={"Content-Type": content_type},
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
