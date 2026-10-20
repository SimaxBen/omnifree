from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from healthmonitor.app import create_app
from healthmonitor.monitor import (
    HealthMonitor,
    JobSpec,
    MonitorConfig,
    TargetSpec,
    UnresolvedReference,
    expand,
    load_config,
)

ROOT = Path(__file__).resolve().parents[2]


def test_expand_env_secret_and_default() -> None:
    secrets = {"HUB_API_TOKEN": "tok"}.get
    env = {"HUB_URL": "https://hub"}
    assert expand("${HUB_URL}/x", secrets, env) == "https://hub/x"
    assert expand("Bearer ${secret:HUB_API_TOKEN}", secrets, env) == "Bearer tok"
    assert expand("${MISSING:-fallback}", secrets, env) == "fallback"
    with pytest.raises(UnresolvedReference):
        expand("${EDGE_URL}/edge/health", secrets, env)


def test_default_targets_file_loads() -> None:
    cfg = load_config(ROOT / "healthmonitor" / "targets.yaml")
    names = {t.name for t in cfg.targets}
    assert {"hub", "hub-auth", "edge-worker", "freellmapi", "pihole"} <= names
    assert any(j.name == "supabase-keepalive" and j.every_s == 86400 for j in cfg.jobs)
    assert all(t.tailnet for t in cfg.targets if "oracle-arm" in t.url or "amd-vm" in t.url)


async def test_down_after_threshold_alerts_once_and_recovers() -> None:
    state = {"hub_ok": False}
    alerts: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            alerts.append(json.loads(request.content))
            return httpx.Response(204)
        if request.url.path == "/healthz":
            assert request.headers.get("authorization") == "Bearer tok"
            return httpx.Response(200 if state["hub_ok"] else 503, text="ok")
        return httpx.Response(404)

    secrets = {"HUB_API_TOKEN": "tok", "HEALTHMONITOR_ALERT_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x"}
    cfg = MonitorConfig(failure_threshold=2, targets=[
        TargetSpec("hub", "${HUB_URL}/healthz", headers={"Authorization": "Bearer ${secret:HUB_API_TOKEN}"}),
        TargetSpec("edge", "${EDGE_URL}/edge/health"),
        TargetSpec("ollama", "http://oracle-arm:11434/api/tags", tailnet=True),
    ])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mon = HealthMonitor(cfg, secret_lookup=secrets.get, client=client, env={"HUB_URL": "https://hub"})
    await mon.sweep()
    assert mon.states["hub"].status == "unknown" and alerts == []
    await mon.sweep()
    assert mon.states["hub"].status == "down" and len(alerts) == 1
    assert "DOWN hub" in alerts[0]["content"]
    await mon.sweep()
    assert len(alerts) == 1  # no repeat while still down
    state["hub_ok"] = True
    await mon.sweep()
    assert mon.states["hub"].status == "up" and "RECOVERED hub" in alerts[1]["content"]
    snap = mon.snapshot()
    assert snap["targets"]["edge"]["status"] == "disabled"
    assert "tailnet-only" in snap["targets"]["ollama"]["last_error"]
    assert snap["down"] == []
    await client.aclose()


async def test_jobs_run_on_schedule_and_heartbeat() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(f"{request.method} {request.url}")
        if "supabase" in request.url.host:
            assert request.headers["apikey"] == "srk"
        return httpx.Response(200)

    now = [1000.0]
    cfg = MonitorConfig(jobs=[JobSpec("keepalive", "${SUPABASE_URL}/rest/v1/", every_s=100,
                                      headers={"apikey": "${secret:SUPABASE_SERVICE_ROLE_KEY}"})])
    secrets = {"SUPABASE_SERVICE_ROLE_KEY": "srk", "HEALTHMONITOR_HEARTBEAT_URL": "https://hc-ping.com/uuid"}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mon = HealthMonitor(cfg, secret_lookup=secrets.get, client=client, env={"SUPABASE_URL": "https://abc.supabase.co"},
                        clock=lambda: now[0])
    await mon.sweep()
    await mon.sweep()  # not due yet
    now[0] += 150
    await mon.sweep()
    jobs = [h for h in hits if "supabase" in h]
    beats = [h for h in hits if "hc-ping" in h]
    assert len(jobs) == 2 and len(beats) == 3
    assert mon.job_results["keepalive"]["ok"] is True
    await client.aclose()


def test_status_app() -> None:
    cfg = MonitorConfig(targets=[TargetSpec("hub", "${HUB_URL}/healthz")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    mon = HealthMonitor(cfg, secret_lookup=lambda _: None, client=client, env={})
    with TestClient(create_app(mon, run_loop=False)) as tc:
        assert tc.get("/healthz").json() == {"ok": True, "sweeps": 0}
        body = tc.get("/status").json()
        assert body["targets"]["hub"]["status"] == "unknown"
        assert "jobs" not in body and "last_error" not in body["targets"]["hub"]


def test_status_app_full_report_needs_token() -> None:
    cfg = MonitorConfig(targets=[TargetSpec("hub", "${HUB_URL}/healthz")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    secrets = {"HEALTHMONITOR_STATUS_TOKEN": "st"}
    mon = HealthMonitor(cfg, secret_lookup=secrets.get, client=client, env={})
    with TestClient(create_app(mon, run_loop=False)) as tc:
        assert "jobs" not in tc.get("/status", headers={"Authorization": "Bearer nope"}).json()
        full = tc.get("/status", headers={"Authorization": "Bearer st"}).json()
        assert "jobs" in full and "last_error" in full["targets"]["hub"]
