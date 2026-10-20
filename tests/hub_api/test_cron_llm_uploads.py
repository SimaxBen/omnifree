"""Hub routes: /cron, /llm/rerank, /llm/transcribe, /drive/upload-url."""

from __future__ import annotations

import pytest

from hub_api.routers.cron import mask_headers, validate_cron
from tests.hub_api import fakes_routes as fr

F = "tests.hub_api.fakes_routes"


@pytest.fixture(autouse=True)
def _reset_route_fakes() -> None:
    fr.reset()


@pytest.fixture
def cron_hub(make_hub, plugins):
    plugins["upstash_qstash"] = {"module": f"{F}:FakeCron", "kind": "cron", "group": "compute_edge",
                                 "priority": 10, "description": "QStash schedules"}
    plugins["github_actions"] = {"module": f"{F}:FakeCron", "kind": "cron", "group": "compute_edge",
                                 "priority": 20, "description": "GitHub workflows"}
    plugins["cron_off"] = {"module": f"{F}:FakeCron", "kind": "cron", "group": "compute_edge",
                           "priority": 30, "enabled": False}
    plugins["cron_manual"] = {"module": f"{F}:FakeNoTriggerCron", "kind": "cron", "group": "compute_edge",
                              "priority": 40}
    return make_hub(plugins)


JOB = {
    "provider": "upstash_qstash",
    "name": "ping",
    "schedule": "*/5 * * * *",
    "target": "https://example.com/hook",
    "headers": {"Authorization": "Bearer s3cret", "X-Api-Key": "k", "X-Trace": "visible"},
    "body": '{"hello":1}',
}


# ------------------------------------------------------------------ cron


def test_validate_cron_and_mask_headers() -> None:
    assert validate_cron("  0   3 * * 1-5 ") == "0 3 * * 1-5"
    assert validate_cron("0 0 1 JAN,JUL MON") == "0 0 1 JAN,JUL MON"
    for bad in ("* * * *", "* * * * * *", "@daily", "0 3 * * $(rm)", ""):
        with pytest.raises(ValueError):
            validate_cron(bad)
    assert mask_headers({"Authorization": "a", "X-Upstash-Token": "b", "Client-Secret": "c", "x-api-key": "d",
                         "Content-Type": "text/plain"}) == {
        "Authorization": "***", "X-Upstash-Token": "***", "Client-Secret": "***", "x-api-key": "***",
        "Content-Type": "text/plain",
    }


def test_cron_providers(cron_hub) -> None:
    r = cron_hub.get("/cron/providers")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()["providers"]}
    assert set(by_name) == {"upstash_qstash", "github_actions", "cron_off", "cron_manual"}
    assert by_name["upstash_qstash"] == {"name": "upstash_qstash", "enabled": True, "healthy": None,
                                         "description": "QStash schedules"}
    assert by_name["cron_off"]["enabled"] is False
    cron_hub.get("/plugins", params={"health": True, "kind": "cron"})
    by_name = {p["name"]: p for p in cron_hub.get("/cron/providers").json()["providers"]}
    assert by_name["upstash_qstash"]["healthy"] is True
    assert by_name["cron_off"]["healthy"] is None


def test_cron_create_list_trigger_delete_masks_secrets(cron_hub) -> None:
    r = cron_hub.post("/cron/jobs", json=JOB)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["id"] == "upstash_qstash-1" and job["provider"] == "upstash_qstash"
    assert job["method"] == "POST" and job["schedule"] == "*/5 * * * *" and job["body"] == '{"hello":1}'
    assert job["headers"] == {"Authorization": "***", "X-Api-Key": "***", "X-Trace": "visible"}
    assert set(job) >= {"id", "name", "schedule", "target", "method", "headers", "body", "enabled", "provider",
                        "last_run_at", "next_run_at"}
    # The provider received the real secret; only the response is masked.
    assert fr.JOBS["upstash_qstash"]["upstash_qstash-1"].headers["Authorization"] == "Bearer s3cret"

    gh = cron_hub.post("/cron/jobs", json={**JOB, "provider": "github_actions", "method": "get", "headers": {}})
    assert gh.status_code == 201 and gh.json()["method"] == "GET"

    listing = cron_hub.get("/cron/jobs").json()
    assert [j["provider"] for j in listing["jobs"]] == ["upstash_qstash", "github_actions"]
    assert listing["errors"] == {}
    assert listing["jobs"][0]["headers"]["Authorization"] == "***"
    only = cron_hub.get("/cron/jobs", params={"provider": "github_actions"}).json()
    assert [j["id"] for j in only["jobs"]] == ["github_actions-1"]

    r = cron_hub.post("/cron/jobs/upstash_qstash/upstash_qstash-1/trigger")
    assert r.status_code == 202 and r.json() == {"triggered": True}
    assert fr.TRIGGERS == [("upstash_qstash", "upstash_qstash-1")]
    assert cron_hub.post("/cron/jobs/upstash_qstash/nope/trigger").status_code == 404

    r = cron_hub.delete("/cron/jobs/upstash_qstash/upstash_qstash-1")
    assert r.status_code == 204 and r.content == b""
    assert fr.JOBS["upstash_qstash"] == {}
    assert cron_hub.delete("/cron/jobs/upstash_qstash/upstash_qstash-1").status_code == 404


