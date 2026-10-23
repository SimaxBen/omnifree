"""/drive: unified storage across the storage chain (R2, B2, GCS, Oracle block volume ...)."""

from __future__ import annotations

import asyncio
import mimetypes
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from core.interfaces import ProviderError, ProviderKind, StoredObject
from hub_api.appdata import AppDB
from hub_api.deps import spec_of_kind, with_adapter
from hub_api.routers import notes
from hub_api.state import HubState, get_state

__all__ = ["router", "normalise_key", "UploadUrlBody", "TranscribeBody", "TRANSCRIBE_MAX_BYTES"]

router = APIRouter(prefix="/drive", tags=["drive"])

_READ_CHUNK = 1024 * 1024


def normalise_key(key: str) -> str:
    """Object keys are ``/``-separated relative paths without empty or dot segments."""
    parts = [p for p in key.replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or any(ord(c) < 32 for c in key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"invalid object key {key!r}")
    return "/".join(parts)


def _obj(o: StoredObject) -> dict[str, Any]:
    return o.model_dump(mode="json")


async def _listing(
    state: HubState, prefix: str, provider: str | None, limit: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    providers = [spec_of_kind(state, provider, ProviderKind.STORAGE).name] if provider else None
    raw = await state.router.storage_list(prefix, providers=providers, limit=limit)
    items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for name, result in raw.items():
        if isinstance(result, str):
            errors[name] = result
            continue
        items.extend(_obj(o) for o in result)
    items.sort(key=lambda o: (o["key"], o["backend"]))
    return items, errors


@router.get("/usage")
async def usage(state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Per-backend used/limit bytes (quota bars) plus pool totals."""
    backends: list[dict[str, Any]] = []
    total_used = 0.0
    total_limit = 0.0
    for spec in state.registry.list(ProviderKind.STORAGE, enabled_only=True):
        cap = await state.router.storage_capacity(spec.name)
        backends.append(
            {
                "provider": spec.name,
                "description": spec.description,
                "used": cap.used,
                "limit": cap.limit,
                "remaining": cap.remaining,
                "fraction_used": cap.fraction_used,
                "source": cap.source,
            }
        )
        total_used += cap.used
        total_limit += cap.limit or 0.0
    return {
        "backends": backends,
        "total": {"used": total_used, "limit": total_limit or None},
        "strategy": state.registry.defaults.storage_strategy,
    }


@router.get("/files")
async def list_files(
    prefix: str = "",
    provider: str | None = None,
    limit: int = Query(1000, ge=1, le=10000),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    items, errors = await _listing(state, prefix, provider, limit)
    return {"items": items, "count": len(items), "errors": errors}


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    prefix: str = "",
    limit: int = Query(200, ge=1, le=5000),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    """Case-insensitive substring match on keys and metadata values across every backend."""
    items, errors = await _listing(state, prefix, None, 10000)
    needle = q.lower()
    hits = [
        o for o in items
        if needle in o["key"].lower() or any(needle in str(v).lower() for v in (o.get("metadata") or {}).values())
    ]
    return {"query": q, "items": hits[:limit], "count": len(hits), "errors": errors}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile = File(...),
    key: str | None = Form(None),
    prefix: str = Form(""),
    provider: str | None = Form(None),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    """Store a file on the backend with the most free space (or the pinned ``provider``)."""
    if provider:
        spec_of_kind(state, provider, ProviderKind.STORAGE)
    target = normalise_key(key or f"{prefix.rstrip('/')}/{file.filename or 'upload.bin'}")
    limit = state.hub.max_upload_bytes
    buf = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, detail=f"file exceeds {limit} bytes")
    content_type = file.content_type or mimetypes.guess_type(target)[0] or "application/octet-stream"
    metadata = {"original-filename": file.filename} if file.filename else None
    obj = await state.router.storage_put(
        target, bytes(buf), content_type, metadata, provider=provider, allow_fallback=provider is None
    )
    return _obj(obj)


class UploadUrlBody(BaseModel):
    filename: str = Field(min_length=1)
    size: int | None = Field(None, ge=0)
    content_type: str | None = None
    prefix: str = ""
    key: str | None = None
    provider: str | None = None


@router.post("/upload-url")
async def upload_url(body: UploadUrlBody, state: HubState = Depends(get_state)) -> dict[str, Any]:
    """Direct-upload instructions (bypasses the hub/serverless body limit).

    The backend is chosen like ``/drive/upload`` (most free space, or the pinned
    ``provider``); the client then sends the bytes with ``method`` to ``url`` adding ``headers``.
    """
    if body.provider:
        spec_of_kind(state, body.provider, ProviderKind.STORAGE)
    target = normalise_key(body.key or f"{body.prefix.rstrip('/')}/{body.filename}")
    content_type = body.content_type or mimetypes.guess_type(target)[0] or "application/octet-stream"
    result = await state.router.storage_upload_target(target, content_type, body.size, provider=body.provider)
    return result.model_dump(mode="json")


@router.get("/locate/{key:path}")
async def locate(key: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    obj = await state.router.storage_locate(normalise_key(key))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="object not found on any backend")
    return _obj(obj)


@router.get("/files/{provider}/{key:path}")
async def download(
    provider: str,
    key: str,
    inline: bool = False,
    state: HubState = Depends(get_state),
) -> Response:
    spec_of_kind(state, provider, ProviderKind.STORAGE)
    target = normalise_key(key)
    data: bytes = await with_adapter(state, provider, lambda a: a.get(target))
    media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    filename = target.rsplit("/", 1)[-1]
    disposition = "inline" if inline else "attachment"
    return Response(
        data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "X-Storage-Backend": provider,
            # Stored files are untrusted: an inline HTML/SVG opened from the hub origin must not
            # run script (the URL may carry ?access_token=). Sandbox it and forbid sniffing.
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; media-src 'self'; "
            "style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/files/{provider}/{key:path}")
async def delete(provider: str, key: str, state: HubState = Depends(get_state)) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.STORAGE)
    target = normalise_key(key)
    await state.router.storage_delete(target, provider)
    return {"deleted": target, "provider": provider}


@router.get("/presign/{provider}/{key:path}")
async def presign(
    provider: str,
    key: str,
    expires_s: int = Query(3600, ge=60, le=7 * 24 * 3600),
    method: str = Query("GET", pattern="^(GET|PUT)$"),
    state: HubState = Depends(get_state),
) -> dict[str, Any]:
    spec_of_kind(state, provider, ProviderKind.STORAGE)
    target = normalise_key(key)
    url = await with_adapter(state, provider, lambda a: a.presign(target, expires_s, method))
    return {"url": url, "provider": provider, "key": target, "expires_s": expires_s, "method": method}


# ------------------------------------------------------------------ transcribe a Drive audio file into a note

TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024  # Groq Whisper's upload limit
TRANSCRIBE_BUDGET_S = 55.0  # the serverless hub stops a request at 60 s
TRANSCRIBE_DOWNLOAD_S = 25.0


class TranscribeBody(BaseModel):
    provider: str = Field(min_length=1)
    key: str = Field(min_length=1)
    notebook: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=12)


def _too_big(size: int) -> HTTPException:
    return HTTPException(
        413,
        detail=f"This file is {size / 1048576:.1f} MB; transcription takes audio up to "
        f"{TRANSCRIBE_MAX_BYTES // 1048576} MB. Trim or compress it (e.g. to mono Opus/MP3) first.",
    )


def transcript_note(key: str, provider: str, transcript: str) -> tuple[str, str]:
    """(title, markdown body) of the note made from a Drive file's transcript."""
    name = key.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    stem = " ".join(stem.replace("_", " ").split()) or name
    title = f"{stem} (transcript)"[:300]
    body = f"{transcript}\n\n---\n_Transcribed from the Drive file `{key}` on {provider}._"
    return title, body


@router.post("/transcribe", status_code=status.HTTP_201_CREATED)
async def transcribe_file(
    body: TranscribeBody,
    state: HubState = Depends(get_state),
    db: AppDB = Depends(notes.notes_db),
) -> dict[str, Any]:
    """Transcribe an audio file already in the Drive and save the transcript as a note: ``{note, transcript}``.

    The hub reads the file from the backend itself (no upload body limit); Whisper takes up to 25 MB.
    The note keeps the file as its audio (``source: voice``, ``audio_key``) and links back to it.
    """
    started = time.monotonic()
    spec_of_kind(state, body.provider, ProviderKind.STORAGE)
    target = normalise_key(body.key)
    try:
        meta = await state.router.storage_stat(target, body.provider)
    except ProviderError:
        meta = None  # some backends cannot stat; the size is checked after the download instead
    if meta is not None and meta.size > TRANSCRIBE_MAX_BYTES:
        raise _too_big(meta.size)
    data: bytes = await with_adapter(state, body.provider, lambda a: a.get(target), timeout_s=TRANSCRIBE_DOWNLOAD_S)
    if len(data) > TRANSCRIBE_MAX_BYTES:
        raise _too_big(len(data))
    if not data:
        raise HTTPException(422, detail="the file is empty")
    remaining = TRANSCRIBE_BUDGET_S - (time.monotonic() - started)
    try:
        transcript = await asyncio.wait_for(
            notes.transcribe_audio(state, data, target.rsplit("/", 1)[-1], body.language), max(remaining, 1.0)
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            504, detail="Transcription took too long for one request. Try a shorter recording."
        ) from None
    title, text = transcript_note(target, body.provider, transcript)
    note = await notes.insert_note(
        db, body=text, title=title, notebook=body.notebook, tags=["transcript"], pinned=False,
        source="voice", audio_key=target,
    )
    return {"note": note, "transcript": transcript}
