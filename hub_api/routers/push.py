"""/push: native Web Push subscriptions of the PWA (see :mod:`hub_api.webpush`), next to ntfy (``/notify``).

* ``GET /push/config``: ``{configured, public_key, error}``; the PWA subscribes with ``public_key`` as
  ``applicationServerKey``;
* ``POST /push/subscribe``: the ``PushSubscription`` JSON (``{endpoint, keys: {p256dh, auth}}``) plus an optional
  ``label`` ("S25 Brave") and ``browser`` hint; the server records the user-agent. Idempotent (keyed by the
  SHA-256 of the endpoint), so the PWA calls it on every start;
* ``DELETE /push/subscribe``: ``{endpoint}`` (this device) or ``{id}`` (any device from the list);
* ``GET /push/subscriptions``: label, browser, push service host, created / last success / last failure; never
  the keys or the endpoint;
* ``POST /push/test``: a test push to every device, or to one with ``{endpoint}`` / ``{id}``; per-device results.

Subscriptions answering 404/410 are deleted on the spot (by the test and by every fan-out from ``/notify``).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from hub_api import webpush
from hub_api.appdata import AppDB, get_appdb, now_iso
from hub_api.state import HubState, get_state

__all__ = ["router", "SCHEMA", "ensure_schema", "endpoint_id", "list_subs", "fan_out", "browser_of", "pwa_url"]

router = APIRouter(prefix="/push", tags=["push"])

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_push_subs ("
    " id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL,"
    " label TEXT, browser TEXT, user_agent TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
    " last_success_at TEXT, last_failure_at TEXT, last_error TEXT)",
)
MAX_SUBS = 20


async def ensure_schema(db: AppDB) -> AppDB:
    await db.ensure("push", SCHEMA)
    return db


async def _db(db: AppDB = Depends(get_appdb)) -> AppDB:
    return await ensure_schema(db)


def endpoint_id(endpoint: str) -> str:
    """SHA-256 hex of the endpoint (the PWA computes the same to spot "this device" in the list)."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def browser_of(user_agent: str | None, hint: str | None = None) -> str:
    """A short browser name. Brave hides in a Chrome user-agent, so the PWA's hint (``navigator.brave``) wins."""
    if hint and re.fullmatch(r"[\w .()+-]{1,40}", hint):
        return hint
    ua = user_agent or ""
    os_name = ("Android" if "Android" in ua else "iOS" if re.search(r"iPhone|iPad|iPod", ua)
               else "Windows" if "Windows" in ua else "macOS" if "Mac OS X" in ua else "Linux" if "Linux" in ua else "")
    if "SamsungBrowser" in ua:
        name = "Samsung Internet"
    elif "EdgA" in ua or "Edg/" in ua:
        name = "Edge"
    elif "Firefox" in ua or "FxiOS" in ua:
        name = "Firefox"
    elif "OPR/" in ua:
        name = "Opera"
    elif "Chrome/" in ua or "CriOS" in ua:
        name = "Chrome"
    elif "Safari/" in ua:
        name = "Safari"
    else:
        name = "Browser"
    return f"{name} ({os_name})" if os_name else name


def pwa_url(state: HubState, path: str) -> str:
    base = state.hub.pwa_base_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}" if path else f"{base}/"


def _sub_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "label": row.get("label"), "browser": row.get("browser"),
        "push_service": urlparse(row.get("endpoint") or "").hostname, "created_at": row.get("created_at"),
        "last_success_at": row.get("last_success_at"), "last_failure_at": row.get("last_failure_at"),
        "last_error": row.get("last_error"),
    }


async def list_subs(db: AppDB) -> list[dict[str, Any]]:
    return await db.fetch("SELECT * FROM app_push_subs ORDER BY created_at", [], limit=MAX_SUBS * 2)


async def _send_one(cfg: webpush.WebPushConfig, db: AppDB, row: dict[str, Any], payload: str, *, ttl: int,
                    urgency: str) -> dict[str, Any]:
    sub = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
    res = await webpush.send(cfg, sub, payload, ttl=ttl, urgency=urgency)
    now = now_iso()
    if res.ok:
        await db.execute("UPDATE app_push_subs SET last_success_at = ?, last_error = NULL WHERE id = ?", [now, row["id"]])
    elif res.gone:
        await db.execute("DELETE FROM app_push_subs WHERE id = ?", [row["id"]])
    else:
        await db.execute("UPDATE app_push_subs SET last_failure_at = ?, last_error = ? WHERE id = ?",
                         [now, (res.error or "")[:300], row["id"]])
    return {"id": row["id"], "label": row.get("label"), "browser": row.get("browser"), "ok": res.ok,
            "status": res.status, "error": res.error, "removed": res.gone}


