"""/links, /drops and the PUBLIC /s/{code}, /d/{code} pages (fake D1, presign-capable fake storage)."""

from __future__ import annotations

import re

import pytest

from hub_api.routers.links import hash_password, verify_password
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa

BASE = "https://short.test"


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def app_hub(make_hub):
    hub = make_hub(fa.apps_plugins(), hub_overrides={"public_base_url": BASE + "/"})
    fakes.STORES["r2"]["share/report.pdf"] = (b"%PDF-1.7 hello", "application/pdf")
    fakes.STORES["plain"]["share/a.txt"] = (b"hi", "text/plain")
    return hub


def _public(hub, method: str, url: str, **kw):
    """Anonymous request (no bearer token), redirects not followed."""
    return hub.client.request(method, url, follow_redirects=False, **kw)


# ------------------------------------------------------------------ passwords


def test_password_hashing() -> None:
    stored = hash_password("s3cret")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert stored != hash_password("s3cret")  # salted
    assert verify_password("s3cret", stored)
    assert not verify_password("S3cret", stored)
    assert not verify_password("s3cret", None)
    assert not verify_password("s3cret", "garbage")
    assert not verify_password("s3cret", "pbkdf2$1$2$3$aa$bb")


# ------------------------------------------------------------------ links


def test_links_create_list_delete(app_hub) -> None:
    r = app_hub.post("/links", json={"url": "https://example.com/a?b=1", "title": "Example"})
    assert r.status_code == 201, r.text
    link = r.json()
    assert re.fullmatch(r"[0-9A-Za-z]{6}", link["code"])
    assert link["short_url"] == f"{BASE}/s/{link['code']}"
    assert (link["clicks"], link["expires_at"], link["title"]) == (0, None, "Example")

    custom = app_hub.post("/links", json={"url": "http://x.org", "code": "cv", "expires_in_days": 7}).json()
    assert custom["code"] == "cv" and custom["expires_at"] > custom["created_at"]
    assert app_hub.post("/links", json={"url": "http://y.org", "code": "cv"}).status_code == 409

    for bad in ({"url": "ftp://x.org"}, {"url": "javascript:alert(1)"}, {"url": "example.com"},
                {"url": "https://x.org", "code": "a b"}, {"url": "https://x.org", "code": "api"},
                {"url": "https://x.org", "expires_in_days": 0}):
        assert app_hub.post("/links", json=bad).status_code == 422, bad

    codes = [x["code"] for x in app_hub.get("/links").json()["links"]]
    assert set(codes) == {link["code"], "cv"}

    assert app_hub.delete("/links/cv").status_code == 204
    assert app_hub.delete("/links/cv").status_code == 404
    assert _public(app_hub, "GET", "/s/cv").status_code == 404


def test_public_short_link_redirect_and_counter(app_hub) -> None:
    code = app_hub.post("/links", json={"url": "https://example.com/target"}).json()["code"]
    for _ in range(2):
        r = _public(app_hub, "GET", f"/s/{code}")
        assert r.status_code == 302
        assert r.headers["location"] == "https://example.com/target"
    assert app_hub.get("/links").json()["links"][0]["clicks"] == 2

    r = _public(app_hub, "GET", "/s/unknown")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "<html" in r.text and "http" not in r.text.split("<body>", 1)[1]  # no external assets

    fa.sql("UPDATE app_links SET expires_at = '2020-01-01T00:00:00Z' WHERE code = ?", [code])
    assert _public(app_hub, "GET", f"/s/{code}").status_code == 404
    assert app_hub.get("/links").json()["links"][0]["clicks"] == 2


# ------------------------------------------------------------------ drops


def test_drop_create_validation(app_hub) -> None:
    r = app_hub.post("/drops", json={"provider": "r2", "key": "/share//report.pdf", "max_downloads": 3})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["key"] == "share/report.pdf" and d["filename"] == "report.pdf" and d["size"] == 14
    assert d["url"] == f"{BASE}/d/{d['code']}" and d["has_password"] is False
    assert (d["downloads"], d["max_downloads"]) == (0, 3)
    assert d["expires_at"] > d["created_at"]

    assert app_hub.post("/drops", json={"provider": "plain", "key": "share/a.txt"}).status_code == 400  # no presign
    assert app_hub.post("/drops", json={"provider": "nope", "key": "a"}).status_code == 404
    assert app_hub.post("/drops", json={"provider": "cloudflare_d1", "key": "a"}).status_code == 400
    assert app_hub.post("/drops", json={"provider": "r2", "key": "missing.bin"}).status_code == 404
    assert app_hub.post("/drops", json={"provider": "r2", "key": "../etc"}).status_code == 400
    assert app_hub.post("/drops", json={"provider": "r2", "key": "share/report.pdf", "expires_in_hours": 0}).status_code == 422

    assert [x["code"] for x in app_hub.get("/drops").json()["drops"]] == [d["code"]]
    assert app_hub.delete(f"/drops/{d['code']}").status_code == 204
    assert app_hub.delete(f"/drops/{d['code']}").status_code == 404


