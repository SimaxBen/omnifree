"""GitHub Actions as a CronAdapter.

* ``list()``: the repository's workflows; ``schedule`` is the first ``on.schedule[].cron`` of
  the workflow file (empty when the workflow has no schedule), ``target`` is the file name,
  ``enabled`` is ``state == active``, ``last_run_at`` comes from the latest run.
* ``trigger(job_id)``: ``workflow_dispatch`` on ``settings.ref`` (``job_id`` = workflow id or
  file name; the workflow must declare ``workflow_dispatch``).
* ``create(job)``: commits ``.github/workflows/<managed_prefix><slug>.yml`` running
  ``curl`` against ``job.target`` (an http(s) URL) on ``job.schedule`` (UTC) and on
  ``workflow_dispatch``. Header values and the body are committed verbatim, so reference
  repository secrets with ``${{ secrets.NAME }}`` instead of literal tokens.
* ``delete(job_id)``: removes a managed workflow file. Workflows not created by OmniFree
  (outside ``managed_prefix``) are refused; ``disable(job_id)`` turns any workflow off.

Settings: ``owner``, ``repo``, ``ref`` (default ``main``), ``managed_prefix``
(default ``omnifree-cron-``), ``api_base``.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shlex
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote

import yaml

from core.interfaces import CronAdapter, CronJob, HealthStatus, PluginMeta, ProviderError

from ._github import GitHubMixin

WORKFLOW_DIR = ".github/workflows"


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ProviderError("job name must contain letters or digits", status_code=400)
    return slug[:60]


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def workflow_crons(text: str) -> list[str]:
    """Cron expressions of a workflow file (YAML 1.1 parses the ``on`` key as ``True``)."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return []
    schedule = triggers.get("schedule") or []
    return [str(item["cron"]) for item in schedule if isinstance(item, dict) and item.get("cron")]


def render_workflow(job: CronJob) -> str:
    command = ["curl", "--fail-with-body", "--silent", "--show-error", "--max-time", "60",
               "-X", job.method.upper()]
    for name, value in job.headers.items():
        command += ["-H", f"{name}: {value}"]
    if job.body is not None:
        command += ["--data-binary", job.body]
    command.append(job.target)
    workflow = {
        "name": f"omnifree cron: {job.name}",
        "on": {"schedule": [{"cron": job.schedule}], "workflow_dispatch": {}},
        "permissions": {"contents": "read"},
        "jobs": {
            "call": {
                "runs-on": "ubuntu-latest",
                "timeout-minutes": 5,
                "steps": [{"name": "Call target", "run": " ".join(shlex.quote(part) for part in command)}],
            }
        },
    }
    return "# Managed by OmniFree (plugins/compute_edge/github_actions.py)\n" + yaml.safe_dump(
        workflow, sort_keys=False, width=1000
    )


