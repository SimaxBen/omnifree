"""/push and the web push channel of /notify: VAPID config, subscriptions, test sends (pywebpush mocked), the
fan-out of notify kinds to ntfy + web push with per-channel results, 404/410 cleanup and the channel switches."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any

import httpx
import pytest
import pywebpush
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from automation import vapid
from hub_api import notifier, webpush
from hub_api.appdata import local_tz
from hub_api.routers import expenses as exp_mod
from hub_api.routers import notes as notes_mod
from hub_api.routers import notify as notify_mod
from hub_api.routers.push import browser_of, endpoint_id
from tests.hub_api import fakes_apps as fa
from tests.hub_api.conftest import AUTH

TODAY = date(2026, 10, 21)
TOPIC = "omnifree-test-topic-x9k2"
PUBLIC, PRIVATE = vapid.generate()
UA_S25 = ("Mozilla/5.0 (Linux; Android 15; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/141.0.0.0 Mobile Safari/537.36")


def _sub(n: int) -> dict[str, Any]:
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/device-{n}", "expirationTime": None,
            "keys": {"p256dh": "BOr" + "x" * 84, "auth": "auth-secret-" + str(n)}}


class PushLog(list):
    """The recorded calls, plus ``answers[endpoint] = status`` to make an endpoint fail."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, int] = {}


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.reason = "Gone" if status == 410 else "Error"
        self.text = ""


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> PushLog:
    """Records every pywebpush.webpush call; ``answers[endpoint] = status`` makes that endpoint fail."""
    fa.reset()
    for mod in (exp_mod, notes_mod, notify_mod):
        monkeypatch.setattr(mod, "local_today", lambda: TODAY)
    for key in ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN", "VAPID_SUBJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", PRIVATE)
    log = PushLog()
    answers = log.answers

    def fake_webpush(**kwargs: Any) -> FakeResponse:
        log.append(kwargs)
        status = answers.get(kwargs["subscription_info"]["endpoint"], 201)
        if status > 202:
            raise pywebpush.WebPushException(f"Push failed: {status}", response=FakeResponse(status))
        return FakeResponse(status)

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    return log


@pytest.fixture
def ntfy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": f"msg{len(log)}"})

    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(handler))
    return log


@pytest.fixture
def hub(make_hub, pushes):
    return make_hub(fa.apps_plugins())


def _subscribe(hub, n: int, label: str | None = None, ua: str = UA_S25, browser: str | None = None) -> dict[str, Any]:
    body = {**_sub(n), **({"label": label} if label else {}), **({"browser": browser} if browser else {})}
    r = hub.post("/push/subscribe", json=body, headers={"User-Agent": ua})
    assert r.status_code == 200, r.text
    return r.json()


def _unsubscribe(hub, body: dict[str, Any]) -> httpx.Response:
    return hub.client.request("DELETE", "/push/subscribe", json=body, headers=AUTH)


# --------------------------------------------------------------------------- pure helpers


def test_keys_and_payload() -> None:
    assert len(PUBLIC) == 87 and PUBLIC.startswith("B") and len(PRIVATE) == 43
    raw_pub = base64.urlsafe_b64decode(PUBLIC + "=")
    assert len(raw_pub) == 65 and raw_pub[0] == 4
    key = webpush.parse_private_key(PRIVATE)
    assert webpush.public_key_of(key) == PUBLIC
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    assert webpush.public_key_of(webpush.parse_private_key(pem)) == PUBLIC  # PEM accepted too

    env = {"VAPID_PRIVATE_KEY": PRIVATE}
    cfg = webpush.load_config(env.get)
    assert cfg.configured and cfg.public_key == PUBLIC and cfg.subject == "https://omnifree.ilyasouhnine.tech"
    other = webpush.public_key_of(ec.generate_private_key(ec.SECP256R1()))
    bad = webpush.load_config({"VAPID_PRIVATE_KEY": PRIVATE, "VAPID_PUBLIC_KEY": other}.get)
    assert not bad.configured and "does not match" in (bad.error or "")
    assert not webpush.load_config({"VAPID_PRIVATE_KEY": "nope"}.get).configured
    assert webpush.load_config({"VAPID_PRIVATE_KEY": PRIVATE, "VAPID_SUBJECT": "me@x.org"}.get).subject == "mailto:me@x.org"

    text = webpush.build_payload("Titre", "é" * 5000, tag="t", url="https://p/#/x",
                                 actions=[{"action": "done", "title": "Fait ✓", "url": "https://h/notify/done/1?sig=a"}])
    data = json.loads(text)
    assert len(text.encode("utf-8")) <= webpush.MAX_PAYLOAD_BYTES and data["body"].endswith("…")
    assert data["actions"][0]["action"] == "done" and data["url"] == "https://p/#/x" and data["tag"] == "t"

    assert browser_of(UA_S25) == "Chrome (Android)"
    assert browser_of(UA_S25.replace("Chrome/141", "SamsungBrowser/28.0 Chrome/141")) == "Samsung Internet (Android)"
    assert browser_of(UA_S25, "Brave (Android)") == "Brave (Android)"
    assert browser_of("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit Version/18.0 Mobile Safari/604.1") == "Safari (iOS)"


