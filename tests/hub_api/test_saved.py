"""The Saved library: canonical URLs, SSRF guard, OG parsing, oEmbed, dedupe, LLM fallback, collections, hybrid
search, ask with citations, global search and MCP (every HTTP call goes to an httpx.MockTransport)."""

from __future__ import annotations

import struct
import zlib
from datetime import timedelta
from typing import Any

import httpx
import pytest

from core.interfaces import PluginContext, ProviderKind
from hub_api.appdata import AppDB, local_today
from hub_api.routers import saved
from hub_api.saved_enrich import (
    MAX_PAGE_BYTES,
    BlockedURL,
    SafeFetcher,
    canonical_url,
    check_url,
    clean_annotation,
    guess_type,
    heuristic_annotation,
    is_public_ip,
    parse_meta,
    readable_text,
    site_of,
    youtube_id,
)
from tests.hub_api import fakes, fakes_rag
from tests.hub_api import fakes_apps as fa
from tests.hub_api import fakes_saved as fs
from tests.hub_api.test_mcp import MCP

S = "tests.hub_api.fakes_saved"
A = "tests.hub_api.fakes_apps"
R = "tests.hub_api.fakes_rag"

YT_ID = "abcdefghijk"


def saved_plugins(*, turso: bool = True) -> dict[str, Any]:
    plugins: dict[str, Any] = {
        "llm": {"module": f"{S}:SavedLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
                "settings": {"default_model": "fake-chat", "embedding_models": ["fake-embed"]}},
        "r2": {"module": f"{A}:FakePresignStorage", "kind": "storage", "group": "storage", "priority": 10,
               "settings": {"capacity_bytes": 10_000_000}},
        "cloudflare_d1": {"module": f"{A}:FakeD1", "kind": "db", "group": "cloudflare", "priority": 10},
    }
    if turso:
        plugins["turso"] = {"module": f"{R}:FakeTurso", "kind": "db", "group": "databases", "priority": 20}
    return plugins


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    fa.reset()
    fakes_rag.reset()
    fs.reset()
    monkeypatch.setattr(saved.FETCH, "transport", httpx.MockTransport(fs.handler))
    monkeypatch.setattr(saved.FETCH, "resolver", fs.resolve)
    saved._PREVIEWS.clear()
    saved._THUMB_BACKEND.clear()


@pytest.fixture
def hub(make_hub):
    return make_hub(saved_plugins())


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x80\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


ARTICLE_HTML = """<!doctype html><html lang="en"><head><title>Ignored &lt;title&gt;</title>
<meta property="og:title" content="Engineering managers &amp; the art of focus">
<meta property="og:description" content="How a manager keeps an engineering team productive.">
<meta property="og:image" content="/img/cover.png">
<meta property="og:site_name" content="LeadDev">
<meta property="og:type" content="article">
<meta name="twitter:card" content="summary_large_image">
<meta property="article:published_time" content="2026-09-30T08:00:00Z">
<link rel="canonical" href="https://leaddev.test/focus">
<link rel="icon" href="/favicon.png">
<script type="application/ld+json">{"@type": "Article", "author": {"name": "Jane Doe"}}</script>
</head><body><nav>Home Menu Login Subscribe</nav><script>var tracking = 1;</script>
<article><h1>Engineering managers</h1>
<p>Engineering managers protect deep work. """ + "They plan the week around focus blocks and fewer meetings. " * 6 + """</p>
</article><footer>Copyright LeadDev</footer></body></html>"""

YT_PAGE = f"""<html><head><meta property="og:title" content="Retro football anthem (1998) - YouTube">
<meta property="og:description" content="The 1998 stadium anthem, remastered.">
<meta property="og:type" content="video.other"></head><body>video {YT_ID}</body></html>"""

PLAY_HTML = """<html><head><meta property="og:title" content="StepUp: pedometer - Apps on Google Play">
<meta property="og:description" content="Count your steps every day and reach your goal.">
<meta property="og:image" content="https://play-lh.test/icon.png"></head><body><main>Count steps.</main></body></html>"""


def web_article() -> None:
    fs.WEB["https://leaddev.test/focus"] = fs.html(ARTICLE_HTML)
    fs.WEB["https://www.leaddev.test/focus"] = fs.html(ARTICLE_HTML)
    fs.WEB["https://leaddev.test/img/cover.png"] = fs.image(png())


