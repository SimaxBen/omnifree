"""/rag: Ask my Drive. Index Drive files into Turso (libSQL vectors) and answer questions with citations.

* ``POST /rag/index`` handles one file per call (fits a 60 s serverless request): download through
  the storage router, extract text (``hub_api.rag_extract``), chunk, embed in batches through the
  router embed path, then replace the file's chunks in Turso (``hub_api.rag_store``).
* ``POST /rag/ask`` embeds the question, takes the 30 nearest chunks by cosine distance, reranks
  them through the router rerank path (Cohere; vector order when unavailable) and asks the router
  chat for an answer grounded in the numbered sources.

The embedding model (and its dimension) is fixed by the first indexed file. Embeddings from a
different model are refused; deleting every indexed document resets it.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from core.interfaces import (
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    ProviderError,
    ProviderKind,
    RerankRequest,
)
from hub_api.deps import spec_of_kind
from hub_api.rag_extract import (
    MIME_BY_EXTENSION,
    SUPPORTED_EXTENSIONS,
    ExtractionError,
    chunk_pages,
    extension_of,
    extract,
)
from hub_api.rag_store import EmbeddingMeta, RagStore, SearchHit
from hub_api.state import HubState, get_state

__all__ = ["router", "IndexBody", "AskBody", "NOT_FOUND_ANSWER"]

router = APIRouter(prefix="/rag", tags=["rag"])

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_CHUNKS = 400
EMBED_BATCH = 32
EMBED_CONCURRENCY = 4
RETRIEVE_K = 30
SNIPPET_CHARS = 300
NOT_FOUND_ANSWER = "I could not find this in your indexed documents."

SYSTEM_PROMPT = (
    "You answer questions using ONLY the numbered sources from the user's own documents.\n"
    "- Cite every fact with its source marker like [1] or [2][3], placed right after the claim.\n"
    "- If the sources do not contain the answer, say clearly that the answer is not in the "
    "documents; do not guess and do not use outside knowledge.\n"
    "- Answer in the language of the question, concisely, in Markdown."
)


class IndexBody(BaseModel):
    provider: str = Field(min_length=1)
    key: str = Field(min_length=1)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(8, ge=1, le=20)
    keys: list[str] | None = None


def _db_provider() -> str:
    return os.environ.get("OMNIFREE_RAG_DB_PROVIDER", "turso")


async def _store(state: HubState) -> RagStore:
    name = _db_provider()
    adapter = await state.router.adapter(name)  # NotConfigured -> 409 via the error handlers
    store = RagStore(adapter, timeout_s=state.registry.timeout_for(name))  # type: ignore[arg-type]
    await store.ensure_schema()
    return store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _doc_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": row.get("provider"),
        "key": row.get("key"),
        "title": row.get("title"),
        "mime": row.get("mime"),
        "size": row.get("size"),
        "chunks": int(row.get("chunks") or 0),
        "indexed_at": row.get("indexed_at"),
        "error": row.get("error"),
    }


def _norm_model(model: str) -> str:
    model = model.strip().lower()
    return model[len("models/"):] if model.startswith("models/") else model


def _base_provider(routed_via: str) -> str:
    return routed_via.split(":", 1)[0]


async def _embed(
    state: HubState, texts: list[str], meta: EmbeddingMeta | None, input_type: str
) -> tuple[list[list[float]], EmbeddingMeta]:
    """Embed ``texts`` in batches with one model; the first batch fixes it when ``meta`` is None."""
    batches = [texts[i : i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
    results: list[list[list[float]] | None] = [None] * len(batches)

    def check(vectors: list[list[float]], model: str, expected: EmbeddingMeta) -> None:
        if _norm_model(model) != _norm_model(expected.model) or any(len(v) != expected.dim for v in vectors):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=(
                    f"documents are indexed with embedding model '{expected.model}' ({expected.dim} dims) "
                    f"but '{model}' answered; it is unavailable right now. Retry later, or delete every "
                    "indexed document and re-index to switch models."
                ),
            )

    async def call(batch: list[str], pinned: EmbeddingMeta | None) -> tuple[list[list[float]], str, str]:
        request = EmbeddingRequest(
            input=batch,
            model=pinned.model if pinned else None,
            provider=pinned.provider if pinned and pinned.provider else None,
            input_type=input_type,
        )
        resp = await state.router.embed(request)
        if len(resp.vectors) != len(batch):
            raise ProviderError(
                f"embedding returned {len(resp.vectors)} vectors for {len(batch)} inputs",
                provider=resp.routed_via or None,
            )
        return resp.vectors, resp.model or (pinned.model if pinned else ""), _base_provider(resp.routed_via)

    start = 0
    if meta is None:
        vectors, model, provider = await call(batches[0], None)
        if not vectors or not vectors[0]:
            raise ProviderError("embedding provider returned empty vectors", provider=provider or None)
        meta = EmbeddingMeta(model=model or "unknown", provider=provider, dim=len(vectors[0]))
        check(vectors, model or "unknown", meta)
        results[0] = vectors
        start = 1
    fixed = meta
    sem = asyncio.Semaphore(EMBED_CONCURRENCY)

    async def one(i: int) -> None:
        async with sem:
            vectors, model, _provider = await call(batches[i], fixed)
        check(vectors, model, fixed)
        results[i] = vectors

    await asyncio.gather(*(one(i) for i in range(start, len(batches))))
    flat = [v for batch in results for v in (batch or [])]
    return flat, fixed


async def _current_meta(store: RagStore) -> EmbeddingMeta | None:
    """Embedding model in use; forgotten (and the chunk table dropped) once nothing is indexed."""
    meta = await store.meta()
    if meta is not None and await store.indexed_count() == 0:
        await store.reset_vectors()
        return None
    return meta


# --------------------------------------------------------------------------- routes


@router.get("/docs")
async def list_docs(state: HubState = Depends(get_state)) -> dict[str, Any]:
    store = await _store(state)
    meta = await store.meta()
    return {"docs": [_doc_json(r) for r in await store.list_docs()], "embedding_model": meta.model if meta else ""}


@router.post("/index")
async def index(body: IndexBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, body.provider, ProviderKind.STORAGE)
    store = await _store(state)
    ext = extension_of(body.key)
    doc: dict[str, Any] = {
        "provider": body.provider,
        "key": body.key,
        "title": posixpath.basename(body.key) or body.key,
        "mime": MIME_BY_EXTENSION.get(ext, "application/octet-stream"),
        "size": None,
        "chunks": 0,
        "indexed_at": _now(),
        "error": None,
    }

    async def fail(message: str) -> dict[str, Any]:
        meta = await store.meta()
        if meta is not None:
            await store.delete_chunks(body.provider, body.key)
        doc["error"] = message
        await store.upsert_doc(doc)
        return {"doc": _doc_json(doc)}

    if ext not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        return await fail(f"unsupported file type '{ext or '(none)'}' (supported: {supported})")

    obj = await state.router.storage_stat(body.key, body.provider)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"'{body.key}' not found on {body.provider}")
    doc["size"] = obj.size or None
    if obj.content_type and obj.content_type != "application/octet-stream":
        doc["mime"] = obj.content_type
    if obj.size and obj.size > MAX_FILE_BYTES:
        return await fail(f"file too large to index ({obj.size} bytes > {MAX_FILE_BYTES})")

    data = await state.router.storage_get(body.key, body.provider)
    doc["size"] = len(data)
    if len(data) > MAX_FILE_BYTES:
        return await fail(f"file too large to index ({len(data)} bytes > {MAX_FILE_BYTES})")

    try:
        extracted = await asyncio.to_thread(extract, body.key, data)
    except ExtractionError as exc:
        return await fail(str(exc))
    if extracted.title:
        doc["title"] = extracted.title[:300]
    chunks = chunk_pages(extracted.pages, max_chunks=MAX_CHUNKS)
    if not chunks:
        return await fail("no extractable text")

    meta = await _current_meta(store)
    vectors, used = await _embed(state, [c.text for c in chunks], meta, "search_document")
    if meta is None:
        await store.adopt(used)
    else:
        await store.delete_chunks(body.provider, body.key)
    await store.insert_chunks(body.provider, body.key, [(c.ord, c.page, c.text) for c in chunks], vectors)
    doc["chunks"] = len(chunks)
    doc["indexed_at"] = _now()
    await store.upsert_doc(doc)
    return {"doc": _doc_json(doc)}


@router.delete("/docs/{provider}/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_doc(provider: str, key: str, state: HubState = Depends(get_state)) -> Response:
    store = await _store(state)
    meta = await store.meta()
    if not await store.delete_doc(provider, key, has_chunks_table=meta is not None):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"'{key}' on {provider} is not indexed")
    await _current_meta(store)  # drops the vectors/model once nothing is indexed
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _not_found() -> dict[str, Any]:
    return {"answer": NOT_FOUND_ANSWER, "sources": [], "routed_via": ""}


async def _rerank(state: HubState, question: str, hits: list[SearchHit], top_k: int) -> list[tuple[SearchHit, float]]:
    try:
        resp = await state.router.rerank(
            RerankRequest(query=question, documents=[h.text for h in hits], top_n=min(top_k, len(hits)))
        )
        ranked = [(hits[r.index], float(r.relevance_score)) for r in resp.results if 0 <= r.index < len(hits)]
        if ranked:
            ranked.sort(key=lambda item: item[1], reverse=True)
            return ranked[:top_k]
    except ProviderError:
        pass  # no rerank provider (or quota): keep the vector order
    return [(h, round(1.0 - h.distance, 6)) for h in hits[:top_k]]


def _source_label(n: int, hit: SearchHit) -> str:
    title = hit.title or posixpath.basename(hit.key)
    page = f", page {hit.page}" if hit.page is not None else ""
    return f"[{n}] {title}{page}"


@router.post("/ask")
async def ask(body: AskBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    store = await _store(state)
    meta = await store.meta()
    if meta is None:
        return _not_found()
    (qvec,), _meta = await _embed(state, [body.question], meta, "search_query")
    hits = await store.search(qvec, limit=RETRIEVE_K, keys=body.keys or None)
    if not hits:
        return _not_found()
    ranked = await _rerank(state, body.question, hits, body.top_k)

    sources: list[dict[str, Any]] = []
    blocks: list[str] = []
    for n, (hit, score) in enumerate(ranked, start=1):
        sources.append({
            "n": n,
            "provider": hit.provider,
            "key": hit.key,
            "title": hit.title or posixpath.basename(hit.key),
            "page": hit.page,
            "snippet": hit.text[:SNIPPET_CHARS],
            "score": round(score, 6),
        })
        blocks.append(f"{_source_label(n, hit)}\n{hit.text}")
    user = (
        "Sources:\n\n" + "\n\n---\n\n".join(blocks)
        + f"\n\n---\n\nQuestion: {body.question}\n\n"
        "Answer from the sources above with [n] citations. If they do not contain the answer, "
        f"reply: \"{NOT_FOUND_ANSWER}\""
    )
    resp = await state.router.chat(
        ChatRequest(
            messages=[ChatMessage(role="system", content=SYSTEM_PROMPT), ChatMessage(role="user", content=user)],
            temperature=0.2,
            max_tokens=1200,
        )
    )
    return {"answer": (resp.content or "").strip() or NOT_FOUND_ANSWER, "sources": sources, "routed_via": resp.routed_via}
