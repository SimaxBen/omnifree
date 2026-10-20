"""One-shot uptime + keep-alive check for the serverless deployment (run by .github/workflows/uptime.yml).

Checks the public edge/PWA/hub endpoints, then asks the hub to health-check the database and storage
plugins. That second part doubles as a keep-alive: Supabase pauses free projects after a week without
traffic and Neon suspends idle computes.

Standard library only, so the workflow needs no ``pip install``.

    python -m automation.uptime                       # uses the URLs below and $HUB_API_TOKEN
    python -m automation.uptime --json report.json    # also write a machine-readable report

Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

EDGE = os.environ.get("OMNIFREE_EDGE_URL", "https://omnifree-edge.simaxben.workers.dev")
SITE = os.environ.get("OMNIFREE_SITE_URL", "https://omnifree.ilyasouhnine.tech")
HUB = os.environ.get("OMNIFREE_HUB_URL", f"{EDGE}/api")
KEEPALIVE_PLUGINS = ("supabase", "neon", "turso", "mongodb_atlas", "upstash_redis", "cloudflare_d1", "backblaze_b2")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    ms: int


def _request(method: str, url: str, token: str | None = None, timeout: float = 45.0) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "omnifree-uptime/1"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if method == "POST":
        req.data = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        return status, json.loads(body or b"null")
    except ValueError:
        return status, body[:200].decode("utf-8", "replace")


def _timed(name: str, fn: Any) -> Check:
    start = time.monotonic()
    try:
        ok, detail = fn()
    except Exception as exc:  # network errors are check failures, not crashes
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return Check(name, bool(ok), str(detail)[:300], int((time.monotonic() - start) * 1000))


def run_checks(token: str | None, request: Any = _request) -> list[Check]:
    checks: list[Check] = []

    def edge() -> tuple[bool, str]:
        status, body = request("GET", f"{EDGE}/edge/health")
        hub_ok = isinstance(body, dict) and (body.get("hub") or {}).get("ok")
        return status == 200 and bool(hub_ok), f"HTTP {status} hub_ok={hub_ok}"

    def site() -> tuple[bool, str]:
        status, _ = request("GET", f"{SITE}/")
        return status == 200, f"HTTP {status}"

    def hub() -> tuple[bool, str]:
        status, body = request("GET", f"{HUB}/healthz")
        return status == 200 and isinstance(body, dict) and body.get("ok") is True, f"HTTP {status}"

    checks += [_timed("edge", edge), _timed("site", site), _timed("hub", hub)]
    if not token:
        checks.append(Check("plugins", False, "HUB_API_TOKEN not set: keep-alive checks skipped", 0))
        return checks
    for plugin in KEEPALIVE_PLUGINS:
        def probe(plugin: str = plugin) -> tuple[bool, str]:
            status, body = request("POST", f"{HUB}/plugins/{plugin}/health", token)
            health = body.get("health", body) if isinstance(body, dict) else {}
            return status == 200 and health.get("ok") is True, f"HTTP {status} {health.get('detail', '')}"
        checks.append(_timed(f"plugin:{plugin}", probe))
    return checks


def render(checks: list[Check]) -> str:
    lines = ["| check | ok | ms | detail |", "|---|---|---|---|"]
    lines += [f"| {c.name} | {'✅' if c.ok else '❌'} | {c.ms} | {c.detail.replace('|', '/')} |" for c in checks]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.uptime")
    parser.add_argument("--json", help="write the report to this file")
    args = parser.parse_args(argv)
    checks = run_checks(os.environ.get("HUB_API_TOKEN") or None)
    report = render(checks)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## OmniFree uptime\n\n" + report + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks],
                       "markdown": report}, fh, indent=2)
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