@pytest.mark.parametrize("patch", [
    {"schedule": "* * * *"},
    {"schedule": "0 0 * * * *"},
    {"schedule": "@hourly"},
    {"method": "BREW"},
    {"name": ""},
    {"target": ""},
])
def test_cron_create_validation_is_422(cron_hub, patch) -> None:
    r = cron_hub.post("/cron/jobs", json={**JOB, **patch})
    assert r.status_code == 422, r.text
    assert fr.JOBS == {}


def test_cron_provider_errors(cron_hub) -> None:
    assert cron_hub.post("/cron/jobs", json={**JOB, "provider": "nope"}).status_code == 404
    assert cron_hub.post("/cron/jobs", json={**JOB, "provider": "llm_a"}).status_code == 400
    assert cron_hub.post("/cron/jobs", json={**JOB, "provider": "cron_off"}).status_code == 409
    assert cron_hub.get("/cron/jobs", params={"provider": "store_big"}).status_code == 400
    cron_hub.post("/cron/jobs", json={**JOB, "provider": "cron_manual"})
    r = cron_hub.post("/cron/jobs/cron_manual/cron_manual-1/trigger")
    assert r.status_code == 501 and r.json()["error"] == "capability_not_supported"


def test_cron_list_aggregates_and_reports_failing_providers(make_hub, plugins) -> None:
    plugins["upstash_qstash"] = {"module": f"{F}:FakeCron", "kind": "cron", "group": "compute_edge", "priority": 10}
    plugins["github_actions"] = {"module": f"{F}:FakeCron", "kind": "cron", "group": "compute_edge", "priority": 20,
                                 "settings": {"behaviour": "down"}}
    hub = make_hub(plugins)
    hub.post("/cron/jobs", json=JOB)
    listing = hub.get("/cron/jobs").json()
    assert [j["id"] for j in listing["jobs"]] == ["upstash_qstash-1"]
    assert set(listing["errors"]) == {"github_actions"} and "503" in listing["errors"]["github_actions"]
    r = hub.get("/cron/jobs", params={"provider": "github_actions"})
    assert r.status_code == 503 and r.json()["error"] == "provider_unavailable"


def test_cron_requires_token(cron_hub) -> None:
    assert cron_hub.client.get("/cron/providers").status_code == 401
    assert cron_hub.client.post("/cron/jobs", json=JOB).status_code == 401


# ------------------------------------------------------------------ llm rerank / transcribe


@pytest.fixture
def audio_hub(make_hub, plugins):
    plugins["cohere"] = {"module": f"{F}:FakeAudioLLM", "kind": "llm", "group": "llm_pool", "priority": 40,
                         "settings": {"default_model": "command", "rerank_models": ["rerank-v3.5"]}}
    plugins["groq"] = {"module": f"{F}:FakeAudioLLM", "kind": "llm", "group": "llm_pool", "priority": 50,
                       "settings": {"default_model": "llama", "transcription_models": ["whisper-large-v3"],
                                    "upstream": "whisper"}}
    return make_hub(plugins)