async def fan_out(cfg: webpush.WebPushConfig, db: AppDB, rows: list[dict[str, Any]], payload: str, *,
                  ttl: int = webpush.DEFAULT_TTL_S, urgency: str = "normal") -> list[dict[str, Any]]:
    """Send ``payload`` to every row concurrently; updates last success/failure and deletes 404/410 ones."""
    return list(await asyncio.gather(*(_send_one(cfg, db, r, payload, ttl=ttl, urgency=urgency) for r in rows)))


# --------------------------------------------------------------------------- routes


class SubKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class SubscribeBody(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    expirationTime: float | None = None  # noqa: N815 - the PushSubscription JSON field name
    keys: SubKeys
    label: str | None = Field(None, max_length=60)
    browser: str | None = Field(None, max_length=40)


class TargetBody(BaseModel):
    endpoint: str | None = Field(None, max_length=1000)
    id: str | None = Field(None, max_length=64)


@router.get("/config")
async def push_config(state: HubState = Depends(get_state)) -> dict[str, Any]:
    cfg = webpush.load_config(state.settings.secret)
    return {"configured": cfg.configured, "public_key": cfg.public_key if cfg.configured else None, "error": cfg.error}


@router.post("/subscribe")
async def push_subscribe(body: SubscribeBody, request: Request, state: HubState = Depends(get_state),
                         db: AppDB = Depends(_db)) -> dict[str, Any]:
    if not body.endpoint.startswith("https://"):
        raise HTTPException(422, detail="the endpoint must be an https:// URL")
    sid = endpoint_id(body.endpoint)
    ua = (request.headers.get("user-agent") or "")[:300]
    existing = await db.fetch_one("SELECT id FROM app_push_subs WHERE id = ?", [sid])
    if existing is None:
        count = await db.fetch_one("SELECT COUNT(*) AS n FROM app_push_subs", [])
        if int((count or {}).get("n") or 0) >= MAX_SUBS:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"at most {MAX_SUBS} devices: remove one first")
    now = now_iso()
    label = (body.label or "").strip() or None
    await db.execute(
        "INSERT INTO app_push_subs (id, endpoint, p256dh, auth, label, browser, user_agent, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,"
        " label = COALESCE(excluded.label, app_push_subs.label), browser = excluded.browser,"
        " user_agent = excluded.user_agent, updated_at = excluded.updated_at",
        [sid, body.endpoint, body.keys.p256dh, body.keys.auth, label, browser_of(ua, body.browser), ua, now, now],
    )
    row = await db.fetch_one("SELECT * FROM app_push_subs WHERE id = ?", [sid])
    cfg = webpush.load_config(state.settings.secret)
    return {"subscription": _sub_out(row or {"id": sid}), "created": existing is None, "configured": cfg.configured}


@router.delete("/subscribe")
async def push_unsubscribe(body: TargetBody, db: AppDB = Depends(_db)) -> dict[str, Any]:
    sid = body.id or (endpoint_id(body.endpoint) if body.endpoint else None)
    if not sid:
        raise HTTPException(422, detail="give endpoint or id")
    removed = await db.execute("DELETE FROM app_push_subs WHERE id = ?", [sid])
    return {"removed": bool(removed), "id": sid}


@router.get("/subscriptions")
async def push_subscriptions(state: HubState = Depends(get_state), db: AppDB = Depends(_db)) -> dict[str, Any]:
    cfg = webpush.load_config(state.settings.secret)
    return {"configured": cfg.configured, "subscriptions": [_sub_out(r) for r in await list_subs(db)]}


@router.post("/test")
async def push_test(body: TargetBody | None = None, state: HubState = Depends(get_state),
                    db: AppDB = Depends(_db)) -> dict[str, Any]:
    cfg = webpush.load_config(state.settings.secret)
    if not cfg.configured:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"web push is not set up on the server: {cfg.error or 'missing VAPID keys'}")
    rows = await list_subs(db)
    target = body and (body.id or (endpoint_id(body.endpoint) if body.endpoint else None))
    if target:
        rows = [r for r in rows if r["id"] == target]
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="this device is not subscribed on the hub")
    payload = webpush.build_payload(
        "OmniFree : test réussi ✓", "Les notifications de l'appli arrivent bien sur cet appareil.",
        tag="omnifree-test", url=pwa_url(state, "#/more/notifications"),
    )
    results = await fan_out(cfg, db, rows, payload, urgency="high")
    return {"sent": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"]),
            "removed": sum(1 for r in results if r["removed"]), "results": results}
