from __future__ import annotations

from typing import Any

from core.interfaces import ChatMessage, ChatRequest, PluginContext, ProviderKind


def make(cls: type, name: str, settings: dict[str, Any], secrets: dict[str, str] | None = None, **ctx: Any) -> Any:
    return cls(
        PluginContext(
            name=name,
            kind=ProviderKind.LLM,
            settings=settings,
            secrets={"api_key": "sk-test"} if secrets is None else secrets,
            **ctx,
        )
    )


def req(model: str | None = None, **kw: Any) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role="user", content="hi")], model=model, **kw)


def sse(*events: Any, done: bool = True) -> bytes:
    import json

    lines = [f"data: {json.dumps(e)}\n\n" for e in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def completion(content: str = "hello", model: str = "m", usage: tuple[int, int] = (3, 4)) -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


