"""/links, /drops: short links and expiring file and folder drops, plus their PUBLIC pages.

``router`` (management, token protected) and ``public_router`` (``GET /s/{code}``,
``GET|POST /d/{code}``, ``GET /d/{code}/zip``, mounted WITHOUT the token dependency). Public URLs are
built from the ``public_base_url`` setting. Drop passwords are stored as salted scrypt hashes and
compared in constant time; downloads are 10-minute presigned GET URLs on the storage backend.

A folder drop (``POST /drops/folder``, ``kind = 'folder'``, ``key`` = the prefix ending in ``/``)
opens a page listing the files under the prefix with per-file presigned links; each opening counts
as one download. Only the files present when the drop was created are shared (``file_keys``, a JSON
list of ``[backend, key]``): files added to the folder later never show. When the folder is small
(``ZIP_MAX_BYTES``), the page also offers a ZIP built by the
hub, behind a 10-minute signed link: answered inline when small, otherwise stored under
``ZIP_PREFIX`` on a presign-capable backend and redirected to.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import re
import secrets
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from core.interfaces import Capability, ProviderError, ProviderKind, StoredObject
from hub_api.appdata import AppDB, get_appdb, iso, local_tz, random_code
from hub_api.deps import spec_of_kind
from hub_api.routers.drive import normalise_key
from hub_api.state import HubState, get_state

__all__ = ["router", "public_router", "hash_password", "verify_password", "zip_sig", "SCHEMA", "DROP_COLUMNS"]

log = logging.getLogger("omnifree.hub.links")

router = APIRouter(tags=["links"])
public_router = APIRouter(tags=["public"])

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_links ("
    " code TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, clicks INTEGER NOT NULL DEFAULT 0,"
    " created_at TEXT NOT NULL, expires_at TEXT)",
    "CREATE TABLE IF NOT EXISTS app_drops ("
    " code TEXT PRIMARY KEY, provider TEXT NOT NULL, key TEXT NOT NULL, filename TEXT NOT NULL,"
    " size INTEGER, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, max_downloads INTEGER,"
    " downloads INTEGER NOT NULL DEFAULT 0, password_hash TEXT)",
)
# Migration v2 (folder drops): kind 'file' (NULL on older rows) or 'folder'; a folder's file count at creation;
# the files it shares, snapshotted at creation (JSON list of [backend, key]).
DROP_COLUMNS = (("kind", "TEXT"), ("file_count", "INTEGER"), ("file_keys", "TEXT"))

LINK_CODE_LEN = 6
DROP_CODE_LEN = 8
PRESIGN_TTL_S = 600
FOLDER_MAX_FILES = 500  # listed on a folder page; the rest is reported as "and N more"
ZIP_MAX_BYTES = 25 * 1024 * 1024  # a ZIP is offered only below this total, to fit the 60 s request budget
ZIP_MAX_FILES = 300
ZIP_INLINE_MAX = 4 * 1024 * 1024  # serverless response limit: bigger ZIPs go through the storage backend
ZIP_BUDGET_S = 40.0
ZIP_PREFIX = "_shares/zips/"
ZIP_TOKEN_TTL_S = 600
ALL_BACKENDS = "*"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED = {"api", "admin", "s", "d", "links", "drops"}

# scrypt parameters (~16 MiB memory, a few tens of ms): fine for a handful of drop passwords.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    await db.ensure("links", SCHEMA)
    await db.add_columns("links_v2_folder_drops", "app_drops", DROP_COLUMNS)
    return db


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        got = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# --------------------------------------------------------------------------- helpers


def _base(state: HubState) -> str:
    return state.hub.public_base_url.rstrip("/")


def _expired(expires_at: str | None, now: datetime | None = None) -> bool:
    if not expires_at:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def _link_out(row: dict[str, Any], state: HubState) -> dict[str, Any]:
    return {
        "code": row["code"],
        "url": row["url"],
        "title": row.get("title"),
        "clicks": int(row.get("clicks") or 0),
        "created_at": row["created_at"],
        "expires_at": row.get("expires_at"),
        "short_url": f"{_base(state)}/s/{row['code']}",
    }


def _drop_out(row: dict[str, Any], state: HubState) -> dict[str, Any]:
    size = row.get("size")
    max_dl = row.get("max_downloads")
    count = row.get("file_count")
    return {
        "code": row["code"],
        "kind": row.get("kind") or "file",
        "file_count": None if count is None else int(count),
        "provider": row["provider"],
        "key": row["key"],
        "filename": row["filename"],
        "size": None if size is None else int(size),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "max_downloads": None if max_dl is None else int(max_dl),
        "downloads": int(row.get("downloads") or 0),
        "has_password": bool(row.get("password_hash")),
        "url": f"{_base(state)}/d/{row['code']}",
    }


async def _unique_code(db: AppDB, table: str, length: int) -> str:
    for _ in range(8):
        code = random_code(length)
        if await db.fetch_one(f"SELECT code FROM {table} WHERE code = ?", [code]) is None:
            return code
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="could not allocate a unique code, retry")


# --------------------------------------------------------------------------- links (protected)


class LinkCreate(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    code: str | None = None
    title: str | None = Field(None, max_length=300)
    expires_in_days: int | None = Field(None, ge=1, le=3650)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc or any(c in v for c in "\r\n\t "):
            raise ValueError("url must be an absolute http(s) URL")
        return v

    @field_validator("code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _CODE_RE.match(v):
            raise ValueError("code must be 1-64 characters of A-Z a-z 0-9 _ -")
        if v.lower() in _RESERVED:
            raise ValueError("code is reserved")
        return v


@router.get("/links")
async def list_links(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_links ORDER BY created_at DESC, code")
    return {"links": [_link_out(r, state) for r in rows]}


@router.post("/links", status_code=status.HTTP_201_CREATED)
async def create_link(body: LinkCreate, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    if body.code:
        if await db.fetch_one("SELECT code FROM app_links WHERE code = ?", [body.code]) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"code '{body.code}' is already taken")
        code = body.code
    else:
        code = await _unique_code(db, "app_links", LINK_CODE_LEN)
    now = datetime.now(timezone.utc)
    row = {
        "code": code,
        "url": body.url,
        "title": (body.title or "").strip() or None,
        "clicks": 0,
        "created_at": iso(now),
        "expires_at": iso(now + timedelta(days=body.expires_in_days)) if body.expires_in_days else None,
    }
    try:
        await db.execute(
            "INSERT INTO app_links (code, url, title, clicks, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            [row["code"], row["url"], row["title"], 0, row["created_at"], row["expires_at"]],
        )
    except ProviderError as exc:  # lost a race on the primary key
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"code '{code}' is already taken") from exc
        raise
    return _link_out(row, state)


@router.delete("/links/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(code: str, db: AppDB = Depends(_db)) -> Response:
    if await db.fetch_one("SELECT code FROM app_links WHERE code = ?", [code]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="link not found")
    await db.execute("DELETE FROM app_links WHERE code = ?", [code])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- drops (protected)


class DropCreate(BaseModel):
    provider: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=1024)
    expires_in_hours: int = Field(24, ge=1, le=24 * 365)
    max_downloads: int | None = Field(None, ge=1, le=1_000_000)
    password: str | None = Field(None, max_length=200)


@router.get("/drops")
async def list_drops(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    rows = await db.fetch("SELECT * FROM app_drops ORDER BY created_at DESC, code")
    await _sweep_zips(state, rows)
    return {"drops": [_drop_out(r, state) for r in rows]}


@router.post("/drops", status_code=status.HTTP_201_CREATED)
async def create_drop(body: DropCreate, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    provider = spec_of_kind(state, body.provider, ProviderKind.STORAGE).name
    key = normalise_key(body.key)
    adapter = await state.router.adapter(provider)
    if not adapter.supports(Capability.PRESIGN):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"storage '{provider}' cannot presign downloads")
    size: int | None = None
    try:
        obj = await state.router.storage_stat(key, provider)
    except ProviderError as exc:
        log.debug("stat for drop %s/%s failed: %s", provider, key, exc)
        obj = None
    else:
        if obj is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"object '{key}' not found on '{provider}'")
    if obj is not None:
        size = obj.size
    now = datetime.now(timezone.utc)
    row = {
        "code": await _unique_code(db, "app_drops", DROP_CODE_LEN),
        "provider": provider,
        "key": key,
        "filename": key.rsplit("/", 1)[-1],
        "size": size,
        "created_at": iso(now),
        "expires_at": iso(now + timedelta(hours=body.expires_in_hours)),
        "max_downloads": body.max_downloads,
        "downloads": 0,
        "password_hash": hash_password(body.password) if body.password else None,
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_drops ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _drop_out(row, state)


class FolderDropCreate(BaseModel):
    prefix: str = Field(min_length=1, max_length=1024)
    provider: str | None = None  # None: the folder across every storage backend
    expires_in_hours: int = Field(24, ge=1, le=24 * 365)
    max_downloads: int | None = Field(None, ge=1, le=1_000_000)
    password: str | None = Field(None, max_length=200)


async def _folder_files(
    state: HubState, provider: str, prefix: str, keep: set[tuple[str, str]] | None = None
) -> tuple[list[StoredObject], int, dict[str, str]]:
    """Files under ``prefix`` (one entry per key, first backend in chain order wins): (files, hidden, errors).

    ``keep``: only these ``(backend, key)`` pairs (a folder drop's snapshot), whatever else is there now.
    """
    providers = None if provider == ALL_BACKENDS else [provider]
    raw = await state.router.storage_list(prefix, providers=providers, limit=FOLDER_MAX_FILES * 2)
    seen: dict[str, StoredObject] = {}
    errors: dict[str, str] = {}
    for name, result in raw.items():
        if isinstance(result, str):
            errors[name] = result
            continue
        for obj in result:
            if not obj.key.startswith(prefix) or obj.key.endswith("/") or obj.key.startswith(ZIP_PREFIX):
                continue
            obj.backend = obj.backend or name
            if keep is not None and (obj.backend, obj.key) not in keep:
                continue
            seen.setdefault(obj.key, obj)
    files = sorted(seen.values(), key=lambda o: o.key)
    return files[:FOLDER_MAX_FILES], max(len(files) - FOLDER_MAX_FILES, 0), errors


@router.post("/drops/folder", status_code=status.HTTP_201_CREATED)
async def create_folder_drop(
    body: FolderDropCreate, state: HubState = Depends(get_state), db: AppDB = Depends(_db)
) -> dict[str, Any]:
    """A public page at /d/{code} listing the files under ``prefix`` now (snapshot); password and expiry as file drops."""
    prefix = normalise_key(body.prefix) + "/"
    provider = ALL_BACKENDS
    if body.provider:
        provider = spec_of_kind(state, body.provider, ProviderKind.STORAGE).name
        adapter = await state.router.adapter(provider)
        if not adapter.supports(Capability.PRESIGN):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"storage '{provider}' cannot presign downloads")
    files, hidden, errors = await _folder_files(state, provider, prefix)
    if not files:
        if errors:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"cannot list the folder: {'; '.join(errors.values())}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no files under '{prefix}'")
    now = datetime.now(timezone.utc)
    row = {
        "code": await _unique_code(db, "app_drops", DROP_CODE_LEN),
        "provider": provider,
        "key": prefix,
        "filename": prefix.rstrip("/").rsplit("/", 1)[-1],
        "size": sum(int(f.size or 0) for f in files),
        "created_at": iso(now),
        "expires_at": iso(now + timedelta(hours=body.expires_in_hours)),
        "max_downloads": body.max_downloads,
        "downloads": 0,
        "password_hash": hash_password(body.password) if body.password else None,
        "kind": "folder",
        "file_count": len(files) + hidden,
        "file_keys": json.dumps([[f.backend, f.key] for f in files], separators=(",", ":")),
    }
    cols = list(row)
    await db.execute(
        f"INSERT INTO app_drops ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols]
    )
    return _drop_out(row, state)


@router.delete("/drops/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_drop(code: str, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> Response:
    row = await db.fetch_one("SELECT code, kind FROM app_drops WHERE code = ?", [code])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="drop not found")
    await db.execute("DELETE FROM app_drops WHERE code = ?", [code])
    if row.get("kind") == "folder":  # the ZIP kept for a big folder, if one was built
        await _remove_zips(state, lambda obj: _zip_code(obj.key) == code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _zip_code(key: str) -> str:
    """The drop code of a stored ZIP key (``_shares/zips/<code>-<fingerprint>.zip`` or ``<code>.zip``)."""
    return key[len(ZIP_PREFIX):].removesuffix(".zip").split("-", 1)[0]


async def _remove_zips(state: HubState, doomed: Callable[[StoredObject], bool]) -> None:
    """Delete the stored folder ZIPs ``doomed`` picks, on every backend (best effort)."""
    try:
        raw = await state.router.storage_list(ZIP_PREFIX)
    except ProviderError as exc:
        log.info("could not list the stored ZIPs: %s", exc)
        return
    for name, result in raw.items():
        if isinstance(result, str):
            continue
        for obj in result:
            if obj.key.startswith(ZIP_PREFIX) and doomed(obj):
                try:
                    await state.router.storage_delete(obj.key, obj.backend or name)
                except ProviderError as exc:
                    log.info("could not remove the stored ZIP %s: %s", obj.key, exc)


async def _sweep_zips(state: HubState, rows: list[dict[str, Any]]) -> None:
    """Stored ZIPs only live as long as their drop: those of expired, used-up or deleted drops go."""
    live = {
        r["code"] for r in rows
        if r.get("kind") == "folder" and not _expired(r.get("expires_at"))
        and (r.get("max_downloads") is None or int(r.get("downloads") or 0) < int(r["max_downloads"]))
    }
    await _remove_zips(state, lambda obj: _zip_code(obj.key) not in live)


# --------------------------------------------------------------------------- public pages

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}

_STYLE = (
    "*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:#0f1115;color:#e6e8ee;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}"
    "main{width:100%;max-width:380px;background:#171a21;border:1px solid #262b36;border-radius:12px;padding:24px}"
    "h1{font-size:1.15rem;margin:0 0 8px}p{margin:0 0 16px;color:#a3aab8;word-break:break-word}"
    "input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #333a48;background:#0f1115;color:#e6e8ee;"
    "font:inherit;margin-bottom:12px}button{width:100%;padding:10px;border:0;border-radius:8px;background:#4f7cff;"
    "color:#fff;font:inherit;font-weight:600;cursor:pointer}.err{color:#ff7b7b}"
    "main.wide{max-width:560px}ul{list-style:none;margin:0 0 16px;padding:0;border-top:1px solid #262b36}"
    "li{display:flex;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid #262b36}"
    "li span{flex:1;min-width:0;word-break:break-word}li small{color:#a3aab8;white-space:nowrap}"
    "a{color:#8fb0ff}a.btn{display:block;text-align:center;padding:10px;border-radius:8px;background:#4f7cff;"
    "color:#fff;font-weight:600;text-decoration:none;margin-bottom:16px}.muted{color:#a3aab8;font-size:.9rem}"
)


def _page(title: str, body_html: str, status_code: int, *, wide: bool = False) -> HTMLResponse:
    main = '<main class="wide">' if wide else "<main>"
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title>'
        f"<style>{_STYLE}</style></head><body>{main}{body_html}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status_code, headers=_PAGE_HEADERS)


def _message(title: str, text: str, status_code: int) -> HTMLResponse:
    return _page(title, f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>", status_code)


def _not_found() -> HTMLResponse:
    return _message("Not found", "This link does not exist or has expired.", status.HTTP_404_NOT_FOUND)


def _gone(text: str) -> HTMLResponse:
    return _message("No longer available", text, status.HTTP_410_GONE)


def _password_form(filename: str, error: str | None, status_code: int, *, folder: bool = False) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    what, action = ("Protected folder", "Open") if folder else ("Protected file", "Download")
    body = (
        f"<h1>{what}</h1><p>{html.escape(filename)}</p>{err}"
        '<form method="post" autocomplete="off">'
        '<input type="password" name="password" placeholder="Password" required autofocus aria-label="Password">'
        f"<button type=\"submit\">{action}</button></form>"
    )
    return _page(what, body, status_code)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.get("/s/{code}", include_in_schema=False)
async def follow_link(code: str, db: AppDB = Depends(_db)) -> Response:
    row = await db.fetch_one("SELECT * FROM app_links WHERE code = ?", [code])
    if row is None or _expired(row.get("expires_at")):
        return _not_found()
    await db.execute("UPDATE app_links SET clicks = clicks + 1 WHERE code = ?", [code])
    return _redirect(row["url"])


async def _drop_gate(db: AppDB, code: str, *, check_limit: bool = True) -> tuple[dict[str, Any] | None, Response | None]:
    row = await db.fetch_one("SELECT * FROM app_drops WHERE code = ?", [code])
    if row is None:
        return None, _not_found()
    what = "folder" if row.get("kind") == "folder" else "file drop"
    if _expired(row.get("expires_at")):
        return None, _gone(f"This {what} has expired.")
    max_dl = row.get("max_downloads")
    if check_limit and max_dl is not None and int(row.get("downloads") or 0) >= int(max_dl):
        return None, _gone(f"This {what} reached its download limit.")
    return row, None


async def _claim(db: AppDB, code: str) -> bool:
    return bool(
        await db.execute(
            "UPDATE app_drops SET downloads = downloads + 1 "
            "WHERE code = ? AND (max_downloads IS NULL OR downloads < max_downloads)",
            [code],
        )
    )


async def _release(state: HubState, db: AppDB, row: dict[str, Any]) -> Response:
    try:
        url = await state.router.storage_presign(row["key"], row["provider"], PRESIGN_TTL_S, "GET")
    except ProviderError as exc:
        log.warning("presign failed for drop %s: %s", row["code"], exc)
        return _message("Temporarily unavailable", "The file could not be prepared, try again later.", status.HTTP_502_BAD_GATEWAY)
    if not await _claim(db, row["code"]):
        return _gone("This file drop reached its download limit.")
    return _redirect(url)


# --------------------------------------------------------------------------- public folder pages


def zip_sig(hub_token: str, code: str, exp: int) -> str:
    """Signature of a folder's "Download all" link: HMAC-SHA256 under a key derived from the hub token."""
    key = hmac.new(hub_token.encode("utf-8"), b"omnifree-folder-zip-v1", hashlib.sha256).digest()
    return hmac.new(key, f"zip:{code}:{exp}".encode(), hashlib.sha256).hexdigest()


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


async def _presign_all(state: HubState, files: list[StoredObject]) -> list[str | None]:
    """A 10-minute GET link per file (None when its backend cannot presign or fails)."""
    can: dict[str, bool] = {}
    for backend in {f.backend for f in files}:
        try:
            can[backend] = (await state.router.adapter(backend)).supports(Capability.PRESIGN)
        except (ProviderError, KeyError) as exc:
            log.debug("no adapter for %s: %s", backend, exc)
            can[backend] = False

    async def one(f: StoredObject) -> str | None:
        if not can.get(f.backend):
            return None
        try:
            return await state.router.storage_presign(f.key, f.backend, PRESIGN_TTL_S, "GET")
        except ProviderError as exc:
            log.warning("presign failed for %s/%s: %s", f.backend, f.key, exc)
            return None

    return list(await asyncio.gather(*(one(f) for f in files)))


async def _shared_files(state: HubState, row: dict[str, Any]) -> tuple[list[StoredObject], int]:
    """The files a folder drop shares: its creation snapshot, minus files gone since; (files, not listed)."""
    try:
        keep = {(str(b), str(k)) for b, k in json.loads(row.get("file_keys") or "[]")}
    except (ValueError, TypeError):
        keep = set()
    files, _hidden, _errors = await _folder_files(state, row["provider"], row["key"], keep)
    return files, max(int(row.get("file_count") or 0) - len(keep), 0) if keep else 0


async def _folder_page(state: HubState, db: AppDB, row: dict[str, Any]) -> Response:
    prefix = row["key"]
    try:
        files, hidden = await _shared_files(state, row)
        links = await _presign_all(state, files)
    except ProviderError as exc:
        log.warning("listing failed for folder drop %s: %s", row["code"], exc)
        return _message("Temporarily unavailable", "The folder could not be listed, try again later.", status.HTTP_502_BAD_GATEWAY)
    if not await _claim(db, row["code"]):
        return _gone("This folder reached its download limit.")
    total = sum(int(f.size or 0) for f in files)
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).astimezone(local_tz())
    count = len(files) + hidden
    parts = [
        f"<h1>{html.escape(row['filename'])}</h1>",
        f'<p class="muted">{count} file{"s" if count != 1 else ""} · {_human(total)} · '
        f"link expires {expires:%d %b %Y %H:%M}</p>",
    ]
    token = state.hub_token()
    if files and not hidden and token and total <= ZIP_MAX_BYTES and len(files) <= ZIP_MAX_FILES:
        exp = int(time.time()) + ZIP_TOKEN_TTL_S
        href = f"/d/{row['code']}/zip?t={exp}.{zip_sig(token, row['code'], exp)}"
        parts.append(f'<a class="btn" href="{html.escape(href)}" rel="noreferrer">Download all as ZIP</a>')
    items = []
    for f, url in zip(files, links, strict=True):
        name = html.escape(f.key[len(prefix):])
        label = f'<a href="{html.escape(url)}" rel="noreferrer" download>{name}</a>' if url else name
        items.append(f"<li><span>{label}</span><small>{_human(int(f.size or 0))}</small></li>")
    parts.append(f"<ul>{''.join(items)}</ul>" if items else "<p>The shared files are no longer in this folder.</p>")
    if hidden:
        parts.append(f'<p class="muted">and {hidden} more file{"s" if hidden != 1 else ""} not listed.</p>')
    parts.append('<p class="muted">Download links stay valid for 10 minutes; reload the page for new ones.</p>')
    return _page(row["filename"], "".join(parts), status.HTTP_200_OK, wide=True)


