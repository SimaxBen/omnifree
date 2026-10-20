"""Bearer-token authentication for HTTP routes and the WebSocket.

The token is resolved through the core config layer (``Settings.hub_token()``:
``OMNIFREE_HUB_API_TOKEN`` env or vault key ``HUB_API_TOKEN``; fallback key
``OMNIFREE_HUB_TOKEN``). The hub fails closed: when no token is configured,
every protected route answers 503.

Browsers cannot set headers on ``<a href>`` downloads or WebSocket upgrades,
so the token is also accepted as the ``access_token`` query parameter and, for
WebSockets, as the subprotocol pair ``["bearer", "<token>"]``.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, WebSocket, status

from hub_api.state import HubState, get_state

__all__ = ["require_token", "websocket_token_ok", "token_matches"]


def token_matches(expected: str | None, presented: str | None) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def require_token(request: Request, state: HubState = Depends(get_state)) -> None:
    expected = state.hub_token()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="hub token not configured (set OMNIFREE_HUB_API_TOKEN or vault key HUB_API_TOKEN)",
        )
    presented = _bearer(request.headers.get("authorization")) or request.query_params.get("access_token")
    if not token_matches(expected, presented):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def websocket_token_ok(websocket: WebSocket, expected: str | None) -> tuple[bool, str | None]:
    """Return (authorised, subprotocol to echo back)."""
    presented = _bearer(websocket.headers.get("authorization")) or websocket.query_params.get(
        "access_token"
    ) or websocket.query_params.get("token")
    subprotocol: str | None = None
    if not presented:
        offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        if len(offered) >= 2 and offered[0].lower() == "bearer":
            presented = offered[1]
            subprotocol = offered[0]
    return token_matches(expected, presented), subprotocol
