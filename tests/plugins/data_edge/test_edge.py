from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from core.interfaces import (
    Capability,
    CronJob,
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderKind,
    QuotaExhausted,
)
from plugins.compute_edge.github_actions import GitHubActionsCronAdapter, render_workflow, workflow_crons
from plugins.compute_edge.github_pages import GitHubPagesAdapter
from plugins.compute_edge.koyeb import KoyebAdapter
from plugins.compute_edge.netlify import NetlifyAdapter
from plugins.compute_edge.qstash import QStashCronAdapter
from plugins.compute_edge.vercel import VercelAdapter
from plugins.network.pihole import PiHoleDNSAdapter
from plugins.network.tailscale import TailscaleAdapter

from .helpers import Routes, body, make

# --------------------------------------------------------------------------- Koyeb


async def test_koyeb_services_power_and_deployments() -> None:
    routes = Routes()
    services = [
        {"id": "s1", "name": "omnifree-worker", "status": "HEALTHY", "app_id": "a1", "regions": ["fra"],
         "latest_deployment_id": "d1"},
        {"id": "s2", "name": "other", "status": "PAUSED"},
    ]
    routes.add("GET", r"/v1/services\?", {"services": services, "count": 2})
    routes.add("GET", r"/v1/services/s1$", {"service": {**services[0], "status": "PAUSING"}})
    routes.add("POST", r"/v1/services/s1/pause$", {})
    routes.add("POST", r"/v1/services/s1/resume$", {})
    routes.add("GET", r"/v1/deployments\?service_id=s1", {"deployments": [{"id": "d1", "status": "HEALTHY"}]})
    routes.add("POST", r"/v1/services/s1/redeploy$", {"deployment": {"id": "d2", "status": "PENDING"}})
    koyeb = make(KoyebAdapter, ProviderKind.COMPUTE, routes, secrets={"api_token": "kt"},
                 settings={"services": ["omnifree-worker"]})
    assert koyeb.supports(Capability.POWER)
    instances = await koyeb.list_instances()
    assert [(i.id, i.state, i.region) for i in instances] == [("s1", "running", "fra")]
    assert routes.requests[0].headers["Authorization"] == "Bearer kt"
    stopped = await koyeb.stop("omnifree-worker")
    assert stopped.state == "stopping"
    assert routes.find("POST", "/services/s1/pause")
    await koyeb.start("s1")
    assert routes.find("POST", "/services/s1/resume")
    assert (await koyeb.deployments("s1"))[0]["id"] == "d1"
    assert (await koyeb.redeploy("s1"))["id"] == "d2"
    with pytest.raises(ProviderError) as err:
        await koyeb.start("nope")
    assert err.value.status_code == 404


async def test_koyeb_auth_error_and_health() -> None:
    routes = Routes().add("GET", r"/v1/services", {"error": "unauthorized"}, status=401)
    koyeb = make(KoyebAdapter, ProviderKind.COMPUTE, routes, secrets={"api_token": "bad"})
    with pytest.raises(ProviderAuthError):
        await koyeb.list_instances()
    assert (await koyeb.health()).ok is False
    with pytest.raises(NotConfigured):
        await make(KoyebAdapter, ProviderKind.COMPUTE, routes).list_instances()


# --------------------------------------------------------------------------- Vercel / Netlify


async def test_vercel_projects_and_states() -> None:
    routes = Routes()
    routes.add("GET", r"/v9/projects\?", {"projects": [
        {"id": "p1", "name": "omnifree-pwa", "targets": {"production": {"alias": ["omnifree.vercel.app"]}}},
        {"id": "p2", "name": "ignored"}]})
    routes.add("GET", r"/v6/deployments\?projectId=p1", {"deployments": [
        {"uid": "dpl_1", "state": "BUILDING", "url": "omnifree-abc.vercel.app", "created": 1}]})
    routes.add("GET", r"/v9/projects/p1\?", {"id": "p1", "name": "omnifree-pwa"})
    routes.add("POST", r"/v13/deployments\?", lambda r: {"id": "dpl_2", "readyState": "QUEUED", "echo": body(r)})
    vercel = make(VercelAdapter, ProviderKind.COMPUTE, routes, secrets={"token": "vt", "team_id": "team_1"},
                  settings={"projects": ["omnifree-pwa"]})
    instances = await vercel.list_instances()
    assert len(instances) == 1
    inst = instances[0]
    assert inst.state == "deploying" and inst.url == "https://omnifree.vercel.app"
    assert inst.meta["deployment_id"] == "dpl_1"
    assert all(r.url.params.get("teamId") == "team_1" for r in routes.requests)
    status = await vercel.status("p1")
    assert status.url == "https://omnifree-abc.vercel.app"
    redeployed = await vercel.redeploy("p1")
    assert redeployed["id"] == "dpl_2"
    assert body(routes.find("POST", "/v13/deployments")[0])["deploymentId"] == "dpl_1"


