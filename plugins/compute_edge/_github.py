"""GitHub REST helpers shared by the Pages and Actions adapters (private module)."""

from __future__ import annotations

from typing import Any

import httpx

from core.interfaces import ProviderAuthError, QuotaExhausted

DEFAULT_API = "https://api.github.com"


class GitHubMixin:
    """Mix into a ProviderAdapter whose entry has ``token`` secret and ``owner``/``repo`` settings."""

    def _gh_base(self: Any) -> str:
        return str(self.setting("api_base", DEFAULT_API)).rstrip("/")

    def _gh_repo(self: Any) -> str:
        return f"{self.require_setting('owner')}/{self.require_setting('repo')}"

    async def gh(self: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.require_secret('token')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **kwargs.pop("headers", {}),
        }
        try:
            return await self.request(method, self._gh_base() + path, headers=headers, **kwargs)
        except ProviderAuthError as exc:
            # GitHub signals primary/secondary rate limiting with 403 + a "rate limit" message.
            if exc.status_code == 403 and "rate limit" in exc.message.lower():
                raise QuotaExhausted(exc.message, provider=self.name, status_code=403) from exc
            raise

    async def gh_json(self: Any, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.gh(method, path, **kwargs)
        return response.json() if response.content else {}