# --------------------------------------------------------------------------- /push endpoints


def test_config_without_keys_skips_gracefully(make_hub, pushes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAPID_PUBLIC_KEY")
    monkeypatch.delenv("VAPID_PRIVATE_KEY")
    hub = make_hub(fa.apps_plugins())
    assert hub.get("/push/config").json() == {"configured": False, "public_key": None,
                                               "error": "vault key VAPID_PRIVATE_KEY is not set"}
    _subscribe(hub, 1)  # the subscription is still stored (the keys may come later)
    r = hub.post("/push/test")
    assert r.status_code == 409 and "VAPID_PRIVATE_KEY" in r.json()["detail"]
    out = hub.post("/notify/run?kind=evening").json()  # no ntfy, no web push: skipped, no error
    assert out["sent"] is False and "app notifications not configured" in out["skipped"]
    assert "ntfy not configured" in out["skipped"] and not pushes
    assert hub.post("/notify/test").status_code == 409


def test_subscribe_list_unsubscribe(hub, pushes) -> None:
    assert hub.get("/push/config").json() == {"configured": True, "public_key": PUBLIC, "error": None}
    first = _subscribe(hub, 1, label="S25 Brave", browser="Brave (Android)")
    assert first["created"] is True and first["subscription"]["id"] == endpoint_id(_sub(1)["endpoint"])
    again = _subscribe(hub, 1)  # idempotent: same id, the label is kept
    assert again["created"] is False and again["subscription"]["label"] == "S25 Brave"
    _subscribe(hub, 2, label="Laptop", ua="Mozilla/5.0 (Windows NT 10.0) Gecko/20100101 Firefox/131.0")

    listing = hub.get("/push/subscriptions").json()
    subs = listing["subscriptions"]
    assert listing["configured"] is True and [s["label"] for s in subs] == ["S25 Brave", "Laptop"]
    assert subs[0]["browser"] == "Chrome (Android)" and subs[1]["browser"] == "Firefox (Windows)"
    assert subs[0]["push_service"] == "fcm.googleapis.com"
    assert all("endpoint" not in s and "p256dh" not in s and "auth" not in s for s in subs)  # never the keys

    bad = hub.post("/push/subscribe", json={**_sub(3), "endpoint": "http://insecure.example/x"})
    assert bad.status_code == 422
    assert _unsubscribe(hub, {"endpoint": _sub(1)["endpoint"]}).json()["removed"] is True
    assert _unsubscribe(hub, {"id": subs[1]["id"]}).json()["removed"] is True
    assert _unsubscribe(hub, {}).status_code == 422
    assert hub.get("/push/subscriptions").json()["subscriptions"] == []


def test_push_test_per_device_and_410_cleanup(hub, pushes) -> None:
    _subscribe(hub, 1, label="S25 Chrome")
    _subscribe(hub, 2, label="Old phone")
    _subscribe(hub, 3, label="Flaky")
    pushes.answers[_sub(2)["endpoint"]] = 410
    pushes.answers[_sub(3)["endpoint"]] = 500

    out = hub.post("/push/test").json()
    assert (out["sent"], out["failed"], out["removed"]) == (1, 2, 1)
    by_label = {r["label"]: r for r in out["results"]}
    assert by_label["S25 Chrome"]["ok"] is True and by_label["Old phone"]["removed"] is True
    assert by_label["Flaky"]["status"] == 500 and "500" in by_label["Flaky"]["error"]
    call = pushes[0]
    payload = json.loads(call["data"])
    assert payload["title"] == "OmniFree : test réussi ✓" and payload["url"].endswith("/#/more/notifications")
    assert call["vapid_claims"] == {"sub": "https://omnifree.ilyasouhnine.tech"} and call["headers"]["Urgency"] == "high"
    assert call["subscription_info"]["keys"]["auth"] == "auth-secret-1"

    subs = {s["label"]: s for s in hub.get("/push/subscriptions").json()["subscriptions"]}
    assert set(subs) == {"S25 Chrome", "Flaky"}  # the 410 one is gone
    assert subs["S25 Chrome"]["last_success_at"] and subs["Flaky"]["last_failure_at"] and subs["Flaky"]["last_error"]

    pushes.clear()
    one = hub.post("/push/test", json={"endpoint": _sub(1)["endpoint"]}).json()
    assert one["sent"] == 1 and len(pushes) == 1
    assert hub.post("/push/test", json={"endpoint": _sub(9)["endpoint"]}).status_code == 404


# --------------------------------------------------------------------------- fan-out from /notify


def test_fan_out_to_both_channels_with_per_channel_results(hub, pushes, ntfy) -> None:
    _subscribe(hub, 1, label="S25")
    _subscribe(hub, 2, label="Gone")
    pushes.answers[_sub(2)["endpoint"]] = 404
    out = hub.post("/notify/run?kind=evening").json()
    assert out["sent"] is True and len(ntfy) == 1 and len(pushes) == 2
    assert out["channels"]["ntfy"] == {"ok": True, "id": "msg1"}
    assert out["channels"]["webpush"] == {"ok": True, "sent": 1, "failed": 1, "removed": 1,
                                          "errors": ["Gone: Push failed: 404"]}
    payload = json.loads(pushes[0]["data"])
    assert payload["title"] == ntfy[0]["title"] == "Ce soir : les dépenses du jour ?"
    assert payload["url"] == "https://omnifree.ilyasouhnine.tech/#/more/expenses"
    assert payload["tag"].startswith("omnifree-evening-") and pushes[0]["ttl"] == 12 * 3600
    assert pushes[0]["headers"]["Urgency"] == "normal"
    recent = hub.get("/notify/status").json()["recent"][0]
    assert recent["status"] == "sent" and recent["channels"]["webpush"]["removed"] == 1
    assert len(hub.get("/push/subscriptions").json()["subscriptions"]) == 1  # 404 cleaned up

    # The ntfy test goes to the app too.
    pushes.clear()
    assert hub.post("/notify/test").json()["channels"]["webpush"]["sent"] == 1 and len(pushes) == 1


def test_one_channel_failing_still_counts_as_sent(hub, pushes, ntfy, monkeypatch: pytest.MonkeyPatch) -> None:
    _subscribe(hub, 1)
    monkeypatch.setattr(notifier, "transport", httpx.MockTransport(lambda req: httpx.Response(500, text="down")))
    out = hub.post("/notify/run?kind=evening")
    assert out.status_code == 200 and out.json()["sent"] is True
    assert out.json()["channels"]["ntfy"]["ok"] is False and out.json()["channels"]["webpush"]["ok"] is True
    pushes.answers[_sub(1)["endpoint"]] = 500
    r = hub.post("/notify/run?kind=evening&force=1")  # both fail: 502 so QStash retries
    assert r.status_code == 502 and "ntfy answered 500" in r.json()["detail"] and "web push" in r.json()["detail"]
    assert hub.get("/notify/status").json()["recent"][0]["status"] == "failed"


def test_channel_prefs(hub, pushes, ntfy) -> None:
    _subscribe(hub, 1)
    st = hub.get("/notify/status").json()
    assert st["channels"] == {"ntfy": True, "webpush": True}
    assert st["webpush"] == {"configured": True, "error": None, "devices": 1}

    r = hub.put("/notify/prefs", json={"channels": {"webpush": False}})
    assert r.json()["channels"] == {"ntfy": True, "webpush": False} and r.json()["prefs"]["morning"] is True
    assert hub.put("/notify/prefs", json={"channels": {"sms": True}}).status_code == 422
    out = hub.post("/notify/run?kind=evening").json()
    assert out["sent"] is True and set(out["channels"]) == {"ntfy"} and not pushes

    hub.put("/notify/prefs", json={"channels": {"webpush": True, "ntfy": False}})
    out = hub.post("/notify/run?kind=evening&force=1").json()
    assert set(out["channels"]) == {"webpush"} and len(ntfy) == 1 and len(pushes) == 1

    hub.put("/notify/prefs", json={"channels": {"webpush": False}})
    out = hub.post("/notify/run?kind=evening&force=1").json()
    assert out["sent"] is False and "ntfy turned off" in out["skipped"] and "app notifications turned off" in out["skipped"]


def test_reminder_web_push_has_done_action_and_high_urgency(hub, pushes, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 21, 14, 50, tzinfo=local_tz())
    monkeypatch.setattr(notify_mod, "local_now", lambda: now)
    monkeypatch.setattr(notes_mod, "local_now", lambda: now)
    _subscribe(hub, 1)
    task = hub.post("/tasks", json={"title": "Appeler Karim", "due": "2026-10-21", "due_time": "15:00"}).json()
    out = hub.post("/notify/run?kind=reminders").json()
    assert out["sent"] is True and len(pushes) == 1  # web push only (no ntfy topic)
    payload = json.loads(pushes[0]["data"])
    assert pushes[0]["headers"]["Urgency"] == "high"
    done = payload["actions"][0]
    assert done["action"] == "done" and done["title"] == "Fait ✓"
    assert f"/notify/done/{task['id']}?sig=" in done["url"]
    assert payload["actions"][1] == {"action": "open", "title": "Tâches",
                                     "url": "https://omnifree.ilyasouhnine.tech/#/notes/tasks"}
    assert hub.post(done["url"].split("ilyasouhnine.tech", 1)[1]).json()["done"] is True


def test_vapid_script_prints_the_vault_formats(capsys: pytest.CaptureFixture[str]) -> None:
    assert vapid.main([]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [ln.split("=", 1)[0] for ln in lines] == ["VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]
    pub, priv = (ln.split("=", 1)[1] for ln in lines)
    assert webpush.public_key_of(webpush.parse_private_key(priv)) == pub
    vapid.main(["--only", "public"])
    assert len(capsys.readouterr().out.strip()) == 87