async def test_netlify_sites() -> None:
    routes = Routes()
    routes.add("GET", r"/api/v1/sites\?per_page=100", [
        {"id": "n1", "name": "omnifree-pwa", "ssl_url": "https://omnifree-pwa.netlify.app"},
        {"id": "n2", "name": "other"}])
    routes.add("GET", r"/sites/n1/deploys\?per_page=1", [{"id": "d1", "state": "error", "error_message": "boom"}])
    routes.add("GET", r"/sites/n1$", {"id": "n1", "name": "omnifree-pwa"})
    routes.add("POST", r"/sites/n1/builds$", {"id": "b1", "deploy_id": "d2", "done": False})
    netlify = make(NetlifyAdapter, ProviderKind.COMPUTE, routes, secrets={"token": "nt"},
                   settings={"sites": ["omnifree-pwa"]})
    (inst,) = await netlify.list_instances()
    assert inst.state == "error" and inst.meta["error_message"] == "boom"
    assert inst.url == "https://omnifree-pwa.netlify.app"
    assert (await netlify.status("n1")).state == "error"
    assert (await netlify.redeploy("n1"))["deploy_id"] == "d2"


# --------------------------------------------------------------------------- GitHub Pages / Actions


async def test_github_pages_states() -> None:
    routes = Routes()
    routes.add("GET", r"/repos/me/omnifree/pages$", {"html_url": "https://me.github.io/omnifree/",
                                                     "status": "built", "build_type": "legacy"})
    routes.add("GET", r"/repos/me/omnifree/pages/builds/latest$", {"status": "building", "commit": "abc"})
    routes.add("GET", r"/repos/me/nopages/pages$", {"message": "Not Found"}, status=404)
    pages = make(GitHubPagesAdapter, ProviderKind.COMPUTE, routes, secrets={"token": "gt"},
                 settings={"owner": "me", "repo": "omnifree", "repos": ["me/omnifree", "me/nopages"]})
    instances = await pages.list_instances()
    assert [(i.id, i.state) for i in instances] == [("me/omnifree", "deploying"), ("me/nopages", "stopped")]
    assert instances[0].url == "https://me.github.io/omnifree/"
    req = routes.requests[0]
    assert req.headers["Authorization"] == "Bearer gt" and req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    with pytest.raises(NotConfigured):
        await make(GitHubPagesAdapter, ProviderKind.COMPUTE, routes, secrets={"token": "gt"},
                   settings={"owner": None, "repo": "x"}).list_instances()


def test_workflow_render_and_parse_roundtrip() -> None:
    job = CronJob(name="Nightly ping", schedule="0 3 * * *", target="https://hub.example/cron?x=1",
                  headers={"Authorization": "Bearer ${{ secrets.HUB_TOKEN }}"}, body='{"a": 1}')
    text = render_workflow(job)
    assert workflow_crons(text) == ["0 3 * * *"]
    doc = yaml.safe_load(text)
    run = doc["jobs"]["call"]["steps"][0]["run"]
    assert "'https://hub.example/cron?x=1'" in run and "${{ secrets.HUB_TOKEN }}" in run
    assert "workflow_dispatch" in doc.get("on", doc.get(True))
    assert workflow_crons("on: push\n") == []


