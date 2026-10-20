"""In-memory fake of the Upstash Redis REST API (``POST /`` and ``POST /pipeline``) as an httpx transport.

Implements just the commands core.shared_state users send. TTLs are recorded, not enforced.
Set ``fail = True`` to make every request answer HTTP 500.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

URL = "https://fake-upstash.test"
TOKEN = "fake-token"


class FakeUpstash:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.fail = False

    # ------------------------------------------------------------------ transport
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(500, json={"error": "boom"})
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "Unauthorized"})
        body = json.loads(request.content)
        if request.url.path == "/pipeline":
            return httpx.Response(200, json=[self._run(cmd) for cmd in body])
        return httpx.Response(200, json=self._run(body))

    def _run(self, cmd: list[str]) -> dict[str, Any]:
        name, args = cmd[0].upper(), cmd[1:]
        handler = getattr(self, f"_cmd_{name.lower()}", None)
        if handler is None:
            return {"error": f"ERR unknown command '{name}'"}
        return {"result": handler(*args)}

    # ------------------------------------------------------------------ commands
    def _cmd_get(self, key: str) -> Any:
        return self.data.get(key)

    def _cmd_set(self, key: str, value: str) -> str:
        self.data[key] = value
        return "OK"

    def _cmd_mget(self, *keys: str) -> list[Any]:
        return [self.data.get(k) for k in keys]

    def _cmd_del(self, *keys: str) -> int:
        return sum(self.data.pop(k, None) is not None for k in keys)

    def _cmd_incrbyfloat(self, key: str, amount: str) -> str:
        value = float(self.data.get(key) or 0) + float(amount)
        self.data[key] = repr(value)
        return self.data[key]

    def _cmd_expire(self, key: str, seconds: str) -> int:
        self.ttl[key] = int(seconds)
        return int(key in self.data)

    def _cmd_sadd(self, key: str, *members: str) -> int:
        current: set[str] = self.data.setdefault(key, set())
        before = len(current)
        current.update(members)
        return len(current) - before

    def _cmd_srem(self, key: str, *members: str) -> int:
        current: set[str] = self.data.get(key, set())
        before = len(current)
        current.difference_update(members)
        return before - len(current)

    def _cmd_smembers(self, key: str) -> list[str]:
        return sorted(self.data.get(key, set()))

    def _cmd_lpush(self, key: str, *values: str) -> int:
        current: list[str] = self.data.setdefault(key, [])
        for value in values:
            current.insert(0, value)
        return len(current)

    def _cmd_ltrim(self, key: str, start: str, stop: str) -> str:
        current: list[str] = self.data.get(key, [])
        self.data[key] = current[int(start) : int(stop) + 1]
        return "OK"

    def _cmd_lrange(self, key: str, start: str, stop: str) -> list[str]:
        current: list[str] = self.data.get(key, [])
        end = None if int(stop) == -1 else int(stop) + 1
        return current[int(start) : end]
