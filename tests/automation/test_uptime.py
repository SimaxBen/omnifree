from __future__ import annotations

from typing import Any

from automation import uptime


def _fake(responses: dict[str, tuple[int, Any]]) -> Any:
    calls: list[tuple[str, str, str | None]] = []

    def request(method: str, url: str, token: str | None = None, timeout: float = 45.0) -> tuple[int, Any]:
        calls.append((method, url, token))
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                return resp
        return 500, "boom"

    request.calls = calls  # type: ignore[attr-defined]
    return request


def test_all_green_with_token_runs_keepalive_probes() -> None:
    req = _fake({"/edge/health": (200, {"hub": {"ok": True}}), "/": (200, "<html>"),
                 "/healthz": (200, {"ok": True}), "/health": (200, {"health": {"ok": True, "detail": "ok"}})})
    checks = uptime.run_checks("tok", request=req)
    assert all(c.ok for c in checks), checks
    probes = [u for m, u, t in req.calls if m == "POST"]
    assert len(probes) == len(uptime.KEEPALIVE_PLUGINS) and all(t == "tok" for m, _, t in req.calls if m == "POST")
    assert "| check | ok |" in uptime.render(checks)


def test_failures_and_missing_token_are_reported() -> None:
    req = _fake({"/edge/health": (200, {"hub": {"ok": False}}), "/": (200, "x"), "/healthz": (200, {"ok": True})})
    checks = {c.name: c for c in uptime.run_checks(None, request=req)}
    assert checks["edge"].ok is False and checks["hub"].ok is True
    assert checks["plugins"].ok is False and "HUB_API_TOKEN" in checks["plugins"].detail


def test_network_error_is_a_failed_check_not_a_crash() -> None:
    def boom(*_a: Any, **_k: Any) -> tuple[int, Any]:
        raise TimeoutError("slow")

    checks = uptime.run_checks(None, request=boom)
    assert not any(c.ok for c in checks) and "TimeoutError" in checks[0].detail