async def _build_zip(state: HubState, files: list[StoredObject], prefix: str, deadline: float) -> bytes | None:
    """The folder as a ZIP (paths relative to the folder), or None past ``deadline`` (monotonic)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for f in files:
            if time.monotonic() > deadline:
                return None
            data = await state.router.storage_get(f.key, f.backend)
            zf.writestr(f.key[len(prefix):], data)
    return buf.getvalue()


def _zip_key(code: str, files: list[StoredObject]) -> str:
    """Where a folder's big ZIP is kept: named after the drop and a fingerprint of the shared files,
    so it is built once and reused (until a shared file changes or goes)."""
    h = hashlib.sha256()
    for f in files:
        h.update(f"{f.backend}|{f.key}|{f.size}|{f.etag or ''}|{f.last_modified or ''};".encode())
    return f"{ZIP_PREFIX}{code}-{h.hexdigest()[:16]}.zip"


async def _stored_zip(state: HubState, row: dict[str, Any], key: str) -> str | None:
    """A presigned link to the ZIP built earlier for the same files, if it is still there."""
    candidates = [row["provider"]] if row["provider"] != ALL_BACKENDS else state.registry.chain(ProviderKind.STORAGE)
    for name in candidates:
        try:
            if not (await state.router.adapter(name)).supports(Capability.PRESIGN):
                continue
            if await state.router.storage_stat(key, name) is not None:
                return await state.router.storage_presign(key, name, PRESIGN_TTL_S, "GET")
        except ProviderError as exc:
            log.debug("no stored ZIP %s on %s: %s", key, name, exc)
    return None


async def _store_zip(state: HubState, row: dict[str, Any], data: bytes, key: str) -> str:
    """Keep a big ZIP on a presign-capable backend (the folder's own when pinned) and presign it."""
    candidates = [row["provider"]] if row["provider"] != ALL_BACKENDS else state.registry.chain(ProviderKind.STORAGE)
    last: ProviderError | None = None
    for name in candidates:
        try:
            if not (await state.router.adapter(name)).supports(Capability.PRESIGN):
                continue
            obj = await state.router.storage_put(key, data, "application/zip", None, provider=name, allow_fallback=False)
            url = await state.router.storage_presign(key, obj.backend or name, PRESIGN_TTL_S, "GET")
            # Older ZIPs of this drop (built before a shared file changed) are dead weight now.
            await _remove_zips(state, lambda o: _zip_code(o.key) == row["code"] and o.key != key)
            return url
        except ProviderError as exc:
            last = exc
    raise last or ProviderError("no storage backend can presign the ZIP", provider="drops", status_code=503)


@public_router.get("/d/{code}/zip", include_in_schema=False)
async def folder_zip(
    code: str,
    t: str = Query("", max_length=128),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> Response:
    """The "Download all as ZIP" link of a folder page. PUBLIC, authorised by the signed ``t`` only."""
    token = state.hub_token()
    exp_s, _, sig = t.partition(".")
    if not token or not exp_s.isdigit() or not hmac.compare_digest(zip_sig(token, code, int(exp_s)).encode(), sig.encode()):
        return _not_found()
    if int(exp_s) < time.time():
        return _gone("This download link has expired. Open the folder page again.")
    row, refusal = await _drop_gate(db, code, check_limit=False)  # opening the page already counted
    if refusal is not None or row is None:
        return refusal or _not_found()
    if row.get("kind") != "folder":
        return _not_found()
    started = time.monotonic()
    try:
        files, hidden = await _shared_files(state, row)
        total = sum(int(f.size or 0) for f in files)
        if not files or hidden or total > ZIP_MAX_BYTES or len(files) > ZIP_MAX_FILES:
            return _message("Too large to zip", "Download the files one by one from the folder page.", 413)
        key = _zip_key(code, files)
        if total > ZIP_INLINE_MAX and (url := await _stored_zip(state, row, key)):
            return _redirect(url)  # built already: no rebuild, no re-upload
        data = await _build_zip(state, files, row["key"], started + ZIP_BUDGET_S)
        if data is None:
            return _message("Too slow to zip", "Download the files one by one from the folder page.", status.HTTP_503_SERVICE_UNAVAILABLE)
        if len(data) > ZIP_INLINE_MAX:
            return _redirect(await _store_zip(state, row, data, key))
    except ProviderError as exc:
        log.warning("zip failed for folder drop %s: %s", code, exc)
        return _message("Temporarily unavailable", "The ZIP could not be prepared, try again later.", status.HTTP_502_BAD_GATEWAY)
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['filename'] + '.zip')}",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.get("/d/{code}", include_in_schema=False)
async def open_drop(code: str, state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> Response:
    row, refusal = await _drop_gate(db, code)
    if refusal is not None or row is None:
        return refusal or _not_found()
    folder = row.get("kind") == "folder"
    if row.get("password_hash"):
        return _password_form(row["filename"], None, status.HTTP_200_OK, folder=folder)
    return await (_folder_page(state, db, row) if folder else _release(state, db, row))


@public_router.post("/d/{code}", include_in_schema=False)
async def unlock_drop(
    code: str,
    password: str = Form(""),
    state: HubState = Depends(get_state),
    db: AppDB = Depends(_db),
) -> Response:
    row, refusal = await _drop_gate(db, code)
    if refusal is not None or row is None:
        return refusal or _not_found()
    folder = row.get("kind") == "folder"
    if row.get("password_hash") and not verify_password(password, row["password_hash"]):
        return _password_form(row["filename"], "Wrong password.", status.HTTP_401_UNAUTHORIZED, folder=folder)
    return await (_folder_page(state, db, row) if folder else _release(state, db, row))