async def test_github_actions_cron_adapter() -> None:
    routes = Routes()
    workflow_file = "name: ci\non:\n  schedule:\n    - cron: '*/30 * * * *'\n  workflow_dispatch: {}\n"
    workflows = [
        {"id": 11, "name": "ci", "path": ".github/workflows/ci.yml", "state": "active"},
        {"id": 12, "name": "omnifree cron: ping", "path": ".github/workflows/omnifree-cron-ping.yml",
         "state": "disabled_manually"},
    ]
    routes.add("GET", r"/actions/workflows\?per_page=100", {"workflows": workflows})
    routes.add("GET", r"/contents/.github/workflows/ci\.yml\?ref=main",
               {"content": base64.b64encode(workflow_file.encode()).decode(), "sha": "s-ci"})
    routes.add("GET", r"/contents/.github/workflows/omnifree-cron-ping\.yml\?ref=main",
               {"content": base64.b64encode(b"on:\n  workflow_dispatch: {}\n").decode(), "sha": "s-ping"})
    routes.add("GET", r"/contents/.github/workflows/omnifree-cron-new-job\.yml", {"message": "Not Found"}, status=404)
    routes.add("GET", r"/workflows/11/runs", {"workflow_runs": [{"created_at": "2026-10-01T03:00:00Z"}]})
    routes.add("GET", r"/workflows/12/runs", {"workflow_runs": []})
    routes.add("POST", r"/actions/workflows/[^/]+/dispatches$", None, status=204)
    routes.add("PUT", r"/contents/.github/workflows/omnifree-cron-new-job\.yml$", {"content": {}}, status=201)
    routes.add("DELETE", r"/contents/.github/workflows/omnifree-cron-ping\.yml$", {"commit": {}})
    gha = make(GitHubActionsCronAdapter, ProviderKind.CRON, routes, secrets={"token": "gt"},
               settings={"owner": "me", "repo": "omnifree", "ref": "main"})

    jobs = await gha.list()
    assert [(j.id, j.schedule, j.enabled, j.target) for j in jobs] == [
        ("11", "*/30 * * * *", True, "ci.yml"), ("12", "", False, "omnifree-cron-ping.yml")]
    assert jobs[0].last_run_at == datetime(2026, 10, 1, 3, tzinfo=timezone.utc)

    await gha.trigger("ci.yml")
    await gha.trigger("ci")  # by name -> resolved to id
    dispatches = routes.find("POST", "/dispatches")
    assert [str(r.url).split("/workflows/")[1] for r in dispatches] == ["ci.yml/dispatches", "11/dispatches"]
    assert body(dispatches[0]) == {"ref": "main"}

    created = await gha.create(CronJob(name="New job", schedule="0 * * * *", target="https://hub/x"))
    put = body(routes.find("PUT", "omnifree-cron-new-job.yml")[0])
    assert put["branch"] == "main" and "sha" not in put
    assert "0 * * * *" in base64.b64decode(put["content"]).decode()
    assert created.id == "omnifree-cron-new-job.yml" and created.provider == "test"

    with pytest.raises(ProviderError):
        await gha.create(CronJob(name="bad", schedule="0 * * * *", target="ci.yml"))
    with pytest.raises(ProviderError, match="not managed"):
        await gha.delete("11")
    await gha.delete("12")
    assert body(routes.find("DELETE", "omnifree-cron-ping.yml")[0])["sha"] == "s-ping"


async def test_github_rate_limit_maps_to_quota() -> None:
    routes = Routes().add("GET", r"/repos/me/omnifree$", {"message": "API rate limit exceeded for user"}, status=403)
    gha = make(GitHubActionsCronAdapter, ProviderKind.CRON, routes, secrets={"token": "gt"},
               settings={"owner": "me", "repo": "omnifree"})
    with pytest.raises(QuotaExhausted):
        await gha.gh("GET", "/repos/me/omnifree")


# --------------------------------------------------------------------------- QStash


