"""Plan -> act loop: the LLM picks one tool action per step from the current page observation."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from core.events import EventType
from core.interfaces import ProviderError, utcnow
from instinct.browser import BrowserSession, element_selector
from instinct.config import InstinctSettings
from instinct.llm import AgentLLM
from instinct.models import Action, Observation, StepRecord, TaskState

__all__ = ["InstinctAgent", "parse_action", "SYSTEM_PROMPT"]

log = logging.getLogger("omnifree.instinct.agent")

Emit = Callable[..., Awaitable[None]]
OnScreenshot = Callable[[str, int, bytes], None]

SYSTEM_PROMPT = """You are Instinct, an autonomous web-browsing agent controlling a headless browser.
Each turn you receive the user's goal, a summary of your previous steps and the current page
(URL, title, numbered interactive elements, visible text). Reply with EXACTLY ONE JSON object and nothing else:

{"thought": "<short plan for this step>", "action": "<tool>", ...tool arguments}

Tools:
- {"action": "goto", "url": "https://..."}                      open a URL
- {"action": "click", "element": 12}                             click element [12] (or "selector": "text=Sign in")
- {"action": "type", "element": 3, "text": "...", "submit": true} fill an input; submit presses Enter
- {"action": "extract", "element": 7}                            read the text of an element (omit element for the whole page)
- {"action": "screenshot"}                                       capture the viewport for the user
- {"action": "done", "result": "<final answer for the user>"}    finish when the goal is achieved or impossible

