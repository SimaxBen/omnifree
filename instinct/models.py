"""Task, step and action models for the Instinct agent."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.interfaces import utcnow

__all__ = ["ActionName", "Action", "TaskCreate", "StepRecord", "TaskState", "Observation", "PageElement"]

ActionName = Literal["goto", "click", "type", "extract", "screenshot", "done"]
TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class Action(BaseModel):
    """One tool call chosen by the LLM."""

    action: ActionName
    thought: str | None = None
    url: str | None = None  # goto
    element: int | None = None  # click/type/extract: id from the observation list
    selector: str | None = None  # click/type/extract: CSS / Playwright selector (text=..., role=...)
    text: str | None = None  # type: text to enter
    submit: bool = False  # type: press Enter afterwards
    result: str | None = None  # done: final answer

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data: Any) -> Any:
        # Accept {"action": "click", "args": {...}} / {"tool": ..., "parameters": {...}} shapes.
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "action" not in data:
            for alias in ("tool", "name", "type"):
                if isinstance(data.get(alias), str):
                    data["action"] = data.pop(alias)
                    break
        if isinstance(data.get("action"), str):
            data["action"] = data["action"].strip().lower()
        for nested in ("args", "arguments", "parameters", "input"):
            inner = data.pop(nested, None)
            if isinstance(inner, dict):
                for k, v in inner.items():
                    data.setdefault(k, v)
        if isinstance(data.get("element"), str) and data["element"].strip().lstrip("#").isdigit():
            data["element"] = int(data["element"].strip().lstrip("#"))
        if data.get("action") == "done" and data.get("result") is None and data.get("text") is not None:
            data["result"] = data["text"]
        return data

    @model_validator(mode="after")
    def _check(self) -> Action:
        if self.action == "goto" and not self.url:
            raise ValueError("goto needs 'url'")
        if self.action in ("click", "type") and self.element is None and not self.selector:
            raise ValueError(f"{self.action} needs 'element' or 'selector'")
        if self.action == "type" and self.text is None:
            raise ValueError("type needs 'text'")
        return self


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    start_url: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    model: str | None = None


class StepRecord(BaseModel):
    step: int
    action: dict[str, Any] | None = None
    result: str = ""
    error: str | None = None
    url: str | None = None
    routed_via: str | None = None
    at: datetime = Field(default_factory=utcnow)


class TaskState(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str
    start_url: str | None = None
    model: str | None = None
    max_steps: int
    status: TaskStatus = "queued"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    screenshot_step: int | None = None

    def summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"steps"}) | {"step_count": len(self.steps)}


class PageElement(BaseModel):
    id: int
    tag: str
    text: str = ""
    type: str | None = None
    href: str | None = None


class Observation(BaseModel):
    url: str = ""
    title: str = ""
    elements: list[PageElement] = Field(default_factory=list)
    text: str = ""
