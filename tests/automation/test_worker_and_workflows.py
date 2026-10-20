"""Static checks on the Cloudflare Worker, GitHub workflows and README (no network)."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "deploy" / "cloudflare-worker"


def test_wrangler_config() -> None:
    cfg = tomllib.loads((WORKER / "wrangler.toml").read_text(encoding="utf-8"))
    assert cfg["main"] == "src/index.ts" and cfg["name"] == "omnifree-edge"
    assert set(cfg["vars"]) == {"HUB_ORIGIN", "PWA_ORIGINS", "API_PREFIX", "ALLOWED_ORIGINS"}
    src = (WORKER / "src" / "index.ts").read_text(encoding="utf-8")
    for var in cfg["vars"]:
        assert var in src
    assert "X-Routed-Via" in src and "Access-Control-Expose-Headers" in src


@pytest.mark.skipif(not (WORKER / "node_modules" / "typescript").is_dir() or shutil.which("node") is None,
                    reason="worker dev dependencies not installed (npm ci in deploy/cloudflare-worker)")
def test_worker_typechecks() -> None:
    tsc = WORKER / "node_modules" / "typescript" / "bin" / "tsc"
    proc = subprocess.run([shutil.which("node") or "node", str(tsc), "--noEmit", "-p", str(WORKER)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_workflows_are_valid() -> None:
    wf_dir = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((wf_dir / "ci.yml").read_text(encoding="utf-8"))
    hub = yaml.safe_load((wf_dir / "deploy-hub.yml").read_text(encoding="utf-8"))
    for wf in (ci, hub):
        assert wf["jobs"]
        triggers = wf.get("on", wf.get(True))  # PyYAML parses a bare `on:` key as True
        assert triggers
    ci_text = (wf_dir / "ci.yml").read_text(encoding="utf-8")
    assert "pytest" in ci_text and "npm run build" in ci_text
    hub_text = (wf_dir / "deploy-hub.yml").read_text(encoding="utf-8")
    assert "automation.deploy deploy-hub" in hub_text and "${{ secrets." in hub_text


def test_readme_runbook_mentions_every_subcommand() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in ("browser_provisioner", "oci-provision", "ssh-bootstrap", "deploy-arm", "deploy-pihole", "deploy-edge",
                "deploy-hub", "deploy-worker", "deploy-pwa", "deploy-monitor", "budgets", "verify", "qr", "lock-ssh"):
        assert cmd in readme, cmd
