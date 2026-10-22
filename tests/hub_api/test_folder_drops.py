"""Folder drops: POST /drops/folder and the PUBLIC folder page, password, limits and ZIP download."""

from __future__ import annotations

import io
import re
import time
import zipfile

import pytest

from hub_api.routers import links
from hub_api.routers.links import zip_sig
from tests.hub_api import fakes
from tests.hub_api import fakes_apps as fa
from tests.hub_api.conftest import TOKEN

BASE = "https://short.test"


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def app_hub(make_hub):
    hub = make_hub(fa.apps_plugins(), hub_overrides={"public_base_url": BASE + "/"})
    fakes.STORES["r2"]["trip/photo 1.jpg"] = (b"JPEG-1", "image/jpeg")
    fakes.STORES["r2"]["trip/day2/notes.txt"] = (b"hello world", "text/plain")
    fakes.STORES["r2"]["tripx/other.txt"] = (b"not in the folder", "text/plain")
    fakes.STORES["plain"]["trip/plain.txt"] = (b"pl", "text/plain")
    return hub


def _public(hub, method: str, url: str, **kw):
    return hub.client.request(method, url, follow_redirects=False, **kw)


def _zip_href(page: str) -> str:
    m = re.search(r'href="(/d/[^"]+/zip\?t=[^"]+)"', page)
    assert m, page
    return m.group(1).replace("&amp;", "&")


def test_create_folder_drop(app_hub) -> None:
    r = app_hub.post("/drops/folder", json={"prefix": "/trip/", "provider": "r2", "max_downloads": 5, "password": "pw"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["kind"] == "folder" and d["key"] == "trip/" and d["filename"] == "trip"
    assert d["file_count"] == 2 and d["size"] == 6 + 11 and d["provider"] == "r2"
    assert d["url"] == f"{BASE}/d/{d['code']}" and d["has_password"] is True
    assert d["expires_at"] > d["created_at"]

    every = app_hub.post("/drops/folder", json={"prefix": "trip"}).json()  # every backend
    assert every["provider"] == "*" and every["file_count"] == 3

    listed = {x["code"]: x for x in app_hub.get("/drops").json()["drops"]}
    assert listed[d["code"]]["kind"] == "folder"
    file_drop = app_hub.post("/drops", json={"provider": "r2", "key": "trip/photo 1.jpg"}).json()
    assert file_drop["kind"] == "file" and file_drop["file_count"] is None

    assert app_hub.post("/drops/folder", json={"prefix": "empty"}).status_code == 404
    assert app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "plain"}).status_code == 400
    assert app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "nope"}).status_code == 404
    assert app_hub.post("/drops/folder", json={"prefix": "../etc"}).status_code == 400
    assert app_hub.post("/drops/folder", json={"prefix": "/"}).status_code == 400
    assert app_hub.post("/drops/folder", json={"prefix": "trip", "expires_in_hours": 0}).status_code == 422


def test_public_folder_page_lists_files_with_presigned_links(app_hub) -> None:
    d = app_hub.post("/drops/folder", json={"prefix": "trip"}).json()
    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<h1>trip</h1>" in page and "3 files" in page
    assert 'href="https://signed.example/r2/trip/photo 1.jpg?exp=600"' in page
    assert 'href="https://signed.example/r2/trip/day2/notes.txt?exp=600"' in page
    assert "plain.txt" in page and "signed.example/plain" not in page  # listed, no presign on that backend
    assert "tripx" not in page and "Download all as ZIP" in page
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert app_hub.get("/drops").json()["drops"][0]["downloads"] == 1