def test_rerank_routes_to_capable_provider(audio_hub) -> None:
    r = audio_hub.post("/llm/rerank", json={"query": "free cloud", "documents": ["paid plan", "free cloud tier",
                                                                                  "cloud"], "top_n": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["x-routed-via"] == "cohere" and body["routed_via"] == "cohere"
    assert [x["index"] for x in body["results"]] == [1, 2]
    assert body["model"] == "rerank-fake"


def test_rerank_model_pin_validation_and_unsupported(audio_hub) -> None:
    r = audio_hub.post("/llm/rerank", json={"query": "q", "documents": ["a"], "model": "rerank-v3.5"})
    assert r.status_code == 200 and r.json()["model"] == "rerank-v3.5"
    r = audio_hub.post("/llm/rerank", json={"query": "q", "documents": ["a"], "provider": "groq"})
    assert r.headers["x-routed-via"] == "groq:whisper"
    assert audio_hub.post("/llm/rerank", json={"query": "q", "documents": []}).status_code == 422
    assert audio_hub.post("/llm/rerank", json={"query": "q", "documents": ["a"], "top_n": 0}).status_code == 422
    assert audio_hub.post("/llm/rerank", json={"query": "q", "documents": ["a"], "provider": "nope"}).status_code == 404


def test_rerank_without_capable_provider_is_503(hub) -> None:
    r = hub.post("/llm/rerank", json={"query": "q", "documents": ["a"]})
    assert r.status_code == 503 and r.json()["error"] == "no_provider_available"


def test_transcribe_multipart(audio_hub) -> None:
    r = audio_hub.post(
        "/llm/transcribe",
        files={"file": ("memo.m4a", b"\x00" * 1234, "audio/mp4")},
        data={"language": "fr", "prompt": "OmniFree", "model": "whisper-large-v3"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"model": "whisper-large-v3", "text": "1234 bytes", "routed_via": "groq:whisper"}
    assert r.headers["x-routed-via"] == "groq:whisper"
    req = fr.TRANSCRIBED[-1]
    assert (req.filename, req.language, req.prompt, req.model) == ("memo.m4a", "fr", "OmniFree", "whisper-large-v3")


def test_transcribe_pinned_and_limits(audio_hub) -> None:
    r = audio_hub.post("/llm/transcribe", files={"file": ("a.wav", b"abc", "audio/wav")}, data={"provider": "cohere"})
    assert r.status_code == 200 and r.headers["x-routed-via"] == "cohere"
    big = audio_hub.post("/llm/transcribe", files={"file": ("a.wav", b"0" * 50_001, "audio/wav")})
    assert big.status_code == 413
    assert audio_hub.post("/llm/transcribe", files={"file": ("a.wav", b"", "audio/wav")}).status_code == 422
    assert audio_hub.post("/llm/transcribe", data={"model": "x"}).status_code == 422
    assert fr.TRANSCRIBED and all(len(t.audio) <= 50_000 for t in fr.TRANSCRIBED)


# ------------------------------------------------------------------ direct uploads


@pytest.fixture
def upload_hub(make_hub, plugins):
    plugins = {k: v for k, v in plugins.items() if v["kind"] != "storage"}
    plugins["plain"] = {"module": "tests.hub_api.fakes:FakeStorage", "kind": "storage", "group": "storage",
                        "priority": 5, "settings": {"capacity_bytes": 10_000_000}}
    plugins["direct_small"] = {"module": f"{F}:FakeDirectStorage", "kind": "storage", "group": "storage",
                               "priority": 10, "settings": {"capacity_bytes": 1000}}
    plugins["direct_big"] = {"module": f"{F}:FakeDirectStorage", "kind": "storage", "group": "storage",
                             "priority": 20, "settings": {"capacity_bytes": 100_000}}
    return make_hub(plugins)


def test_upload_url_picks_most_free_capable_backend(upload_hub) -> None:
    r = upload_hub.post("/drive/upload-url", json={"filename": "movie.mp4", "size": 20_000, "prefix": "videos/"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "provider": "direct_big", "key": "videos/movie.mp4", "method": "PUT",
        "url": "https://direct_big.test/videos/movie.mp4?sig=x", "headers": {"Content-Type": "video/mp4"},
        "expires_at": "2030-01-01T00:00:00Z",
    }
    assert fr.UPLOAD_TARGETS == [("direct_big", "videos/movie.mp4", "video/mp4", 20_000)]


def test_upload_url_pinned_key_and_content_type(upload_hub) -> None:
    r = upload_hub.post("/drive/upload-url", json={"filename": "x.bin", "key": "/raw//a b.dat",
                                                   "content_type": "application/x-test", "provider": "direct_small"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "direct_small" and r.json()["key"] == "raw/a b.dat"
    assert r.json()["headers"] == {"Content-Type": "application/x-test"}
    unknown_type = upload_hub.post("/drive/upload-url", json={"filename": "blob.zzz9"}).json()
    assert unknown_type["headers"]["Content-Type"] == "application/octet-stream"


def test_upload_url_errors(upload_hub) -> None:
    assert upload_hub.post("/drive/upload-url", json={"filename": "a", "provider": "plain"}).status_code == 501
    assert upload_hub.post("/drive/upload-url", json={"filename": "a", "provider": "nope"}).status_code == 404
    assert upload_hub.post("/drive/upload-url", json={"filename": "a", "provider": "llm_a"}).status_code == 400
    assert upload_hub.post("/drive/upload-url", json={"filename": "a", "key": "../x"}).status_code == 400
    assert upload_hub.post("/drive/upload-url", json={"filename": ""}).status_code == 422
    assert upload_hub.post("/drive/upload-url", json={"filename": "a", "size": -1}).status_code == 422
    too_big = upload_hub.post("/drive/upload-url", json={"filename": "a", "size": 10**9})
    assert too_big.status_code == 503 and too_big.json()["error"] == "no_provider_available"


def test_upload_url_fails_over_when_backend_down(make_hub, plugins) -> None:
    plugins["direct_big"] = {"module": f"{F}:FakeDirectStorage", "kind": "storage", "group": "storage",
                             "priority": 20, "settings": {"capacity_bytes": 100_000, "behaviour": "down"}}
    plugins["direct_small"] = {"module": f"{F}:FakeDirectStorage", "kind": "storage", "group": "storage",
                               "priority": 30, "settings": {"capacity_bytes": 1000}}
    hub = make_hub(plugins)
    r = hub.post("/drive/upload-url", json={"filename": "a.txt", "size": 10})
    assert r.status_code == 200 and r.json()["provider"] == "direct_small"