async def test_qstash_schedules() -> None:
    routes = Routes()
    routes.add("POST", r"/v2/schedules/https://hub\.example/cron$", {"scheduleId": "scd_1"}, status=201)
    routes.add("POST", r"/v2/schedules/scd_1/pause$", None)
    schedule = {"scheduleId": "scd_1", "cron": "*/5 * * * *", "destination": "https://hub.example/cron",
                "method": "POST", "header": {"X-Omnifree-Job": ["health"], "X-Token": ["abc"]},
                "body": "{}", "isPaused": True, "createdAt": 1, "lastScheduleTime": 1760000000000}
    routes.add("GET", r"/v2/schedules$", [schedule])
    routes.add("GET", r"/v2/schedules/scd_1$", schedule)
    routes.add("DELETE", r"/v2/schedules/scd_1$", None)
    routes.add("POST", r"/v2/publish/https://hub\.example/cron$", {"messageId": "m1"}, status=201)
    qstash = make(QStashCronAdapter, ProviderKind.CRON, routes, secrets={"token": "qt"},
                  settings={"base_url": "https://qstash.upstash.io"})
    created = await qstash.create(CronJob(name="health", schedule="*/5 * * * *", target="https://hub.example/cron",
                                          headers={"X-Token": "abc", "Content-Type": "application/json"},
                                          body="{}", enabled=False))
    assert created.id == "scd_1"
    req = routes.requests[0]
    assert req.headers["Upstash-Cron"] == "*/5 * * * *" and req.headers["Upstash-Method"] == "POST"
    assert req.headers["Upstash-Forward-X-Token"] == "abc"
    assert req.headers["Upstash-Forward-X-Omnifree-Job"] == "health"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer qt"
    assert routes.find("POST", "/pause")

    (job,) = await qstash.list()
    assert job.name == "health" and job.headers == {"X-Token": "abc"} and job.enabled is False
    assert job.last_run_at is not None
    await qstash.trigger("scd_1")
    publish = routes.find("POST", "/v2/publish/")[0]
    assert publish.content == b"{}" and publish.headers["Upstash-Forward-X-Token"] == "abc"
    await qstash.delete("scd_1")
    assert routes.find("DELETE", "/v2/schedules/scd_1")


def _jwt(key: str, claims: dict) -> str:
    def enc(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    head = enc({"alg": "HS256", "typ": "JWT"}) + "." + enc(claims)
    sig = base64.urlsafe_b64encode(hmac.new(key.encode(), head.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return head + "." + sig


def test_qstash_signature_verification() -> None:
    qstash = make(QStashCronAdapter, ProviderKind.CRON, None,
                  secrets={"token": "t", "current_signing_key": "cur", "next_signing_key": "nxt"})
    payload = b'{"hello": 1}'
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode()
    claims = {"iss": "Upstash", "sub": "https://hub/cron", "exp": time.time() + 60, "nbf": time.time() - 1,
              "body": digest}
    assert qstash.verify_signature(_jwt("nxt", claims), payload, "https://hub/cron")
    assert not qstash.verify_signature(_jwt("wrong", claims), payload)
    assert not qstash.verify_signature(_jwt("cur", claims), b"tampered")
    assert not qstash.verify_signature(_jwt("cur", {**claims, "exp": time.time() - 10}), payload)
    assert not qstash.verify_signature("garbage", payload)
    with pytest.raises(NotConfigured):
        make(QStashCronAdapter, ProviderKind.CRON, None, secrets={"token": "t"}).verify_signature("a.b.c", b"")


# --------------------------------------------------------------------------- Pi-hole


def pihole_routes() -> Routes:
    routes = Routes()
    routes.add("POST", r"/api/auth$", lambda r: {"session": {"valid": body(r)["password"] == "pw", "sid": "SID",
                                                             "csrf": "CSRF", "validity": 1800}})
    routes.add("DELETE", r"/api/auth$", None, status=204)
    return routes


async def test_pihole_status_toggle_log_records() -> None:
    routes = pihole_routes()
    routes.add("GET", r"/api/dns/blocking$", {"blocking": "enabled", "timer": None})
    routes.add("POST", r"/api/dns/blocking$", lambda r: {"blocking": "enabled" if body(r)["blocking"] else "disabled",
                                                         "timer": body(r)["timer"]})
    routes.add("GET", r"/api/stats/summary$", {"queries": {"total": 1000, "blocked": 150, "percent_blocked": 15.0},
                                               "gravity": {"domains_being_blocked": 120000}})
    routes.add("GET", r"/api/queries\?length=", {"queries": [
        {"time": 1760000000.5, "type": "A", "domain": "ads.example", "status": "GRAVITY",
         "client": {"ip": "100.64.0.2", "name": "phone"}},
        {"time": 1760000001.0, "type": "AAAA", "domain": "ok.example", "status": "FORWARDED",
         "client": {"ip": "100.64.0.3", "name": None}},
    ]})
    routes.add("GET", r"/api/config/dns$", {"config": {"dns": {
        "hosts": ["100.64.0.10 hub.lan hub", "fd7a::1 v6.lan"], "cnameRecords": ["pwa.lan,hub.lan,300"]}}})
    pihole = make(PiHoleDNSAdapter, ProviderKind.DNS, routes, secrets={"password": "pw"},
                  settings={"base_url": "http://amd-vm1", "api_version": 6})

    status = await pihole.status()
    assert status.blocking_enabled and status.queries_today == 1000 and status.domains_on_blocklist == 120000
    toggled = await pihole.toggle_blocking(False, 300)
    assert not toggled.blocking_enabled and toggled.blocking_resumes_at is not None
    assert body(routes.find("POST", "/api/dns/blocking")[0]) == {"blocking": False, "timer": 300}
    assert routes.find("GET", "/api/dns/blocking")[0].headers["X-FTL-SID"] == "SID"
    assert len(routes.find("POST", "/api/auth")) == 1  # session reused

    log = await pihole.query_log(limit=10)
    assert [(e.domain, e.blocked, e.client) for e in log] == [("ads.example", True, "phone"),
                                                              ("ok.example", False, "100.64.0.3")]
    blocked = await pihole.query_log(limit=5, blocked_only=True)
    assert [e.domain for e in blocked] == ["ads.example"]
    assert routes.find("GET", "/api/queries")[-1].url.params["length"] == "50"

    records = await pihole.list_records()
    assert [(r.name, r.type, r.value) for r in records] == [
        ("hub.lan", "A", "100.64.0.10"), ("hub", "A", "100.64.0.10"), ("v6.lan", "AAAA", "fd7a::1"),
        ("pwa.lan", "CNAME", "hub.lan")]
    assert records[-1].ttl == 300
    await pihole.close()
    assert routes.find("DELETE", "/api/auth")


async def test_pihole_reauth_and_bad_password() -> None:
    routes = pihole_routes()
    calls = {"n": 0}

    def blocking(_r):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"key": "unauthorized"}}) if calls["n"] == 1 else \
            httpx.Response(200, json={"blocking": "disabled", "timer": None})

    routes.add("GET", r"/api/dns/blocking$", blocking)
    routes.add("GET", r"/api/stats/summary$", {"error": "x"}, status=500)
    pihole = make(PiHoleDNSAdapter, ProviderKind.DNS, routes, secrets={"password": "pw"},
                  settings={"base_url": "http://amd-vm1"})
    status = await pihole.status()
    assert status.blocking_enabled is False and status.queries_today is None
    assert len(routes.find("POST", "/api/auth")) == 2

    bad = make(PiHoleDNSAdapter, ProviderKind.DNS, routes, secrets={"password": "nope"},
               settings={"base_url": "http://amd-vm1"})
    with pytest.raises(ProviderAuthError):
        await bad.status()
    v5 = make(PiHoleDNSAdapter, ProviderKind.DNS, routes, secrets={"password": "pw"},
              settings={"base_url": "http://x", "api_version": 5})
    with pytest.raises(NotConfigured):
        await v5.status()