def test_folder_password_and_limits(app_hub) -> None:
    d = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2", "password": "s3", "max_downloads": 1}).json()
    r = _public(app_hub, "GET", f"/d/{d['code']}")
    assert r.status_code == 200 and "Protected folder" in r.text and "<form" in r.text and "photo" not in r.text
    assert fa.PRESIGN_LOG == []
    r = _public(app_hub, "POST", f"/d/{d['code']}", data={"password": "nope"})
    assert r.status_code == 401 and "Wrong password" in r.text
    r = _public(app_hub, "POST", f"/d/{d['code']}", data={"password": "s3"})
    assert r.status_code == 200 and "photo 1.jpg" in r.text
    zip_url = _zip_href(r.text)
    r = _public(app_hub, "POST", f"/d/{d['code']}", data={"password": "s3"})
    assert r.status_code == 410 and "download limit" in r.text
    # The ZIP link handed out with the page still works within its 10 minutes.
    assert _public(app_hub, "GET", zip_url).status_code == 200

    e = app_hub.post("/drops/folder", json={"prefix": "trip"}).json()
    fa.sql("UPDATE app_drops SET expires_at = '2020-01-01T00:00:00Z' WHERE code = ?", [e["code"]])
    r = _public(app_hub, "GET", f"/d/{e['code']}")
    assert r.status_code == 410 and "expired" in r.text


def test_folder_zip_inline(app_hub) -> None:
    d = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()
    page = _public(app_hub, "GET", f"/d/{d['code']}").text
    r = _public(app_hub, "GET", _zip_href(page))
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "trip.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["day2/notes.txt", "photo 1.jpg"]
        assert zf.read("day2/notes.txt") == b"hello world"


def test_folder_zip_signature_and_expiry(app_hub) -> None:
    d = app_hub.post("/drops/folder", json={"prefix": "trip"}).json()
    code = d["code"]
    exp = int(time.time()) + 300
    assert _public(app_hub, "GET", f"/d/{code}/zip?t={exp}.{zip_sig(TOKEN, code, exp)}").status_code == 200
    assert _public(app_hub, "GET", f"/d/{code}/zip?t={exp}.{zip_sig('other', code, exp)}").status_code == 404
    assert _public(app_hub, "GET", f"/d/{code}/zip?t={exp + 1}.{zip_sig(TOKEN, code, exp)}").status_code == 404
    assert _public(app_hub, "GET", f"/d/{code}/zip").status_code == 404
    old = int(time.time()) - 5
    assert _public(app_hub, "GET", f"/d/{code}/zip?t={old}.{zip_sig(TOKEN, code, old)}").status_code == 410
    # A file drop has no ZIP.
    f = app_hub.post("/drops", json={"provider": "r2", "key": "trip/photo 1.jpg"}).json()["code"]
    assert _public(app_hub, "GET", f"/d/{f}/zip?t={exp}.{zip_sig(TOKEN, f, exp)}").status_code == 404


def test_big_folder_zip_goes_through_storage(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "ZIP_INLINE_MAX", 10)
    d = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()
    exp = int(time.time()) + 300
    r = _public(app_hub, "GET", f"/d/{d['code']}/zip?t={exp}.{zip_sig(TOKEN, d['code'], exp)}")
    assert r.status_code == 302
    [key] = [k for k in fakes.STORES["r2"] if k.startswith(f"_shares/zips/{d['code']}-")]
    assert r.headers["location"] == f"https://signed.example/r2/{key}?exp=600"
    # The stored ZIP never shows in the folder page, and goes away with the drop.
    assert app_hub.delete(f"/drops/{d['code']}").status_code == 204
    assert key not in fakes.STORES["r2"]


def test_big_folder_zip_is_built_once(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "ZIP_INLINE_MAX", 10)
    d = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()
    exp = int(time.time()) + 300
    url = f"/d/{d['code']}/zip?t={exp}.{zip_sig(TOKEN, d['code'], exp)}"
    built = []
    real = links._build_zip

    async def counting(*a, **kw):
        built.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(links, "_build_zip", counting)
    first = _public(app_hub, "GET", url)
    again = _public(app_hub, "GET", url)
    assert first.status_code == again.status_code == 302 and first.headers["location"] == again.headers["location"]
    assert len(built) == 1  # replaying the link does not rebuild nor re-upload
    # A shared file changed: a new ZIP replaces the old one.
    fakes.STORES["r2"]["trip/photo 1.jpg"] = (b"JPEG-1-edited", "image/jpeg")
    third = _public(app_hub, "GET", url)
    assert third.headers["location"] != first.headers["location"] and len(built) == 2
    assert len([k for k in fakes.STORES["r2"] if k.startswith("_shares/zips/")]) == 1