def web_youtube() -> None:
    fs.WEB["https://www.youtube.com/oembed"] = fs.json_response({
        "title": "Retro football anthem (1998)", "author_name": "GoalsTV",
        "thumbnail_url": f"https://i.ytimg.com/vi/{YT_ID}/hqdefault.jpg"})
    fs.WEB[f"https://youtu.be/{YT_ID}"] = fs.redirect(f"https://www.youtube.com/watch?v={YT_ID}")
    fs.WEB["https://www.youtube.com/watch"] = fs.html(YT_PAGE)
    fs.WEB["https://i.ytimg.com/"] = fs.image(png(), "image/jpeg")


def web_play() -> None:
    fs.WEB["https://play.google.com/store/apps/details"] = fs.html(PLAY_HTML)
    fs.WEB["https://play-lh.test/icon.png"] = fs.image(png())


def save(hub, expect: int = 201, **body: Any) -> dict[str, Any]:
    r = hub.post("/saved", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def save_three(hub) -> dict[str, str]:
    web_youtube()
    web_article()
    web_play()
    fs.ANNOTATION.update({"summary": "A 1998 stadium anthem video.", "tags": ["anthem", "1998", "stadium"],
                          "type": "video", "collection": "Music", "new_collection": True})
    yt = save(hub, url=f"https://youtu.be/{YT_ID}?si=SHARE123")["item"]
    fs.ANNOTATION.clear()
    fs.ANNOTATION.update({"summary": "How engineering managers protect focus time.", "tags": ["management", "focus"],
                          "type": "article", "collection": "Productivity", "new_collection": True})
    art = save(hub, url="https://leaddev.test/focus?utm_source=newsletter")["item"]
    fs.ANNOTATION.clear()
    fs.ANNOTATION.update({"summary": "A pedometer app that counts your steps.", "tags": ["health", "steps"],
                          "type": "app", "collection": "Health", "new_collection": True})
    app = save(hub, url="https://play.google.com/store/apps/details?id=com.stepup&hl=fr")["item"]
    return {"yt": yt["id"], "art": art["id"], "app": app["id"]}


# --------------------------------------------------------------------------- pure helpers


def test_canonical_url_and_site() -> None:
    assert canonical_url("https://www.Example.com/a/?utm_source=x&b=2&fbclid=y&a=1#frag") == "https://example.com/a?a=1&b=2"
    assert canonical_url("http://example.com:80/") == "https://example.com/"
    assert canonical_url(f"https://youtu.be/{YT_ID}?si=abc") == f"https://youtube.com/watch?v={YT_ID}"
    assert canonical_url(f"https://m.youtube.com/shorts/{YT_ID}?feature=share") == f"https://youtube.com/watch?v={YT_ID}"
    assert canonical_url(f"https://www.youtube.com/watch?v={YT_ID}&t=42s&ab_channel=X") == f"https://youtube.com/watch?v={YT_ID}"
    assert canonical_url("https://twitter.com/Jack/status/20?s=20&t=abc") == "https://x.com/jack/status/20"
    assert canonical_url("https://mobile.x.com/jack/status/20") == "https://x.com/jack/status/20"
    assert canonical_url("https://play.google.com/store/apps/details?id=com.app&hl=fr&gl=MA") == \
        "https://play.google.com/store/apps/details?id=com.app"
    assert canonical_url("https://shop.test/p?gclid=1&mc_cid=2&color=red") == "https://shop.test/p?color=red"
    for bad in ("ftp://x.org/f", "javascript:alert(1)", "file:///etc/passwd", "example.com", ""):
        with pytest.raises(BlockedURL):
            canonical_url(bad)
    assert youtube_id("https://youtube.com/embed/abcdefghijk") == YT_ID and youtube_id("https://vimeo.com/1") is None
    assert site_of("https://www.youtube.com/watch?v=1") == "youtube.com" and site_of("https://youtu.be/x") == "youtube.com"
    assert site_of("https://twitter.com/a") == "x.com" and site_of(None) == ""


def test_parse_meta_and_readable_text() -> None:
    meta = parse_meta(ARTICLE_HTML, "https://leaddev.test/focus?x=1")
    assert meta.title == "Engineering managers & the art of focus"
    assert meta.description == "How a manager keeps an engineering team productive."
    assert meta.image == "https://leaddev.test/img/cover.png"
    assert meta.favicon == "https://leaddev.test/favicon.png"
    assert meta.canonical == "https://leaddev.test/focus"
    assert meta.site_name == "LeadDev" and meta.og_type == "article" and meta.lang == "en"
    assert meta.published == "2026-09-30T08:00:00Z" and meta.author == "Jane Doe"
    text = readable_text(ARTICLE_HTML)
    assert text.startswith("Engineering managers") and "focus blocks" in text
    assert "Menu Login" not in text and "tracking" not in text and "Copyright" not in text
    bare = parse_meta("<html><head><title> Plain  page </title></head></html>", "https://plain.test/a/b")
    assert bare.title == "Plain page" and bare.favicon == "https://plain.test/favicon.ico" and bare.image is None


def test_type_guess_and_annotation_cleanup() -> None:
    assert guess_type(site="youtube.com") == "video" and guess_type(site="x.com") == "social post"
    assert guess_type(site="play.google.com") == "app" and guess_type(mime="application/pdf", kind="file") == "doc"
    assert guess_type(site="blog.test", og_type="article") == "article"
    assert guess_type(site="food.test", title="Recette du tajine") == "recipe"
    cols = [{"id": "c1", "name": "Productivity"}]
    fallback = heuristic_annotation(title="Focus tips for productivity", description="Deep work. More text.", text="",
                                    site="blog.test", ctype="article", collections=cols)
    assert fallback["collection_id"] == "c1" and fallback["source"] == "heuristic" and fallback["tags"]
    cleaned = clean_annotation({"summary": " Un  résumé. ", "tags": ["#Focus", "focus", "Work", 3], "type": "VIDEO",
                                "collection": "productivity"}, fallback=fallback, collections=cols)
    assert cleaned["summary"] == "Un résumé." and cleaned["tags"] == ["focus", "work"] and cleaned["type"] == "video"
    assert (cleaned["collection"], cleaned["collection_id"], cleaned["new_collection"]) == ("Productivity", "c1", False)
    assert clean_annotation(None, fallback=fallback, collections=cols) is fallback
    assert clean_annotation({"type": "weird", "collection": "Recipes"}, fallback=fallback, collections=cols)["new_collection"]


# --------------------------------------------------------------------------- SSRF


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://example.com/", "http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://100.64.0.1/", "http://0.0.0.0/", "http://localhost:8080/", "http://router.local/", "http://intranet.test/",
    "http://metadata.test/", "http://v6local.test/", "http://nxdomain.test/", "http:///nohost",
])
async def test_ssrf_refusals(url: str) -> None:
    with pytest.raises(BlockedURL):
        await check_url(url, fs.resolve)