# --------------------------------------------------------------------------- Tailscale


async def test_tailscale_devices() -> None:
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    routes = Routes()
    routes.add("GET", r"/api/v2/tailnet/-/devices\?fields=all", {"devices": [
        {"id": "1", "nodeId": "nA", "name": "oracle-arm.tail1234.ts.net", "hostname": "oracle-arm",
         "addresses": ["100.64.0.1", "fd7a:115c::1"], "os": "linux", "lastSeen": "2020-01-01T00:00:00Z",
         "connectedToControl": True, "tags": ["tag:server"]},
        {"id": "2", "nodeId": "nB", "name": "phone.tail1234.ts.net", "hostname": "phone",
         "addresses": ["100.64.0.2"], "lastSeen": recent},
        {"id": "3", "nodeId": "nC", "name": "old.tail1234.ts.net", "hostname": "old",
         "addresses": ["100.64.0.3"], "lastSeen": "2020-01-01T00:00:00Z"},
    ]})
    ts = make(TailscaleAdapter, ProviderKind.COMPUTE, routes, secrets={"api_key": "tskey-api"},
              settings={"tailnet": "-"})
    instances = await ts.list_instances()
    assert [(i.name, i.state, i.tailscale_ip) for i in instances] == [
        ("old", "offline", "100.64.0.3"), ("oracle-arm", "online", "100.64.0.1"), ("phone", "online", "100.64.0.2")]
    assert instances[1].tags == {"server": ""}
    assert routes.requests[0].headers["Authorization"] == "Bearer tskey-api"
    assert (await ts.status("oracle-arm")).id == "nA"
    assert (await ts.status("phone.tail1234.ts.net")).id == "nB"
    with pytest.raises(ProviderError):
        await ts.status("ghost")
    assert not ts.supports(Capability.POWER)
    assert (await ts.health()).ok
