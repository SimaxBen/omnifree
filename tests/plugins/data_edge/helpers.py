"""Test helpers for the data_edge plugin tests (httpx.MockTransport routing + fake drivers)."""

from __future__ import annotations

import json
import re
import sys
import types
from collections.abc import Callable
from typing import Any

import httpx

from core.interfaces import PluginContext, ProviderKind, QuotaLimit

Handler = Callable[[httpx.Request], httpx.Response | dict | list | str | int | None]


class Routes:
    """Tiny router: ``routes.add("GET", r"/v1/services$", handler_or_json)``; records requests."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Any]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, pattern: str, reply: Any, status: int = 200) -> Routes:
        self.routes.append((method.upper(), re.compile(pattern), (reply, status)))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        target = str(request.url)
        for method, pattern, (reply, status) in reversed(self.routes):
            if method == request.method and pattern.search(target):
                value = reply(request) if callable(reply) else reply
                if isinstance(value, httpx.Response):
                    return value
                if value is None:
                    return httpx.Response(status)
                if isinstance(value, (bytes, str)):
                    return httpx.Response(status, content=value if isinstance(value, bytes) else value.encode())
                return httpx.Response(status, json=value)
        return httpx.Response(404, json={"error": f"no route for {request.method} {target}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def find(self, method: str, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and fragment in str(r.url)]


def body(request: httpx.Request) -> Any:
    return json.loads(request.content.decode() or "null")


def make(cls: type, kind: ProviderKind, routes: Routes | None = None, *, settings: dict | None = None,
         secrets: dict | None = None, limits: tuple[QuotaLimit, ...] = (), name: str = "test") -> Any:
    ctx = PluginContext(
        name=name,
        kind=kind,
        settings=dict(settings or {}),
        secrets=dict(secrets or {}),
        limits=limits,
        http_transport=routes.transport() if routes else None,
    )
    return cls(ctx)


# --------------------------------------------------------------------------- fake asyncpg


class _Attr:
    def __init__(self, name: str) -> None:
        self.name = name


class FakePgDatabase:
    """Scripted responses: ``responses[sql_substring] = (columns, rows, status)``."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[list[str], list[tuple], str]] = {}
        self.executed: list[tuple[str, tuple, bool]] = []
        self.pools_created = 0
        self.closed = False

    def lookup(self, sql: str) -> tuple[list[str], list[tuple], str]:
        for fragment, response in self.responses.items():
            if fragment in sql:
                return response
        raise RuntimeError(f"unexpected SQL: {sql}")


class _FakeStatement:
    def __init__(self, db: FakePgDatabase, sql: str, readonly: bool) -> None:
        self.db, self.sql, self.readonly = db, sql, readonly
        self.columns, self.rows, self.status = db.lookup(sql)

    def get_attributes(self) -> list[_Attr]:
        return [_Attr(c) for c in self.columns]

    async def fetch(self, *args: Any) -> list[tuple]:
        self.db.executed.append((self.sql, args, self.readonly))
        return list(self.rows)

    def get_statusmsg(self) -> str:
        return self.status

    def cursor(self, *args: Any) -> Any:
        stmt = self

        class _Cursor:
            async def fetch(self, n: int) -> list[tuple]:
                return list(stmt.rows[:n])

        async def _make() -> Any:
            self.db.executed.append((self.sql, args, self.readonly))
            return _Cursor()

        return _make()


class _FakeTransaction:
    def __init__(self, conn: _FakeConn, readonly: bool) -> None:
        self.conn, self.readonly = conn, readonly

    async def __aenter__(self) -> None:
        self.conn.readonly = self.readonly

    async def __aexit__(self, *exc: object) -> None:
        self.conn.readonly = False


class _FakeConn:
    def __init__(self, db: FakePgDatabase) -> None:
        self.db, self.readonly = db, False

    def transaction(self, readonly: bool = False) -> _FakeTransaction:
        return _FakeTransaction(self, readonly)

    async def prepare(self, sql: str) -> _FakeStatement:
        return _FakeStatement(self.db, sql, self.readonly)


class _FakePool:
    def __init__(self, db: FakePgDatabase) -> None:
        self.db = db

    def acquire(self) -> Any:
        conn = _FakeConn(self.db)

        class _Ctx:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()

    async def close(self) -> None:
        self.db.closed = True


def install_fake_asyncpg(monkeypatch: Any) -> FakePgDatabase:
    db = FakePgDatabase()
    module = types.ModuleType("asyncpg")

    async def create_pool(**kwargs: Any) -> _FakePool:
        assert kwargs["statement_cache_size"] == 0
        db.pools_created += 1
        return _FakePool(db)

    module.create_pool = create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    return db


# --------------------------------------------------------------------------- fake motor


class FakeMongo:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.replies: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = []
        self.closed = False
        self.uri: str | None = None

    def on(self, command_name: str, reply: Callable[[dict[str, Any]], dict[str, Any]] | dict[str, Any]) -> None:
        self.replies.append((command_name, reply if callable(reply) else (lambda _c, r=reply: r)))

    async def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(command)
        name = next(iter(command))
        for cmd, reply in reversed(self.replies):
            if cmd == name:
                return reply(command)
        raise RuntimeError(f"unexpected command {name}")


def install_fake_motor(monkeypatch: Any) -> FakeMongo:
    mongo = FakeMongo()
    motor = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class AsyncIOMotorClient:
        def __init__(self, uri: str, **kwargs: Any) -> None:
            mongo.uri = uri

        def __getitem__(self, name: str) -> FakeMongo:
            return mongo

        def close(self) -> None:
            mongo.closed = True

    motor_asyncio.AsyncIOMotorClient = AsyncIOMotorClient  # type: ignore[attr-defined]
    motor.motor_asyncio = motor_asyncio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)
    monkeypatch.setitem(sys.modules, "bson", None)  # force the plain-JSON parser path
    return mongo