def test_stored_zips_of_ended_drops_are_swept(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "ZIP_INLINE_MAX", 10)
    codes = [app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()["code"] for _ in range(3)]
    exp = int(time.time()) + 300
    for c in codes:
        assert _public(app_hub, "GET", f"/d/{c}/zip?t={exp}.{zip_sig(TOKEN, c, exp)}").status_code == 302
    expired, used_up, live = codes
    fa.sql("UPDATE app_drops SET expires_at = '2020-01-01T00:00:00Z' WHERE code = ?", [expired])
    fa.sql("UPDATE app_drops SET max_downloads = 1, downloads = 1 WHERE code = ?", [used_up])
    fakes.STORES["r2"]["_shares/zips/GoneDrop-0123456789abcdef.zip"] = (b"PK", "application/zip")  # drop deleted
    assert app_hub.get("/drops").status_code == 200
    left = sorted(k for k in fakes.STORES["r2"] if k.startswith("_shares/zips/"))
    assert len(left) == 1 and left[0].startswith(f"_shares/zips/{live}-")


def test_too_big_folder_hides_zip(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "ZIP_MAX_BYTES", 10)
    d = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()
    page = _public(app_hub, "GET", f"/d/{d['code']}").text
    assert "Download all as ZIP" not in page and "photo 1.jpg" in page
    exp = int(time.time()) + 300
    assert _public(app_hub, "GET", f"/d/{d['code']}/zip?t={exp}.{zip_sig(TOKEN, d['code'], exp)}").status_code == 413


def test_folder_page_escapes_names(app_hub) -> None:
    fakes.STORES["r2"]["<b>/x<script>.txt"] = (b"x", "text/plain")
    d = app_hub.post("/drops/folder", json={"prefix": "<b>", "provider": "r2"}).json()
    page = _public(app_hub, "GET", f"/d/{d['code']}").text
    assert "<script>" not in page and "&lt;script&gt;" in page and "<h1>&lt;b&gt;</h1>" in page


def test_folder_route_requires_token(app_hub) -> None:
    assert _public(app_hub, "POST", "/drops/folder", json={"prefix": "trip"}).status_code == 401


def test_folder_drop_shares_only_the_files_present_at_creation(app_hub) -> None:
    d = app_hub.post("/drops/folder", json={"prefix": "trip"}).json()
    assert d["file_count"] == 3
    # Added after sharing, on the same backend and on another one: never published.
    fakes.STORES["r2"]["trip/later.txt"] = (b"private", "text/plain")
    fakes.STORES["plain"]["trip/day2/later2.txt"] = (b"private", "text/plain")
    # The same key showing up on an earlier backend does not replace the shared copy either.
    fakes.STORES["plain"]["trip/photo 1.jpg"] = (b"other", "image/jpeg")
    del fakes.STORES["r2"]["trip/day2/notes.txt"]  # gone since: no longer listed
    page = _public(app_hub, "GET", f"/d/{d['code']}").text
    assert "later" not in page and "notes.txt" not in page
    assert 'href="https://signed.example/r2/trip/photo 1.jpg?exp=600"' in page and "plain.txt" in page
    assert "2 files" in page

    r2 = app_hub.post("/drops/folder", json={"prefix": "trip", "provider": "r2"}).json()
    fakes.STORES["r2"]["trip/later3.txt"] = (b"private", "text/plain")
    page = _public(app_hub, "GET", f"/d/{r2['code']}").text
    r = _public(app_hub, "GET", _zip_href(page))
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["later.txt", "photo 1.jpg"]

    # A row without a snapshot shares nothing.
    fa.sql("UPDATE app_drops SET file_keys = NULL WHERE code = ?", [d["code"]])
    page = _public(app_hub, "GET", f"/d/{d['code']}").text
    assert "photo" not in page and "no longer in this folder" in page and "not listed" not in page
