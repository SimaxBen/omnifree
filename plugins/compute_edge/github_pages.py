"""GitHub Pages adapter. Each configured repository's Pages site is one instance
(id ``owner/repo``); the state comes from the site status and its latest build
(built -> running, building/queued -> deploying, errored -> error, Pages disabled -> stopped).

Settings: ``owner`` + ``repo`` (required), optional ``repos`` (list of ``owner/repo``
to show several sites), ``api_base``. Extras: ``deployments(id)``, ``redeploy(id)``
(``POST /pages/builds``, legacy branch builds only; workflow-built sites redeploy through
their Actions workflow).
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from core.interfaces import ComputeAdapter, HealthStatus, PluginMeta, ProviderError, VMInstance

from ._github import GitHubMixin

_STATES = {"built": "running", "building": "deploying", "queued": "deploying", "errored": "error"}


class GitHubPagesAdapter(GitHubMixin, ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="GitHub Pages",
        description="GitHub Pages sites and builds (static PWA fallback).",
        required_secrets=("token",),
        docs_url="https://docs.github.com/en/rest/pages/pages",
    )

    def _repos(self) -> list[str]:
        repos = self.setting("repos") or []
        return [str(r) for r in repos] if repos else [self._gh_repo()]

    @staticmethod
    def _check_id(instance_id: str) -> str:
        if instance_id.count("/") != 1:
            raise ProviderError("instance id must be 'owner/repo'", status_code=400)
        return instance_id

    async def _site(self, repo: str) -> VMInstance:
        try:
            site = await self.gh_json("GET", f"/repos/{repo}/pages")
        except ProviderError as exc:
            if exc.status_code != 404:
                raise
            return VMInstance(id=repo, name=repo, provider=self.name, state="stopped",
                              meta={"pages_enabled": False})
        latest: dict[str, Any] = {}
        if site.get("build_type") != "workflow":
            try:
                latest = await self.gh_json("GET", f"/repos/{repo}/pages/builds/latest")
            except ProviderError as exc:
                if exc.status_code != 404:
                    raise
        status = str(latest.get("status") or site.get("status") or "")
        return VMInstance(
            id=repo,
            name=repo.split("/", 1)[1],
            provider=self.name,
            state=_STATES.get(status, "unknown"),
            url=site.get("html_url"),
            meta={
                "pages_enabled": True,
                "status": status or None,
                "build_type": site.get("build_type"),
                "cname": site.get("cname"),
                "source": site.get("source"),
                "commit": latest.get("commit"),
                "error": (latest.get("error") or {}).get("message"),
                "updated_at": latest.get("updated_at"),
            },
        )

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self.gh_json("GET", f"/repos/{self._repos()[0]}"))

    async def list_instances(self) -> list[VMInstance]:
        return list(await asyncio.gather(*(self._site(repo) for repo in self._repos())))

    async def status(self, instance_id: str) -> VMInstance:
        return await self._site(self._check_id(instance_id))

    # ------------------------------------------------------------------ extras
    async def deployments(self, instance_id: str, limit: int = 10) -> list[dict[str, Any]]:
        repo = self._check_id(instance_id)
        builds = await self.gh_json("GET", f"/repos/{repo}/pages/builds", params={"per_page": limit})
        return [
            {"status": b.get("status"), "commit": b.get("commit"), "created_at": b.get("created_at"),
             "duration_ms": b.get("duration"), "error": (b.get("error") or {}).get("message")}
            for b in builds or []
        ]

    async def redeploy(self, instance_id: str) -> dict[str, Any]:
        repo = self._check_id(instance_id)
        build = await self.gh_json("POST", f"/repos/{repo}/pages/builds")
        return {"status": build.get("status"), "url": build.get("url")}
