"""Test-only fakes for the Saved library: a "semantic" LLM (concept embeddings), a scripted web and a fake DNS.

* ``SavedLLM`` embeds words through a small synonym table (``football`` ~ ``soccer`` ~ ``foot``, ``nostalgic`` ~
  ``retro`` ...) so a description can match an item without sharing a single word; its rerank is the cosine of
  those embeddings. ``chat`` answers the annotation prompt with ``ANNOTATION`` (or raises when ``CHAT_FAIL``) and
  any other prompt with ``ANSWER``.
* ``WEB`` maps URLs to ``httpx.Response`` factories for an ``httpx.MockTransport``; ``SEEN`` records every request.
* ``resolve`` is the fake DNS: ``*.internal.test`` and ``metadata.test`` resolve to private / link-local addresses.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from collections.abc import Callable
from typing import Any, ClassVar

import httpx

from core.interfaces import (
    Capability,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ProviderUnavailable,
    RerankRequest,
    RerankResponse,
    RerankResult,
    Usage,
)
from tests.hub_api.fakes_rag import FakeRagLLM

ANNOTATION: dict[str, Any] = {}
ANSWER = {"text": "You saved a retro football anthem video [1]."}
CHAT_FAIL = {"on": False}
CHATS: list[ChatRequest] = []
WEB: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
SEEN: list[str] = []
EMBED_CALLS = {"n": 0}

_CONCEPTS = {
    "football": "SPORT", "soccer": "SPORT", "foot": "SPORT", "match": "SPORT", "kora": "SPORT",
    "nostalgic": "NOSTALGIA", "nostalgia": "NOSTALGIA", "retro": "NOSTALGIA", "nostalgique": "NOSTALGIA",
    "old": "NOSTALGIA", "throwback": "NOSTALGIA",
    "song": "MUSIC", "anthem": "MUSIC", "chanson": "MUSIC", "hymne": "MUSIC", "music": "MUSIC", "ghnya": "MUSIC",
    "manager": "MANAGE", "management": "MANAGE", "engineering": "ENGINEER", "engineers": "ENGINEER",
    "walk": "WALK", "walking": "WALK", "marcher": "WALK", "steps": "WALK", "pedometer": "WALK", "pas": "WALK",
    "productivity": "PRODUCTIVE", "productivite": "PRODUCTIVE", "focus": "PRODUCTIVE",
}
_WORD = re.compile(r"[a-z0-9]+")
DIM = 48


def reset() -> None:
    ANNOTATION.clear()
    ANSWER["text"] = "You saved a retro football anthem video [1]."
    CHAT_FAIL["on"] = False
    CHATS.clear()
    WEB.clear()
    SEEN.clear()
    EMBED_CALLS["n"] = 0


def _fold(text: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def concept_vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    vec[0] = 0.01
    for word in _WORD.findall(_fold(text)):
        key = _CONCEPTS.get(word, word)
        vec[1 + zlib.crc32(key.encode()) % (DIM - 1)] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class SavedLLM(FakeRagLLM):
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {Capability.CHAT, Capability.STREAM, Capability.EMBED, Capability.RERANK}
    )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        CHATS.append(request)
        system = str(request.messages[0].content or "")
        if "saved' library" in system:
            if CHAT_FAIL["on"]:
                raise ProviderUnavailable("llm down", provider=self.name, status_code=503)
            return ChatResponse(model="fake-chat", content="```json\n" + json.dumps(ANNOTATION) + "\n```",
                                usage=Usage(total_tokens=5))
        if CHAT_FAIL["on"]:
            raise ProviderUnavailable("llm down", provider=self.name, status_code=503)
        return ChatResponse(model="fake-chat", content=ANSWER["text"], usage=Usage(total_tokens=5))

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        EMBED_CALLS["n"] += 1
        return EmbeddingResponse(model="fake-embed", vectors=[concept_vector(t) for t in request.input])

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        q = concept_vector(request.query)
        scored = [RerankResult(index=i, relevance_score=max(_cos(q, concept_vector(d)), 0.0))
                  for i, d in enumerate(request.documents)]
        scored.sort(key=lambda r: r.relevance_score, reverse=True)
        return RerankResponse(model="fake-rerank", results=scored[: request.top_n or len(scored)])


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    SEEN.append(url)
    for prefix, make in WEB.items():
        if url == prefix or url.startswith(prefix):
            return make(request)
    return httpx.Response(404, text="not found")


def html(body: str, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def json_response(data: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, json=data)


def redirect(location: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(302, headers={"location": location})


def image(data: bytes, ctype: str = "image/png") -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, content=data, headers={"content-type": ctype})


async def resolve(host: str) -> list[str]:
    if host.endswith(".internal.test") or host == "intranet.test":
        return ["10.0.0.5"]
    if host == "metadata.test":
        return ["169.254.169.254"]
    if host == "v6local.test":
        return ["::1"]
    if host == "nxdomain.test":
        raise OSError("no such host")
    return ["93.184.216.34"]