async def test_ssrf_public_and_redirects() -> None:
    assert await check_url("https://example.com/a", fs.resolve) == "https://example.com/a"
    assert is_public_ip("8.8.8.8") and not is_public_ip("fe80::1") and not is_public_ip("224.0.0.1")
    fs.WEB["https://public.test/go"] = fs.redirect("http://intranet.test/admin")
    fs.WEB["https://public.test/loop"] = fs.redirect("/loop")
    fs.WEB["https://public.test/ip"] = fs.redirect("http://127.0.0.1:8000/")
    fs.WEB["https://public.test/big"] = lambda _r: httpx.Response(200, content=b"x" * (MAX_PAGE_BYTES + 5000))
    async with SafeFetcher(transport=httpx.MockTransport(fs.handler), resolver=fs.resolve) as fetcher:
        for path in ("go", "ip", "loop"):
            with pytest.raises(BlockedURL):
                await fetcher.get(f"https://public.test/{path}")
        big = await fetcher.get("https://public.test/big")
    assert big.truncated and len(big.content) == MAX_PAGE_BYTES
    assert not any("intranet" in u or "127.0.0.1" in u for u in fs.SEEN)


def test_save_refuses_private_targets(hub) -> None:
    for url in ("http://10.0.0.1/x", "http://intranet.test/", "file:///etc/passwd", "http://169.254.169.254/"):
        r = hub.post("/saved", json={"url": url})
        assert r.status_code == 422, (url, r.text)
    # A public link redirecting to a private address: the save keeps the URL, nothing private is fetched.
    fs.WEB["https://public.test/go"] = fs.redirect("http://intranet.test/admin")
    item = save(hub, url="https://public.test/go")["item"]
    assert item["enrich_status"] == "pending" and "blocked" in item["enrich_error"]
    assert not any("intranet" in u for u in fs.SEEN)
    assert hub.get("/saved").json()["total"] == 1