def test_public_drop_download_and_limits(app_hub) -> None:
    d = app_hub.post("/drops", json={"provider": "r2", "key": "share/report.pdf", "max_downloads": 2}).json()
    for _ in range(2):
        r = _public(app_hub, "GET", f"/d/{d['code']}")
        assert r.status_code == 302
        assert r.headers["location"] == "https://signed.example/r2/share/report.pdf?exp=600"
    assert fa.PRESIGN_LOG[-1] == ("r2", "share/report.pdf", 600, "GET")
    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert r.status_code == 410 and "download limit" in r.text
    assert app_hub.get("/drops").json()["drops"][0]["downloads"] == 2

    assert _public(app_hub, "GET", "/d/nothing").status_code == 404

    e = app_hub.post("/drops", json={"provider": "r2", "key": "share/report.pdf"}).json()
    fa.sql("UPDATE app_drops SET expires_at = '2020-01-01T00:00:00Z' WHERE code = ?", [e["code"]])
    r = _public(app_hub, "GET", f"/d/{e['code']}")
    assert r.status_code == 410 and "expired" in r.text


def test_public_drop_presign_failure_does_not_count(app_hub) -> None:
    d = app_hub.post("/drops", json={"provider": "r2", "key": "share/report.pdf", "max_downloads": 1}).json()
    fa.PRESIGN_FAIL.add("r2")
    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert r.status_code == 502 and r.headers["content-type"].startswith("text/html")
    fa.PRESIGN_FAIL.clear()
    assert _public(app_hub, "GET", f"/d/{d['code']}").status_code == 302


def test_public_drop_password_flow(app_hub) -> None:
    d = app_hub.post("/drops", json={"provider": "r2", "key": "share/report.pdf", "password": "open sesame"}).json()
    assert d["has_password"] is True
    stored = fa.sql("SELECT password_hash FROM app_drops WHERE code = ?", [d["code"]])[0][0]
    assert stored.startswith("scrypt$") and "open sesame" not in stored

    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert r.status_code == 200 and '<form method="post"' in r.text and 'name="password"' in r.text
    assert "report.pdf" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert fa.PRESIGN_LOG == []

    r = _public(app_hub, "POST", f"/d/{d['code']}", data={"password": "wrong"})
    assert r.status_code == 401 and "Wrong password" in r.text and "<form" in r.text
    r = _public(app_hub, "POST", f"/d/{d['code']}", data={})
    assert r.status_code == 401
    assert app_hub.get("/drops").json()["drops"][0]["downloads"] == 0

    r = _public(app_hub, "POST", f"/d/{d['code']}", data={"password": "open sesame"})
    assert r.status_code == 302 and r.headers["location"].startswith("https://signed.example/r2/")
    assert app_hub.get("/drops").json()["drops"][0]["downloads"] == 1
    assert _public(app_hub, "POST", "/d/unknown", data={"password": "x"}).status_code == 404


def test_html_is_escaped(app_hub) -> None:
    fakes.STORES["r2"]["share/<img src=x>.txt"] = (b"x", "text/plain")
    d = app_hub.post("/drops", json={"provider": "r2", "key": "share/<img src=x>.txt", "password": "p"}).json()
    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert "<img" not in r.text and "&lt;img src=x&gt;.txt" in r.text


# ------------------------------------------------------------------ authentication


PRIVATE = [
    ("GET", "/notes"), ("POST", "/notes"), ("GET", "/notes/abc"), ("PATCH", "/notes/abc"), ("DELETE", "/notes/abc"),
    ("POST", "/notes/voice"), ("GET", "/tasks"), ("POST", "/tasks"), ("PATCH", "/tasks/abc"),
    ("DELETE", "/tasks/abc"), ("GET", "/expenses"), ("POST", "/expenses"), ("POST", "/expenses/receipt"),
    ("PATCH", "/expenses/abc"), ("DELETE", "/expenses/abc"), ("GET", "/links"), ("POST", "/links"),
    ("DELETE", "/links/abc"), ("GET", "/drops"), ("POST", "/drops"), ("DELETE", "/drops/abc"),
]


@pytest.mark.parametrize(("method", "url"), PRIVATE)
def test_private_routes_require_token(app_hub, method, url) -> None:
    r = _public(app_hub, method, url)
    assert r.status_code == 401
    r = _public(app_hub, method, url, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_public_routes_work_without_any_hub_token(make_hub) -> None:
    hub = make_hub(fa.apps_plugins(), token=None)  # protected routes fail closed (503) ...
    assert _public(hub, "GET", "/links").status_code == 503
    fa.sql("CREATE TABLE IF NOT EXISTS app_links (code TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT,"
           " clicks INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, expires_at TEXT)")
    fa.sql("INSERT INTO app_links (code, url, clicks, created_at) VALUES ('pub', 'https://ok.example', 0, '2026-01-01T00:00:00Z')")
    r = _public(hub, "GET", "/s/pub")  # ... while the public redirect still answers
    assert r.status_code == 302 and r.headers["location"] == "https://ok.example"
    assert _public(hub, "GET", "/d/none").status_code == 404