Rules: prefer element ids from the list; never invent credentials; never make purchases or send
messages unless the goal explicitly asks for it; if a page blocks you, try another approach or finish
with "done" explaining why."""


def parse_action(text: str) -> Action:
    """Extract and validate the first JSON object in an LLM reply (code fences and prose tolerated)."""
    decoder = json.JSONDecoder()
    last_error: Exception | None = None
    start = text.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text, start)
        except ValueError as exc:
            last_error = exc
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            try:
                return Action.model_validate(obj)
            except ValidationError as exc:
                last_error = exc
        start = text.find("{", start + 1)
    if last_error is not None:
        raise ValueError(f"invalid action: {last_error}")
    raise ValueError("no JSON object found in the reply")


def _format_observation(obs: Observation) -> str:
    lines = [f"URL: {obs.url or '(blank)'}", f"Title: {obs.title or '(none)'}", "Interactive elements:"]
    if obs.elements:
        for el in obs.elements:
            extra = f" type={el.type}" if el.type else ""
            href = f" href={el.href[:80]}" if el.href else ""
            lines.append(f"[{el.id}] <{el.tag}{extra}> {el.text!r}{href}")
    else:
        lines.append("(none)")
    lines.append(f"Visible text: {obs.text or '(empty)'}")
    return "\n".join(lines)


class InstinctAgent:
    def __init__(
        self,
        llm: AgentLLM,
        settings: InstinctSettings,
        emit: Emit,
        on_screenshot: OnScreenshot | None = None,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.emit = emit
        self.on_screenshot = on_screenshot

    def _messages(self, task: TaskState, obs: Observation, step: int) -> list[dict[str, str]]:
        history = task.steps[-self.settings.history_steps :]
        if history:
            past = "\n".join(
                f"step {s.step}: {json.dumps(s.action, ensure_ascii=False) if s.action else '(no action)'} -> "
                f"{(s.error and 'ERROR ' + s.error) or s.result}"[:400]
                for s in history
            )
        else:
            past = "(no steps yet)"
        user = (
            f"Goal: {task.goal}\n\nPrevious steps:\n{past}\n\n"
            f"Step {step} of {task.max_steps}. Current page:\n{_format_observation(obs)}\n\n"
            "Reply with one JSON action."
        )
        return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    async def _screenshot(self, task: TaskState, session: BrowserSession, step: int, url: str) -> None:
        try:
            image = await session.screenshot()
        except Exception as exc:
            log.warning("screenshot failed for %s step %s: %r", task.id, step, exc)
            return
        task.screenshot_step = step
        if self.on_screenshot is not None:
            self.on_screenshot(task.id, step, image)
        await self.emit(
            EventType.INSTINCT_TASK_SCREENSHOT,
            task_id=task.id, step=step, url=url,
            image_b64=base64.b64encode(image).decode("ascii"), mime="image/jpeg",
        )

    async def _act(self, action: Action, session: BrowserSession) -> str:
        if action.action == "goto":
            return await session.goto(action.url or "")
        if action.action == "click":
            return await session.click(element_selector(action.element, action.selector))
        if action.action == "type":
            return await session.type(element_selector(action.element, action.selector), action.text or "", action.submit)
        if action.action == "extract":
            target = None if action.element is None and not action.selector else element_selector(action.element, action.selector)
            return await session.extract(target, self.settings.extract_max_chars)
        if action.action == "screenshot":
            return "screenshot captured"
        return action.result or ""

    async def run(self, task: TaskState, session: BrowserSession) -> TaskState:
        """Run until ``done``, an unrecoverable error, or the step limit. Mutates and returns ``task``."""
        task.status = "running"
        task.started_at = utcnow()
        await self.emit(EventType.INSTINCT_TASK_STEP, task_id=task.id, step=0, status="running", result="started")
        if task.start_url:
            try:
                result = await session.goto(task.start_url)
                task.steps.append(StepRecord(step=0, action={"action": "goto", "url": task.start_url}, result=result))
            except Exception as exc:
                task.steps.append(StepRecord(step=0, action={"action": "goto", "url": task.start_url}, error=str(exc)[:500]))
            await self._screenshot(task, session, 0, task.start_url)

        for step in range(1, task.max_steps + 1):
            obs = await session.observe()
            try:
                reply = await self.llm.complete(self._messages(task, obs, step), model=task.model)
            except ProviderError as exc:
                return await self._finish(task, error=f"LLM unavailable: {exc}")
            record = StepRecord(step=step, url=obs.url, routed_via=reply.routed_via)
            try:
                action = parse_action(reply.content)
            except ValueError as exc:
                record.error = f"{exc}"[:500]
                record.result = "reply was not a valid action"
                task.steps.append(record)
                await self.emit(
                    EventType.INSTINCT_TASK_STEP, task_id=task.id, step=step, status="running",
                    error=record.error, routed_via=reply.routed_via,
                )
                continue
            record.action = action.model_dump(exclude_none=True, exclude_defaults=True) | {"action": action.action}
            if action.action == "done":
                record.result = action.result or ""
                task.steps.append(record)
                await self.emit(
                    EventType.INSTINCT_TASK_STEP, task_id=task.id, step=step, status="running",
                    action=record.action, thought=action.thought, result=record.result, routed_via=reply.routed_via,
                )
                await self._screenshot(task, session, step, obs.url)
                return await self._finish(task, result=action.result or "")
            try:
                record.result = (await self._act(action, session))[: self.settings.extract_max_chars]
            except Exception as exc:  # browser errors are fed back to the LLM, not fatal
                record.error = f"{type(exc).__name__}: {exc}"[:500]
            task.steps.append(record)
            current_url = await session.current_url()
            await self.emit(
                EventType.INSTINCT_TASK_STEP, task_id=task.id, step=step, status="running",
                action=record.action, thought=action.thought, result=record.result, error=record.error,
                url=current_url, routed_via=reply.routed_via,
            )
            await self._screenshot(task, session, step, current_url)
        return await self._finish(task, error=f"step limit reached ({task.max_steps})")

    async def _finish(self, task: TaskState, *, result: str | None = None, error: str | None = None) -> TaskState:
        task.finished_at = utcnow()
        if error is None:
            task.status = "completed"
            task.result = result
            await self.emit(
                EventType.INSTINCT_TASK_COMPLETED, task_id=task.id, status="completed",
                result=result, steps=len(task.steps),
            )
        else:
            task.status = "failed"
            task.error = error
            await self.emit(
                EventType.INSTINCT_TASK_FAILED, task_id=task.id, status="failed", error=error, steps=len(task.steps)
            )
        return task