# --------------------------------------------------------------------------- save, enrichment, dedupe


def test_youtube_oembed_save_with_thumbnail_and_index(hub) -> None:
    web_youtube()
    fs.ANNOTATION.update({"summary": "Un hymne de stade de 1998.", "tags": ["anthem", "1998"], "type": "video",
                          "collection": "Music", "new_collection": True})
    res = save(hub, url=f"https://youtu.be/{YT_ID}?si=SHARE123", note="for the party")
    item = res["item"]
    assert res["duplicate"] is False
    assert item["title"] == "Retro football anthem (1998)"
    assert item["author"] == "GoalsTV" and item["site"] == "youtube.com" and item["type"] == "video"
    assert item["description"] == "The 1998 stadium anthem, remastered."
    assert item["image_url"] == f"https://i.ytimg.com/vi/{YT_ID}/hqdefault.jpg"
    assert item["thumb"].startswith("https://signed.example/r2/saved/thumbs/")
    assert item["summary"] == "Un hymne de stade de 1998." and item["tags"] == ["anthem", "1998"]
    assert item["suggestion"] == {"name": "Music", "id": None, "new": True}
    assert item["note"] == "for the party" and item["enrich_status"] == "ok" and item["indexed"] is True
    assert any(u.startswith("https://www.youtube.com/oembed?url=") for u in fs.SEEN)
    stored = fakes.STORES["r2"][f"saved/thumbs/{item['id']}.jpg"]
    assert stored[1] in ("image/jpeg", "image/png") and len(stored[0]) <= saved.THUMB_MAX_BYTES
    chunks = fakes_rag.DBS["turso"].execute("SELECT count(*) FROM saved_vec_chunks WHERE item_id = ?",
                                            [item["id"]]).fetchone()[0]
    assert 1 <= chunks <= 8
    assert fakes_rag.DBS["turso"].execute("SELECT count(*) FROM sqlite_schema WHERE name = 'rag_chunks'").fetchone()[0] == 0


def test_x_post_via_oembed_and_fxtwitter(hub) -> None:
    fs.WEB["https://publish.twitter.com/oembed"] = fs.json_response({
        "author_name": "Jack", "html": "<blockquote><p>just setting up my twttr</p>&mdash; Jack (@jack) March 21, 2006"
                                       "</blockquote>"})
    fs.WEB["https://api.fxtwitter.com/status/20"] = fs.json_response({"tweet": {
        "text": "just setting up my twttr", "author": {"name": "Jack"}, "created_timestamp": 1142974214,
        "media": {"photos": [{"url": "https://pbs.test/p.jpg"}]}}})
    item = save(hub, url="https://twitter.com/jack/status/20?s=20")["item"]
    assert item["title"] == "Jack on X" and item["site"] == "x.com" and item["type"] == "social post"
    assert item["description"] == "just setting up my twttr" and item["image_url"] == "https://pbs.test/p.jpg"
    assert item["published_at"].startswith("2006-03-21")


def test_dedupe_bumps_existing_item(hub) -> None:
    web_article()
    web_play()
    first = save(hub, url="https://leaddev.test/focus?utm_source=x&fbclid=y")["item"]
    other = save(hub, url="https://play.google.com/store/apps/details?id=com.stepup")["item"]
    assert [i["id"] for i in hub.get("/saved").json()["items"]] == [other["id"], first["id"]]
    again = save(hub, expect=200, url="https://www.leaddev.test/focus/#comments", note="re-read")
    assert again["duplicate"] is True and again["item"]["id"] == first["id"] and again["item"]["note"] == "re-read"
    listing = hub.get("/saved").json()
    assert listing["total"] == 2 and listing["items"][0]["id"] == first["id"]
    preview = hub.post("/saved/preview", json={"url": "https://leaddev.test/focus?utm_medium=mail"}).json()
    assert preview["preview"] is None and preview["existing"]["id"] == first["id"]