class GitHubActionsCronAdapter(GitHubMixin, CronAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="GitHub Actions",
        description="Scheduled GitHub Actions workflows; trigger() is workflow_dispatch.",
        required_secrets=("token",),
        docs_url="https://docs.github.com/en/rest/actions/workflows",
    )

    @property
    def ref(self) -> str:
        return str(self.setting("ref", "main"))

    @property
    def managed_prefix(self) -> str:
        return str(self.setting("managed_prefix", "omnifree-cron-"))

    async def _workflows(self) -> list[dict[str, Any]]:
        payload = await self.gh_json("GET", f"/repos/{self._gh_repo()}/actions/workflows",
                                     params={"per_page": 100})
        return payload.get("workflows") or []

    async def _file(self, path: str) -> tuple[str, str] | None:
        """``(text, sha)`` of a repository file on ``ref``, or None."""
        try:
            payload = await self.gh_json("GET", f"/repos/{self._gh_repo()}/contents/{quote(path)}",
                                         params={"ref": self.ref})
        except ProviderError as exc:
            if exc.status_code == 404:
                return None
            raise
        text = base64.b64decode(payload.get("content") or "").decode("utf-8", errors="replace")
        return text, str(payload.get("sha"))

    async def _job(self, workflow: dict[str, Any]) -> CronJob:
        path = str(workflow.get("path") or "")
        file, runs = await asyncio.gather(
            self._file(path),
            self.gh_json("GET", f"/repos/{self._gh_repo()}/actions/workflows/{workflow['id']}/runs",
                         params={"per_page": 1}),
        )
        crons = workflow_crons(file[0]) if file else []
        last = (runs.get("workflow_runs") or [{}])[0] if runs.get("workflow_runs") else {}
        return CronJob(
            id=str(workflow["id"]),
            name=str(workflow.get("name") or path),
            schedule=crons[0] if crons else "",
            target=path.rsplit("/", 1)[-1],
            method="POST",
            enabled=workflow.get("state") == "active",
            provider=self.name,
            last_run_at=_parse_dt(last.get("created_at")),
        )

    async def _find(self, job_id: str) -> dict[str, Any]:
        for workflow in await self._workflows():
            path = str(workflow.get("path") or "")
            if job_id in (str(workflow.get("id")), path, path.rsplit("/", 1)[-1], workflow.get("name")):
                return workflow
        raise ProviderError(f"workflow {job_id} not found", provider=self.name, status_code=404)

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(lambda: self.gh_json("GET", f"/repos/{self._gh_repo()}"))

    async def list(self) -> list[CronJob]:
        workflows = [w for w in await self._workflows() if str(w.get("path", "")).startswith(WORKFLOW_DIR)]
        return list(await asyncio.gather(*(self._job(w) for w in workflows)))

    async def create(self, job: CronJob) -> CronJob:
        if not job.target.startswith(("https://", "http://")):
            raise ProviderError("GitHub Actions cron jobs need an http(s) URL target", provider=self.name,
                                status_code=400)
        if len(job.schedule.split()) != 5:
            raise ProviderError("schedule must be a 5-field cron expression", provider=self.name,
                                status_code=400)
        file_name = f"{self.managed_prefix}{_slug(job.name)}.yml"
        path = f"{WORKFLOW_DIR}/{file_name}"
        existing = await self._file(path)
        body: dict[str, Any] = {
            "message": f"omnifree: schedule {job.name}",
            "content": base64.b64encode(render_workflow(job).encode("utf-8")).decode("ascii"),
            "branch": self.ref,
        }
        if existing:
            body["sha"] = existing[1]
        await self.gh_json("PUT", f"/repos/{self._gh_repo()}/contents/{quote(path)}", json=body)
        try:
            workflow = await self._find(file_name)
            job_id = str(workflow["id"])
        except ProviderError:
            job_id = file_name  # GitHub registers new workflow files asynchronously
        if not job.enabled and job_id != file_name:
            await self.disable(job_id)
        return job.model_copy(update={"id": job_id, "provider": self.name})

    async def delete(self, job_id: str) -> None:
        workflow = await self._find(job_id)
        path = str(workflow.get("path") or "")
        if not path.rsplit("/", 1)[-1].startswith(self.managed_prefix):
            raise ProviderError(
                f"refusing to delete {path}: not managed by OmniFree (use disable())",
                provider=self.name, status_code=400,
            )
        existing = await self._file(path)
        if existing is None:
            return
        await self.gh_json("DELETE", f"/repos/{self._gh_repo()}/contents/{quote(path)}",
                           json={"message": f"omnifree: remove {path}", "sha": existing[1], "branch": self.ref})

    async def trigger(self, job_id: str) -> None:
        workflow_ref = job_id if job_id.endswith((".yml", ".yaml")) or job_id.isdigit() else str(
            (await self._find(job_id))["id"])
        await self.gh("POST", f"/repos/{self._gh_repo()}/actions/workflows/{quote(workflow_ref)}/dispatches",
                      json={"ref": self.ref})

    # ------------------------------------------------------------------ extras
    async def disable(self, job_id: str) -> None:
        workflow = await self._find(job_id)
        await self.gh("PUT", f"/repos/{self._gh_repo()}/actions/workflows/{workflow['id']}/disable")

    async def enable(self, job_id: str) -> None:
        workflow = await self._find(job_id)
        await self.gh("PUT", f"/repos/{self._gh_repo()}/actions/workflows/{workflow['id']}/enable")
