"""Headless browser sessions for the agent (Playwright, imported lazily)."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from urllib.parse import urlparse

from core.interfaces import NotConfigured
from instinct.config import InstinctSettings
from instinct.models import Observation, PageElement

__all__ = ["BrowserSession", "BrowserFactory", "PlaywrightBrowserFactory", "PlaywrightSession", "element_selector"]

ELEMENT_ATTR = "data-omni-id"

# Tags interactive elements with data-omni-id and returns a compact description of each.
_OBSERVE_JS = """
(maxItems) => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],' +
              '[role=tab],[role=menuitem],[role=checkbox],[contenteditable=true],[onclick]';
  document.querySelectorAll('[%ATTR%]').forEach(e => e.removeAttribute('%ATTR%'));
  const out = [];
  let id = 1;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= maxItems) break;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > window.innerHeight * 3) continue;
    el.setAttribute('%ATTR%', String(id));
    const label = (el.innerText || el.value || el.getAttribute('aria-label') ||
                   el.getAttribute('placeholder') || el.getAttribute('title') ||
                   el.getAttribute('name') || '').replace(/\\s+/g, ' ').trim().slice(0, 100);
    out.push({id: id, tag: el.tagName.toLowerCase(), text: label,
              type: el.getAttribute('type'), href: el.getAttribute('href')});
    id += 1;
  }
  return out;
}
""".replace("%ATTR%", ELEMENT_ATTR)


def element_selector(element: int | None, selector: str | None) -> str:
    if element is not None:
        return f'[{ELEMENT_ATTR}="{int(element)}"]'
    if selector:
        return selector
    raise ValueError("an element id or a selector is required")


class BrowserSession(Protocol):
    async def goto(self, url: str) -> str: ...
    async def click(self, target: str) -> str: ...
    async def type(self, target: str, text: str, submit: bool) -> str: ...
    async def extract(self, target: str | None, max_chars: int) -> str: ...
    async def screenshot(self) -> bytes: ...
    async def observe(self) -> Observation: ...
    async def current_url(self) -> str: ...
    async def close(self) -> None: ...


class BrowserFactory(Protocol):
    async def new_session(self) -> BrowserSession: ...
    async def close(self) -> None: ...


class PlaywrightSession:
    """One isolated browser context + page per task."""

    def __init__(self, context: Any, page: Any, settings: InstinctSettings) -> None:
        self._context = context
        self.page = page
        self.settings = settings

    def _check_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in self.settings.allowed_schemes:
            raise ValueError(f"URL scheme '{parsed.scheme}' is not allowed")
        return parsed.geturl()

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=self.settings.navigation_timeout_ms)
        except Exception:  # pages that never settle are still usable
            return

    async def goto(self, url: str) -> str:
        target = self._check_url(url)
        response = await self.page.goto(
            target, wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms
        )
        code = response.status if response is not None else "n/a"
        return f"navigated to {self.page.url} (HTTP {code})"

    async def click(self, target: str) -> str:
        await self.page.locator(target).first.click(timeout=self.settings.action_timeout_ms)
        await self._settle()
        return f"clicked {target}; now at {self.page.url}"

    async def type(self, target: str, text: str, submit: bool) -> str:
        locator = self.page.locator(target).first
        await locator.fill(text, timeout=self.settings.action_timeout_ms)
        if submit:
            await locator.press("Enter", timeout=self.settings.action_timeout_ms)
            await self._settle()
        return f"typed {len(text)} chars into {target}" + (" and pressed Enter" if submit else "")

    async def extract(self, target: str | None, max_chars: int) -> str:
        if target:
            text = await self.page.locator(target).first.inner_text(timeout=self.settings.action_timeout_ms)
        else:
            text = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join(str(text).split())
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="jpeg", quality=self.settings.jpeg_quality, full_page=False)

    async def observe(self) -> Observation:
        try:
            raw = await self.page.evaluate(_OBSERVE_JS, self.settings.observation_elements)
            text = await self.page.evaluate("() => document.body ? document.body.innerText : ''")
            title = await self.page.title()
        except Exception as exc:  # navigation in flight: report what we can
            return Observation(url=self.page.url, title="", text=f"(page not readable yet: {exc})")
        limit = self.settings.page_text_chars
        flat = " ".join(str(text).split())
        return Observation(
            url=self.page.url,
            title=title,
            elements=[PageElement.model_validate(e) for e in raw],
            text=flat[:limit],
        )

    async def current_url(self) -> str:
        return str(self.page.url)

    async def close(self) -> None:
        await self._context.close()


class PlaywrightBrowserFactory:
    """Shares one Chromium process; each task gets a fresh context (cookies isolated)."""

    def __init__(self, settings: InstinctSettings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Any:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise NotConfigured("pip install playwright && playwright install chromium", provider="instinct") from exc
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.headless,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            return self._browser

    async def new_session(self) -> PlaywrightSession:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": self.settings.viewport_width, "height": self.settings.viewport_height},
            user_agent=self.settings.user_agent,
        )
        page = await context.new_page()
        page.set_default_timeout(self.settings.action_timeout_ms)
        return PlaywrightSession(context, page, self.settings)

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
