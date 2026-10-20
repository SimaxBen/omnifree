"""Process-wide hub state: settings, event bus, registry, router, Instinct client, agent cache."""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request, WebSocket

from core.config import Settings
from core.events import EventBus
from core.registry import PluginRegistry
from core.router import SmartRouter
from hub_api.settings import HUB_TOKEN_FALLBACK_KEY, INSTINCT_TOKEN_KEY, HubSettings

__all__ = ["HubState", "InstinctClient", "AgentCache", "get_state", "get_ws_state"]


class InstinctClient:
    """Thin async proxy to the Instinct agent service (``instinct.service``)."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self._timeout_s,
                transport=self._transport,
            )
        return self._client

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.client.request(method, path, **kwargs)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


@dataclass
class AgentTaskView:
    task_id: str
    status: str = "created"
    last_event: dict[str, Any] = field(default_factory=dict)
    screenshot_b64: str | None = None
    screenshot_step: int | None = None


class AgentCache:
    """Latest known state/screenshot per Instinct task (fed by the event callback)."""

    def __init__(self, max_tasks: int = 100) -> None:
        self.max_tasks = max_tasks
        self._tasks: OrderedDict[str, AgentTaskView] = OrderedDict()

    def update(self, event_type: str, data: dict[str, Any]) -> AgentTaskView | None:
        task_id = str(data.get("task_id") or "")
        if not task_id:
            return None
        view = self._tasks.pop(task_id, None) or AgentTaskView(task_id=task_id)
        suffix = event_type.rsplit(".", 1)[-1]
        if suffix == "screenshot" and data.get("image_b64"):
            view.screenshot_b64 = str(data["image_b64"])
            view.screenshot_step = data.get("step")
        else:
            view.last_event = {"type": event_type, **{k: v for k, v in data.items() if k != "image_b64"}}
            view.status = {
                "created": "queued",
                "step": "running",
                "completed": "completed",
                "failed": "failed",
            }.get(suffix, view.status)
            if data.get("status"):
                view.status = str(data["status"])
        self._tasks[task_id] = view
        while len(self._tasks) > self.max_tasks:
            self._tasks.popitem(last=False)
        return view

    def get(self, task_id: str) -> AgentTaskView | None:
        return self._tasks.get(task_id)

    def screenshot_bytes(self, task_id: str) -> bytes | None:
        view = self._tasks.get(task_id)
        if view is None or not view.screenshot_b64:
            return None
        return base64.b64decode(view.screenshot_b64)

    def all(self) -> list[AgentTaskView]:
        return list(reversed(self._tasks.values()))


@dataclass
class HubState:
    settings: Settings
    hub: HubSettings
    bus: EventBus
    registry: PluginRegistry
    router: SmartRouter
    instinct: InstinctClient
    agent_cache: AgentCache

    def hub_token(self) -> str | None:
        return self.settings.hub_token() or self.settings.secret(HUB_TOKEN_FALLBACK_KEY)

    @classmethod
    def build(
        cls,
        settings: Settings,
        hub: HubSettings,
        *,
        bus: EventBus | None = None,
        registry: PluginRegistry | None = None,
        router: SmartRouter | None = None,
        instinct_transport: httpx.AsyncBaseTransport | None = None,
    ) -> HubState:
        from core.quota import QuotaStore

        bus = bus or (registry.bus if registry is not None else EventBus())
        if registry is None:
            registry = PluginRegistry.from_file(settings.providers_path, secret_lookup=settings.secret, bus=bus)
        if router is None:
            router = SmartRouter(registry, QuotaStore(settings.quota_db_path), bus)
        instinct = InstinctClient(
            hub.instinct_url,
            settings.secret(INSTINCT_TOKEN_KEY),
            timeout_s=hub.instinct_timeout_s,
            transport=instinct_transport,
        )
        return cls(
            settings=settings,
            hub=hub,
            bus=bus,
            registry=registry,
            router=router,
            instinct=instinct,
            agent_cache=AgentCache(hub.agent_task_cache),
        )


def get_state(request: Request) -> HubState:
    return request.app.state.hub


def get_ws_state(websocket: WebSocket) -> HubState:
    return websocket.app.state.hub
