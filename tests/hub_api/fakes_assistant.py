"""Fakes for /assistant: a scripted LLM that advertises function calling (test code only, no network).

``FakeToolLLM`` pops its answers from ``fakes_apps.CHAT_SCRIPT[name]`` like ``FakeChatLLM``; an item
may also be a dict ``{"content": ..., "tool_calls": [(name, args), ...]}`` to answer with tool calls.
Every request is logged to ``fakes_apps.CHAT_LOG``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from core.interfaces import Capability, ChatRequest, ChatResponse, Usage
from tests.hub_api import fakes_apps as fa
from tests.hub_api.fakes import FakeLLM, _fail

A = "tests.hub_api.fakes_assistant"


def calls(*items: tuple[str, dict[str, Any]], content: str | None = None) -> dict[str, Any]:
    """A scripted answer made of tool calls."""
    return {"content": content, "tool_calls": list(items)}


class FakeToolLLM(FakeLLM):
    capabilities: ClassVar[frozenset[str]] = FakeLLM.capabilities | {Capability.TOOLS}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        fa.CHAT_LOG.append((self.name, request))
        _fail(self.name, self.setting("behaviour"))
        queue = fa.CHAT_SCRIPT[self.name]
        answer = queue.pop(0) if queue else "ok"
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            tool_calls = [
                {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                for i, (n, a) in enumerate(answer.get("tool_calls") or [])
            ]
            return ChatResponse(model="tool-model", content=answer.get("content"), tool_calls=tool_calls or None,
                                usage=Usage(total_tokens=9))
        return ChatResponse(model="tool-model", content=answer, usage=Usage(total_tokens=9))


def assistant_plugins(*, tools: bool = True) -> dict[str, Any]:
    """The apps plugin map plus (``tools``) a function-calling LLM first in the chain."""
    plugins = fa.apps_plugins()
    if tools:
        plugins["tooly"] = {"module": f"{A}:FakeToolLLM", "kind": "llm", "group": "llm_pool", "priority": 5,
                            "settings": {"default_model": "t", "models": ["t"]}}
    return plugins
