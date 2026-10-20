from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx


class Recorder:
    """MockTransport handler wrapper that records requests."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def token_response(request: httpx.Request) -> httpx.Response | None:
    """Answer Google's OAuth token endpoint; None for any other URL."""
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600, "token_type": "Bearer"})
    return None


def jresp(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json",
                                                                              **(headers or {})})