def test_preview_then_save_reuses_enrichment(hub) -> None:
    web_play()
    preview = hub.post("/saved/preview", json={"url": "https://play.google.com/store/apps/details?id=com.stepup"}).json()
    assert preview["preview"]["title"] == "StepUp: pedometer" and preview["preview"]["type"] == "app"
    assert preview["existing"] is None
    seen = len(fs.SEEN)
    item = save(hub, url="https://play.google.com/store/apps/details?id=com.stepup&hl=fr")["item"]
    assert item["title"] == "StepUp: pedometer"
    assert not any("play.google.com" in u for u in fs.SEEN[seen:])  # served from the preview cache


def test_llm_failure_falls_back_to_heuristics(hub) -> None:
    web_article()
    assert hub.post("/saved/collections", json={"name": "Engineering"}).status_code == 201
    fs.CHAT_FAIL["on"] = True
    item = save(hub, url="https://leaddev.test/focus")["item"]
    assert item["ai_source"] == "heuristic" and item["type"] == "article"
    assert item["summary"] == "How a manager keeps an engineering team productive."
    assert "engineering" in item["tags"] and item["suggestion"]["name"] == "Engineering"
    assert item["suggestion"]["new"] is False


def test_unreachable_site_is_kept_pending_then_refreshed(hub) -> None:
    fs.WEB["https://flaky.test/post"] = fs.html("down", status=503)
    item = save(hub, url="https://flaky.test/post")["item"]
    assert item["enrich_status"] == "pending" and item["title"] == "https://flaky.test/post"
    fs.WEB["https://flaky.test/post"] = fs.html("<html><head><title>Back online</title></head><body>"
                                                "<p>Some content about gardening.</p></body></html>")
    r = hub.post(f"/saved/{item['id']}/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["item"]["title"] == "Back online" and r.json()["item"]["enrich_status"] == "ok"


def test_text_and_drive_file_saves(hub) -> None:
    quote = save(hub, text="« La simplicité est la sophistication suprême. »\nLéonard de Vinci")["item"]
    assert quote["kind"] == "text" and quote["title"].startswith("« La simplicité") and quote["url"] is None
    link_as_text = save(hub, text="https://leaddev.test/focus")["item"]
    assert link_as_text["kind"] == "url"
    fakes.STORES["r2"]["uploads/beach-day.png"] = (png(), "image/png")
    photo = save(hub, key="uploads/beach-day.png", provider="r2")["item"]
    assert photo["kind"] == "file" and photo["type"] == "image" and photo["title"] == "beach day"
    assert photo["file"] == {"key": "uploads/beach-day.png", "provider": "r2"}
    assert photo["thumb"].startswith("https://signed.example/r2/")
    again = save(hub, expect=200, key="uploads/beach-day.png", provider="r2")
    assert again["duplicate"] and again["item"]["id"] == photo["id"]
    assert hub.post("/saved", json={"key": "uploads/missing.png", "provider": "r2"}).status_code == 404
    assert hub.post("/saved", json={"note": "nothing to save"}).status_code == 422


def test_patch_and_delete_item(hub) -> None:
    web_article()
    item = save(hub, url="https://leaddev.test/focus")["item"]
    r = hub.patch(f"/saved/{item['id']}", json={"note": "share with the team", "tags": ["Lead", "#lead", "focus"]})
    assert r.status_code == 200 and r.json()["note"] == "share with the team" and r.json()["tags"] == ["lead", "focus"]
    assert hub.get(f"/saved/{item['id']}").json()["text"].startswith("Engineering managers")
    assert hub.delete(f"/saved/{item['id']}").status_code == 204
    assert hub.get(f"/saved/{item['id']}").status_code == 404
    assert hub.delete(f"/saved/{item['id']}").status_code == 404
    assert not fakes.STORES["r2"].get(f"saved/thumbs/{item['id']}.jpg")


# --------------------------------------------------------------------------- collections


def test_collections_crud_counts_and_suggestion(hub) -> None:
    ids = save_three(hub)
    created = hub.post("/saved/collections", json={"name": "Watch later", "emoji": "🎬", "color": "#FF7A9C"})
    assert created.status_code == 201 and created.json()["count"] == 0
    col = created.json()
    assert hub.post("/saved/collections", json={"name": "watch LATER"}).status_code == 409
    assert hub.post("/saved/collections", json={"name": "Bad", "color": "red"}).status_code == 422

    moved = hub.patch(f"/saved/{ids['yt']}", json={"collection_id": col["id"]}).json()
    assert moved["collection_id"] == col["id"] and moved["suggestion"] is None
    accepted = hub.patch(f"/saved/{ids['art']}", json={"collection": "Productivity"}).json()  # one-tap accept
    assert accepted["collection_id"] and accepted["suggestion"] is None
    assert hub.patch(f"/saved/{ids['app']}", json={"collection_id": "nope"}).status_code == 404

    listing = hub.get("/saved/collections").json()
    counts = {c["name"]: c["count"] for c in listing["collections"]}
    assert counts == {"Watch later": 1, "Productivity": 1}
    assert listing["uncategorised"] == 1 and listing["total"] == 3
    assert {s["type"]: s["count"] for s in listing["smart"]} == {"video": 1, "article": 1, "app": 1}
    assert {s["label"] for s in listing["smart"]} == {"Videos", "Articles", "Apps"}
    assert [i["id"] for i in hub.get(f"/saved?collection={col['id']}").json()["items"]] == [ids["yt"]]
    assert [i["id"] for i in hub.get("/saved?collection=none").json()["items"]] == [ids["app"]]
    assert [i["id"] for i in hub.get("/saved?type=app").json()["items"]] == [ids["app"]]
    assert hub.get("/saved?type=weird").status_code == 422

    renamed = hub.patch(f"/saved/collections/{col['id']}", json={"name": "Videos to watch", "emoji": "📺"}).json()
    assert renamed["name"] == "Videos to watch" and renamed["emoji"] == "📺" and renamed["count"] == 1
    assert hub.patch(f"/saved/collections/{col['id']}", json={"name": "productivity"}).status_code == 409
    assert hub.delete(f"/saved/collections/{col['id']}").status_code == 204
    assert hub.delete(f"/saved/collections/{col['id']}").status_code == 404
    assert hub.get(f"/saved/{ids['yt']}").json()["collection_id"] is None
    assert hub.get("/saved/collections").json()["uncategorised"] == 2


# --------------------------------------------------------------------------- search and ask


def test_hybrid_search_finds_by_description(hub) -> None:
    ids = save_three(hub)

    def top(q: str) -> dict[str, Any]:
        r = hub.get("/saved/search", params={"q": q})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "hybrid" and body["results"], body
        return body

    found = top("the video about a nostalgic football song")
    assert found["results"][0]["item"]["id"] == ids["yt"] and found["type_hint"] == "video"
    assert found["reranked"] is True
    assert top("l'article sur le manager engineering")["results"][0]["item"]["id"] == ids["art"]
    walk = top("une app pour marcher")
    assert walk["results"][0]["item"]["id"] == ids["app"] and walk["type_hint"] == "app"
    meaning = top("throwback soccer hymne")["results"][0]
    assert meaning["item"]["id"] == ids["yt"] and meaning["match"] == "meaning"
    assert meaning["reason"] == "Similar in meaning"
    tagged = top("stadium")["results"][0]
    assert tagged["item"]["id"] == ids["yt"] and tagged["reason"].startswith("Tags matches")
    assert "stadium" in tagged["snippet"]


def test_search_without_vectors_is_keyword_only(make_hub) -> None:
    hub = make_hub(saved_plugins(turso=False))
    web_article()
    item = save(hub, url="https://leaddev.test/focus")["item"]
    assert item["indexed"] is False
    body = hub.get("/saved/search", params={"q": "managers focus"}).json()
    assert body["mode"] == "keyword" and body["results"][0]["item"]["id"] == item["id"]
    assert body["results"][0]["match"] == "keyword"


def test_ask_with_citations_and_period(hub) -> None:
    ids = save_three(hub)
    r = hub.post("/saved/ask", json={"question": "which football song did I save?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "[1]" in body["answer"] and body["sources"][0]["id"] == ids["yt"] and body["sources"][0]["n"] == 1
    prompt = str(fs.CHATS[-1].messages[1].content)
    assert "[1] Retro football anthem (1998)" in prompt and "Question: which football song did I save?" in prompt

    last_month = (local_today().replace(day=1) - timedelta(days=3)).isoformat() + "T10:00:00Z"
    fa.sql("UPDATE app_saved SET saved_at = ? WHERE id = ?", [last_month, ids["art"]])
    fs.ANSWER["text"] = "Last month you saved one article about engineering managers [1]."
    body = hub.post("/saved/ask", json={"question": "what did I save last month?"}).json()
    assert [s["id"] for s in body["sources"]] == [ids["art"]] and body["period"]["key"] == "last_month"
    fr = hub.post("/saved/ask", json={"question": "qu'est-ce que j'ai enregistré le mois dernier ?"}).json()
    assert [s["id"] for s in fr["sources"]] == [ids["art"]]

    fs.CHAT_FAIL["on"] = True
    down = hub.post("/saved/ask", json={"question": "football anthem"}).json()
    assert down["sources"] and "[1]" in down["answer"]


def test_ask_with_nothing_saved(hub) -> None:
    body = hub.post("/saved/ask", json={"question": "anything about cooking?"}).json()
    assert body["answer"] == saved.NOT_FOUND_ANSWER and body["sources"] == []


def test_parse_period() -> None:
    from datetime import date

    today = date(2026, 10, 22)  # a Thursday
    assert saved.parse_period("what did I save last month?", today)[:2] == (date(2026, 9, 1), date(2026, 9, 30))
    assert saved.parse_period("la semaine dernière", today)[:2] == (date(2026, 10, 12), date(2026, 10, 18))
    assert saved.parse_period("chno savit had chhar", today)[:2] == (date(2026, 10, 1), today)
    assert saved.parse_period("les 7 derniers jours", today)[:2] == (date(2026, 10, 16), today)
    assert saved.parse_period("hier", today)[:2] == (date(2026, 10, 21), date(2026, 10, 21))
    assert saved.parse_period("the football video", today) is None
    assert saved.type_hint("une vidéo sur le foot") == "video" and saved.type_hint("تطبيق للمشي") == "app"
    assert saved.type_hint("the article") == "article" and saved.type_hint("something") is None


# --------------------------------------------------------------------------- integrations


def test_global_search_includes_saved(hub) -> None:
    ids = save_three(hub)
    body = hub.get("/search", params={"q": "football"}).json()
    groups = {g["type"]: g for g in body["groups"]}
    assert "saved" in groups and groups["saved"]["label"] == "Saved"
    hit = groups["saved"]["items"][0]
    assert hit["id"] == ids["yt"] and hit["data"]["site"] == "youtube.com" and hit["data"]["type"] == "video"
    assert hub.get("/search", params={"q": "football", "types": "saved"}).json()["groups"][0]["type"] == "saved"


def test_mcp_saved_tools(hub) -> None:
    ids = save_three(hub)
    mcp = MCP(hub.client)
    found = mcp.ok("saved_search", query="nostalgic football song", limit=3)
    assert found["results"][0]["id"] == ids["yt"] and found["results"][0]["reason"]
    cols = mcp.ok("collections_list")
    assert cols["total"] == 3 and cols["uncategorised"] == 3
    listing = mcp.ok("saved_list", type="app")
    assert [i["id"] for i in listing["items"]] == [ids["app"]]
    added = mcp.ok("saved_add", text="Remember: buy the 1998 jersey", collection="Ideas")
    assert added["duplicate"] is False and added["item"]["collection_id"]
    answer = mcp.ok("saved_ask", question="football anthem")
    assert answer["sources"][0]["id"] == ids["yt"]
    bad = mcp.call("saved_list", type="weird")
    assert bad["isError"] is True


async def test_daily_summary_counts_saved_items() -> None:
    from hub_api.daily_summary import saved_line

    adapter = fa.FakeD1(PluginContext(name="d1", kind=ProviderKind.DB))

    async def source() -> Any:
        return adapter

    db = AppDB(source)
    today = local_today()
    assert await saved_line(db, today) is None  # no table yet
    await saved.saved_db(db)
    assert await saved_line(db, today) is None
    now = saved.now_iso()
    for i in range(2):
        await db.execute("INSERT INTO app_saved (id, kind, saved_at, created_at, updated_at) VALUES (?, 'text', ?, ?, ?)",
                         [f"s{i}", now, now, now])
    assert await saved_line(db, today) == "Enregistrés depuis hier : 2 éléments"
