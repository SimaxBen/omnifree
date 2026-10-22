"""Saved library: URL canonicalisation, SSRF-safe fetching, page metadata and the AI/heuristic annotation.

Everything here is pure or takes its I/O as arguments (an ``httpx`` transport and a DNS resolver), so the tests
run it against ``httpx.MockTransport`` with a fake resolver and never touch the network.

* :func:`canonical_url` normalises a URL for dedupe: lower-case scheme/host, ``www.``/``m.`` dropped, tracking
  parameters (``utm_*``, ``fbclid``, ``gclid``, ``si`` ...) and fragments removed, ``youtu.be``/shorts folded into
  ``youtube.com/watch?v=``, ``twitter.com`` into ``x.com``.
* :func:`check_url` is the SSRF guard: http/https only, and every address the host resolves to must be public
  (no private, loopback, link-local, multicast, reserved or CGNAT ranges). :class:`SafeFetcher` applies it to
  every redirect hop (redirects are followed by hand, at most :data:`MAX_REDIRECTS`) and caps the body size.
* :func:`parse_meta` reads OpenGraph / Twitter / standard meta tags, ``<link rel=canonical|icon>`` and JSON-LD dates;
  :func:`readable_text` keeps the main text (``<article>``/``<main>`` when present, never nav/header/footer).
* :func:`enrich_url` runs the whole best-effort pipeline within a time budget, with the YouTube / X / TikTok
  oEmbed special cases; it never raises for a site that blocks scrapers, it returns what it has.
* :func:`heuristic_annotation` is the no-LLM fallback for summary, tags, content type and collection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

__all__ = [
    "CONTENT_TYPES",
    "BlockedURL",
    "PageMeta",
    "Enrichment",
    "SafeFetcher",
    "Resolver",
    "canonical_url",
    "site_of",
    "check_url",
    "is_public_ip",
    "parse_meta",
    "readable_text",
    "youtube_id",
    "tweet_id",
    "enrich_url",
    "heuristic_annotation",
    "clean_annotation",
    "annotation_prompt",
    "guess_type",
    "keywords_of",
]

CONTENT_TYPES: tuple[str, ...] = (
    "article", "video", "social post", "app", "product", "recipe", "place", "music", "tool", "doc", "image", "other",
)

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-S931B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Mobile Safari/537.36"
)
# Instagram / TikTok / Facebook only serve their OpenGraph tags to link-preview bots.
PREVIEW_BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
ACCEPT_LANGUAGE = "fr-FR,fr;q=0.9,en;q=0.8,ar;q=0.6"

MAX_PAGE_BYTES = 1_500_000
MAX_JSON_BYTES = 300_000
MAX_REDIRECTS = 5
TEXT_CHARS = 20_000
ENRICH_BUDGET_S = 12.0

Resolver = Callable[[str], Awaitable[list[str]]]


class BlockedURL(ValueError):
    """The URL is not allowed (scheme, private address, too many redirects)."""


# --------------------------------------------------------------------------- canonical URLs

_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "igsh",
    "ref_src", "ref_url", "_hsenc", "_hsmi", "mkt_tok", "oly_anon_id", "oly_enc_id", "vero_id", "spm", "si",
    "feature", "share", "share_id", "ref", "utm", "rcm", "smid", "smtyp", "cmpid", "s_cid", "wt_mc",
    "_ga", "_gl", "trk", "trkcampaign", "sr_share", "ved", "usg", "ei", "sxsrf", "pp", "ab_channel",
})
_X_HOSTS = frozenset({"twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com", "fxtwitter.com", "vxtwitter.com",
                      "fixupx.com", "fixvx.com", "nitter.net"})
_YT_HOSTS = frozenset({"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"})
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TWEET_RE = re.compile(r"/status(?:es)?/(\d{1,25})")


def _bare_host(host: str) -> str:
    host = host.lower().strip(".")
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
    return host


def youtube_id(url: str) -> str | None:
    """The 11-character video id of a YouTube URL (watch, youtu.be, shorts, embed, live), else None."""
    parts = urlsplit(url.strip())
    host = _bare_host(parts.hostname or "")
    if host not in _YT_HOSTS and host != "youtube.com":
        return None
    if host == "youtu.be":
        cand = parts.path.strip("/").split("/")[0]
        return cand if _YT_ID_RE.match(cand) else None
    query = dict(parse_qsl(parts.query))
    if _YT_ID_RE.match(query.get("v", "")):
        return query["v"]
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 2 and segs[0] in ("shorts", "embed", "live", "v") and _YT_ID_RE.match(segs[1]):
        return segs[1]
    return None


def tweet_id(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if _bare_host(parts.hostname or "") not in _X_HOSTS:
        return None
    m = _TWEET_RE.search(parts.path)
    return m.group(1) if m else None


def _is_tracking(name: str) -> bool:
    low = name.lower()
    return low.startswith("utm_") or low.startswith("pk_") or low.startswith("hsa_") or low in _TRACKING_PARAMS


def canonical_url(url: str) -> str:
    """A stable form of ``url`` for dedupe. Raises :class:`BlockedURL` when it is not an http(s) URL."""
    raw = (url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("only http and https links can be saved")
    vid = youtube_id(raw)
    if vid:
        return f"https://youtube.com/watch?v={vid}"
    host = _bare_host(parts.hostname)
    if host in _X_HOSTS:
        tid = tweet_id(raw)
        if tid:
            user = parts.path.strip("/").split("/")[0] or "i"
            return f"https://x.com/{user.lower()}/status/{tid}"
        host = "x.com"
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _is_tracking(k)]
    if host == "play.google.com":
        query = [(k, v) for k, v in query if k == "id"]
    if host in ("x.com",):
        query = [(k, v) for k, v in query if k not in ("s", "t")]
    query.sort()
    return urlunsplit(("https", netloc, quote(path, safe="/%:@!$&'()*+,;=-._~"),
                       urlencode(query), ""))


def site_of(url: str | None) -> str:
    """``youtube.com``, ``x.com``, ``play.google.com``: the host without ``www.`` (empty for no URL)."""
    if not url:
        return ""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = _bare_host(host)
    if host == "youtu.be":
        return "youtube.com"
    if host in _X_HOSTS:
        return "x.com"
    return host


# --------------------------------------------------------------------------- SSRF guard

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return is_public_ip(str(mapped))
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local
                                       or ip.is_private or ip.is_unspecified)


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def check_url(url: str, resolver: Resolver = system_resolver) -> str:
    """Refuse non-http(s) URLs and hosts resolving to a non-public address. Returns the URL."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise BlockedURL(f"scheme '{parts.scheme or '(none)'}' is not allowed")
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise BlockedURL("the URL has no host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise BlockedURL(f"host '{host}' is not public")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await resolver(host)
        except (OSError, UnicodeError) as exc:
            raise BlockedURL(f"cannot resolve '{host}'") from exc
    if not addresses:
        raise BlockedURL(f"cannot resolve '{host}'")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise BlockedURL(f"host '{host}' resolves to a non-public address")
    return url


@dataclass
class Fetched:
    url: str
    status: int
    content_type: str
    content: bytes
    truncated: bool = False

    def text(self) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.content_type or "", re.I)
        if m:
            charset = m.group(1)
        else:
            head = self.content[:2048].decode("ascii", errors="ignore")
            m2 = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
            if m2:
                charset = m2.group(1)
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class SafeFetcher:
    """GET with the SSRF guard on every hop, manual redirects and a body cap."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver = system_resolver,
                 timeout_s: float = 8.0) -> None:
        self.resolver = resolver
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_s, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SafeFetcher:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get(self, url: str, *, max_bytes: int = MAX_PAGE_BYTES, user_agent: str = BROWSER_UA,
                  accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> Fetched:
        headers = {"User-Agent": user_agent, "Accept": accept, "Accept-Language": ACCEPT_LANGUAGE}
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            await check_url(current, self.resolver)
            async with self._client.stream("GET", current, headers=headers) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise BlockedURL("redirect without a location")
                    current = urljoin(current, location)
                    continue
                buf = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        del buf[max_bytes:]
                        truncated = True
                        break
                return Fetched(url=str(current), status=resp.status_code,
                               content_type=resp.headers.get("content-type", ""), content=bytes(buf), truncated=truncated)
        raise BlockedURL(f"more than {MAX_REDIRECTS} redirects")

    async def get_json(self, url: str) -> Any:
        res = await self.get(url, max_bytes=MAX_JSON_BYTES, user_agent="OmniFree/1.0 (+link preview)",
                             accept="application/json")
        if res.status >= 400:
            raise httpx.HTTPStatusError(f"HTTP {res.status}", request=httpx.Request("GET", url),
                                        response=httpx.Response(res.status))
        return json.loads(res.content.decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------- HTML parsing


@dataclass
class PageMeta:
    title: str | None = None
    description: str | None = None
    image: str | None = None
    site_name: str | None = None
    favicon: str | None = None
    canonical: str | None = None
    published: str | None = None
    author: str | None = None
    og_type: str | None = None
    lang: str | None = None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.ld_json: list[str] = []
        self.lang: str | None = None
        self._in_title = False
        self._in_ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rels = a.get("rel", "").lower().split()
            href = a.get("href", "").strip()
            if href:
                for rel in rels:
                    if rel in ("canonical", "icon", "apple-touch-icon", "shortcut") and rel not in self.links:
                        self.links[rel] = href
        elif tag == "title" and not self.title_parts:
            self._in_title = True
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self._ld_buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            if len(self.ld_json) < 5:
                self.ld_json.append("".join(self._ld_buf))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_ld:
            self._ld_buf.append(data)


def _clean(text: str | None, limit: int = 600) -> str | None:
    if not text:
        return None
    flat = re.sub(r"\s+", " ", unescape(text)).strip()
    return flat[:limit] or None


def _ld_fields(blobs: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for key in ("datePublished", "uploadDate", "dateCreated"):
                if isinstance(node.get(key), str) and "published" not in out:
                    out["published"] = node[key]
            author = node.get("author")
            if "author" not in out:
                if isinstance(author, dict) and isinstance(author.get("name"), str):
                    out["author"] = author["name"]
                elif isinstance(author, list) and author and isinstance(author[0], dict) and isinstance(author[0].get("name"), str):
                    out["author"] = author[0]["name"]
            for key in ("headline", "name"):
                if isinstance(node.get(key), str) and "headline" not in out:
                    out["headline"] = node[key]
            if "@graph" in node:
                walk(node["@graph"])

    for blob in blobs:
        try:
            walk(json.loads(blob))
        except ValueError:
            continue
    return out


def parse_meta(html: str, base_url: str) -> PageMeta:
    """OpenGraph / Twitter / standard metadata of a page (absolute URLs, HTML entities decoded)."""
    parser = _MetaParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a broken page still gives whatever was parsed so far
        pass
    m = parser.meta
    ld = _ld_fields(parser.ld_json)

    def first(*keys: str) -> str | None:
        for k in keys:
            if m.get(k):
                return m[k]
        return None

    def absolute(value: str | None) -> str | None:
        if not value:
            return None
        try:
            out = urljoin(base_url, value.strip())
        except ValueError:
            return None
        return out if out.startswith(("http://", "https://")) else None

    favicon = parser.links.get("icon") or parser.links.get("shortcut") or parser.links.get("apple-touch-icon")
    if not favicon:
        parts = urlsplit(base_url)
        favicon = f"{parts.scheme}://{parts.netloc}/favicon.ico" if parts.netloc else None
    return PageMeta(
        title=_clean(first("og:title", "twitter:title") or "".join(parser.title_parts) or ld.get("headline"), 300),
        description=_clean(first("og:description", "twitter:description", "description"), 1000),
        image=absolute(first("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src",
                             "thumbnailurl")),
        site_name=_clean(first("og:site_name", "application-name", "twitter:site"), 100),
        favicon=absolute(favicon),
        canonical=absolute(first("og:url") or parser.links.get("canonical")),
        published=_clean(first("article:published_time", "og:published_time", "datepublished", "date",
                               "pubdate", "publish-date", "dc.date") or ld.get("published"), 40),
        author=_clean(first("author", "article:author", "twitter:creator") or ld.get("author"), 120),
        og_type=_clean(first("og:type"), 60),
        lang=_clean(parser.lang, 20),
    )


class _TextParser(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head", "nav", "header", "footer", "aside",
                       "form", "button", "select", "iframe", "canvas", "figure"})
    _MAIN = frozenset({"article", "main"})
    _BLOCK = frozenset({"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "h1", "h2", "h3", "h4", "h5",
                        "h6", "pre", "blockquote", "hr", "dd", "dt"} | _MAIN)
    _VOID = frozenset({"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed",
                       "param", "track"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.all: list[str] = []
        self.main: list[str] = []
        self._skip = 0
        self._main = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID:
            if tag in self._BLOCK:
                self._emit("\n")
            return
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._MAIN:
            self._main += 1
        if tag in self._BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(self._skip - 1, 0)
        elif tag in self._MAIN:
            self._main = max(self._main - 1, 0)
        if tag in self._BLOCK:
            self._emit("\n")

    def _emit(self, text: str) -> None:
        if self._skip:
            return
        self.all.append(text)
        if self._main:
            self.main.append(text)

    def handle_data(self, data: str) -> None:
        self._emit(data)


def _normalise_text(text: str) -> str:
    lines = [re.sub(r"[ \t\f\v ]+", " ", ln).strip() for ln in text.replace("\r", "\n").split("\n")]
    out = "\n".join(ln for ln in lines if ln)
    return out


def readable_text(html: str, limit: int = TEXT_CHARS) -> str:
    """The page's main text: ``<article>``/``<main>`` when they hold enough, else the body minus chrome."""
    parser = _TextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    main = _normalise_text("".join(parser.main))
    text = main if len(main) >= 200 else _normalise_text("".join(parser.all))
    return text[:limit]


# --------------------------------------------------------------------------- enrichment pipeline


@dataclass
class Enrichment:
    url: str
    canonical: str
    site: str
    title: str | None = None
    description: str | None = None
    image: str | None = None
    site_name: str | None = None
    favicon: str | None = None
    published: str | None = None
    author: str | None = None
    og_type: str | None = None
    lang: str | None = None
    text: str = ""
    status: str = "pending"  # ok | partial | pending (nothing could be read: retry later)
    error: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("url", "canonical", "site", "title", "description", "image",
                                              "site_name", "favicon", "published", "author", "og_type", "lang",
                                              "status", "error")}

    def absorb(self, meta: PageMeta) -> None:
        for name in ("title", "description", "image", "site_name", "favicon", "published", "author", "og_type", "lang"):
            if getattr(self, name) in (None, "") and getattr(meta, name):
                setattr(self, name, getattr(meta, name))


_PLAY_SUFFIX = re.compile(r"\s*[-–—|]\s*(Apps on Google Play|Applications sur Google Play|Apps bei Google Play|"
                          r"Aplicaciones en Google Play|Google Play)\s*$", re.I)
_SITE_SUFFIX = re.compile(r"\s*[|–—-]\s*(YouTube|X|Twitter|Instagram|TikTok)\s*$")


def _tidy_title(title: str | None, site: str) -> str | None:
    if not title:
        return title
    out = _PLAY_SUFFIX.sub("", title) if site == "play.google.com" else _SITE_SUFFIX.sub("", title)
    return out.strip() or title


async def _youtube(fetcher: SafeFetcher, url: str, out: Enrichment) -> None:
    vid = youtube_id(url)
    data = await fetcher.get_json("https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"}))
    if isinstance(data, dict):
        out.title = out.title or _clean(data.get("title"), 300)
        out.author = out.author or _clean(data.get("author_name"), 120)
        out.image = out.image or data.get("thumbnail_url") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None)
        out.site_name = out.site_name or "YouTube"
        out.hints["channel"] = data.get("author_name")


async def _twitter_oembed(fetcher: SafeFetcher, url: str, out: Enrichment) -> None:
    data = await fetcher.get_json("https://publish.twitter.com/oembed?" + urlencode(
        {"url": url, "omit_script": "1", "dnt": "true"}))
    if isinstance(data, dict):
        html = str(data.get("html") or "")
        text = readable_text(html, 2000) if html else ""
        # The blockquote ends with "— Name (@user) Date": keep only the post body.
        body = re.split(r"\n?—\s", text, maxsplit=1)[0].strip()
        if body:
            out.text = out.text or body
            out.description = out.description or _clean(body, 1000)
        author = data.get("author_name")
        if author:
            out.author = out.author or _clean(author, 120)
            out.title = out.title or _clean(f"{author} on X", 300)
        out.site_name = out.site_name or "X"


async def _fxtwitter(fetcher: SafeFetcher, url: str, out: Enrichment) -> None:
    tid = tweet_id(url)
    if not tid:
        return
    data = await fetcher.get_json(f"https://api.fxtwitter.com/status/{tid}")
    tweet = data.get("tweet") if isinstance(data, dict) else None
    if not isinstance(tweet, dict):
        return
    text = str(tweet.get("text") or "").strip()
    author = tweet.get("author") or {}
    name = author.get("name") if isinstance(author, dict) else None
    if text:
        out.text = out.text or text
        out.description = out.description or _clean(text, 1000)
    if name:
        out.author = out.author or _clean(name, 120)
        out.title = out.title or _clean(f"{name} on X", 300)
    media = tweet.get("media") or {}
    if isinstance(media, dict):
        for kind, key in (("photos", "url"), ("videos", "thumbnail_url")):
            items = media.get(kind) or []
            if items and isinstance(items[0], dict) and items[0].get(key):
                out.image = out.image or items[0][key]
                break
    if not out.image and isinstance(author, dict) and author.get("avatar_url"):
        out.image = author["avatar_url"]
    stamp = tweet.get("created_timestamp")
    if isinstance(stamp, int | float) and not out.published:
        out.published = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
    out.site_name = out.site_name or "X"


async def _tiktok(fetcher: SafeFetcher, url: str, out: Enrichment) -> None:
    data = await fetcher.get_json("https://www.tiktok.com/oembed?" + urlencode({"url": url}))
    if isinstance(data, dict):
        out.title = out.title or _clean(data.get("title"), 300)
        out.author = out.author or _clean(data.get("author_name"), 120)
        out.image = out.image or data.get("thumbnail_url")
        out.site_name = out.site_name or "TikTok"


async def _page(fetcher: SafeFetcher, url: str, out: Enrichment, user_agent: str) -> None:
    res = await fetcher.get(url, user_agent=user_agent)
    if res.status >= 400:
        raise httpx.HTTPStatusError(f"HTTP {res.status}", request=httpx.Request("GET", url),
                                    response=httpx.Response(res.status))
    ctype = res.content_type.split(";")[0].strip().lower()
    out.hints["final_url"] = res.url
    out.hints["mime"] = ctype
    if ctype.startswith("image/"):
        out.image = out.image or res.url
        out.og_type = out.og_type or "image"
        return
    if ctype == "application/pdf":
        out.og_type = out.og_type or "pdf"
        return
    if ctype and "html" not in ctype and "xml" not in ctype and not ctype.startswith("text/"):
        return
    html = res.text()
    out.absorb(parse_meta(html, res.url))
    if not out.text:
        out.text = readable_text(html)


async def enrich_url(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver = system_resolver,
    budget_s: float = ENRICH_BUDGET_S,
) -> Enrichment:
    """Best-effort metadata for ``url`` within ``budget_s``. Raises :class:`BlockedURL` only for a refused URL."""
    canonical = canonical_url(url)
    await check_url(url, resolver)  # refuse private targets before anything else (raises BlockedURL)
    site = site_of(url)
    out = Enrichment(url=url, canonical=canonical, site=site)
    errors: list[str] = []
    jobs: list[Awaitable[None]] = []
    async with SafeFetcher(transport=transport, resolver=resolver, timeout_s=max(budget_s - 1, 2)) as fetcher:
        if youtube_id(url):
            jobs = [_youtube(fetcher, url, out), _page(fetcher, url, out, BROWSER_UA)]
        elif tweet_id(url):
            jobs = [_twitter_oembed(fetcher, url, out), _fxtwitter(fetcher, url, out)]
        elif site.endswith("tiktok.com"):
            jobs = [_tiktok(fetcher, url, out), _page(fetcher, url, out, PREVIEW_BOT_UA)]
        elif site.endswith("instagram.com") or site.endswith("facebook.com") or site.endswith("threads.net"):
            jobs = [_page(fetcher, url, out, PREVIEW_BOT_UA)]
        else:
            jobs = [_page(fetcher, url, out, BROWSER_UA)]
        tasks = [asyncio.ensure_future(j) for j in jobs]
        done, pending = await asyncio.wait(tasks, timeout=budget_s)
        for task in pending:
            task.cancel()
            errors.append("timed out")
        for task in done:
            exc = task.exception()
            if exc is not None:
                errors.append(_describe(exc))
    out.title = _tidy_title(out.title, site)
    if out.og_type == "pdf" and not out.title:
        out.title = urlsplit(url).path.rsplit("/", 1)[-1] or None
    useful = bool(out.title or out.description or out.text)
    out.status = "ok" if useful and not errors else ("partial" if useful or out.image else "pending")
    out.error = "; ".join(errors)[:500] or None
    return out


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BlockedURL):
        return f"blocked: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc)
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "timed out"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


async def enrich_with_deadline(url: str, *, transport: httpx.AsyncBaseTransport | None, resolver: Resolver,
                               budget_s: float) -> Enrichment:
    """:func:`enrich_url`, but a blown budget still gives the URL-only result (status ``pending``)."""
    try:
        return await asyncio.wait_for(enrich_url(url, transport=transport, resolver=resolver, budget_s=budget_s),
                                      budget_s + 0.5)
    except TimeoutError:
        out = Enrichment(url=url, canonical=canonical_url(url), site=site_of(url))
        out.error = "timed out"
        return out


# --------------------------------------------------------------------------- annotation (LLM + heuristics)

_STOP = frozenset("""
a an the and or of to in on for with by at from is are was were be this that these those it its as your you my our
i me we he she they them his her their not no yes how what why when where which who about into over more most new
le la les un une des du de d l et ou au aux en dans sur pour par avec sans ce cet cette ces se sa son ses leur leurs
est sont été etre être qui que quoi dont où ne pas plus moins tout tous toute toutes très tres mon ma mes ton ta tes
nos vos votre notre il elle ils elles on je tu nous vous y a à l' d' qu' c'est comment pourquoi quand
dyal dial f fi w wla li lli had hadi hada chi shi wach kifach fin mn men 3la ela bach ana nta nti howa hiya
video vidéo article app application site page post www com http https
""".split())


def keywords_of(text: str, limit: int = 6) -> list[str]:
    """Distinct meaningful words of ``text`` (first occurrence order), lower case."""
    out: list[str] = []
    for word in re.findall(r"[^\W\d_][\w'-]{2,}", (text or "").lower()):
        word = word.strip("'-")
        if len(word) < 3 or word in _STOP or word in out:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return out


_TYPE_BY_SITE: tuple[tuple[str, str], ...] = (
    ("youtube.com", "video"), ("vimeo.com", "video"), ("dailymotion.com", "video"), ("twitch.tv", "video"),
    ("tiktok.com", "video"), ("x.com", "social post"), ("instagram.com", "social post"), ("facebook.com", "social post"),
    ("threads.net", "social post"), ("linkedin.com", "social post"), ("reddit.com", "social post"),
    ("bsky.app", "social post"), ("mastodon.social", "social post"),
    ("play.google.com", "app"), ("apps.apple.com", "app"), ("f-droid.org", "app"),
    ("open.spotify.com", "music"), ("spotify.com", "music"), ("soundcloud.com", "music"), ("deezer.com", "music"),
    ("music.apple.com", "music"), ("anghami.com", "music"), ("bandcamp.com", "music"),
    ("github.com", "tool"), ("gitlab.com", "tool"), ("npmjs.com", "tool"), ("pypi.org", "tool"),
    ("huggingface.co", "tool"), ("producthunt.com", "tool"),
    ("maps.google.com", "place"), ("maps.app.goo.gl", "place"), ("goo.gl", "place"), ("tripadvisor.com", "place"),
    ("booking.com", "place"), ("airbnb.com", "place"),
    ("amazon.com", "product"), ("amazon.fr", "product"), ("aliexpress.com", "product"), ("jumia.ma", "product"),
    ("ebay.com", "product"), ("avito.ma", "product"), ("temu.com", "product"), ("etsy.com", "product"),
    ("marmiton.org", "recipe"), ("allrecipes.com", "recipe"), ("cuisineaz.com", "recipe"),
    ("docs.google.com", "doc"), ("notion.so", "doc"), ("arxiv.org", "doc"), ("medium.com", "article"),
    ("substack.com", "article"), ("wikipedia.org", "article"),
)
_TYPE_WORDS: tuple[tuple[str, str], ...] = (
    ("recipe", "recipe"), ("recette", "recipe"), ("ingredients", "recipe"), ("ingrédients", "recipe"),
    ("podcast", "music"), ("album", "music"), ("song", "music"), ("chanson", "music"),
)


def guess_type(*, site: str = "", og_type: str | None = None, mime: str | None = None, kind: str = "url",
               title: str = "", text: str = "") -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/") or og_type == "image":
        return "image"
    if mime == "application/pdf" or og_type == "pdf":
        return "doc"
    if kind == "text":
        return "other"
    for domain, ctype in _TYPE_BY_SITE:
        if site == domain or site.endswith("." + domain):
            return ctype
    og = (og_type or "").lower()
    if og.startswith("video"):
        return "video"
    if og.startswith("music"):
        return "music"
    if og in ("product", "og:product", "product.item"):
        return "product"
    if og in ("place", "business.business", "restaurant.restaurant"):
        return "place"
    low = f"{title} {text[:1500]}".lower()
    for word, ctype in _TYPE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", low):
            return ctype
    if og == "article" or len(text) > 1500:
        return "article"
    return "other"


def _first_sentences(text: str, limit: int = 240) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 60 else cut.rstrip() + "…").strip()


def _match_collection(words: Sequence[str], collections: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for col in collections:
        name_words = set(keywords_of(str(col.get("name") or ""), 8)) | {str(col.get("name") or "").lower()}
        hits = sum(1 for w in words if w in name_words or any(w.startswith(n[:5]) for n in name_words if len(n) >= 5))
        if hits and (best is None or hits > best[0]):
            best = (hits, col)
    return best[1] if best else None


def heuristic_annotation(*, title: str, description: str, text: str, site: str, ctype: str,
                         collections: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summary, tags, content type and collection without an LLM."""
    summary = _first_sentences(description or text or title)
    words = keywords_of(f"{title} {description}", 8)
    tags = words[:5]
    if site and site.split(".")[0] not in tags and len(tags) < 6:
        tags.append(site.split(".")[0])
    col = _match_collection(words, collections)
    return {
        "summary": summary,
        "tags": tags[:6],
        "type": ctype,
        "collection": col.get("name") if col else None,
        "collection_id": col.get("id") if col else None,
        "new_collection": False,
        "source": "heuristic",
    }


def annotation_prompt(*, title: str, site: str, description: str, text: str, note: str, collections: Sequence[str],
                      kind: str) -> tuple[str, str]:
    existing = ", ".join(f'"{c}"' for c in collections) or "(none yet)"
    system = (
        "You file items into a personal 'saved' library. Answer with ONE JSON object only:\n"
        '{"summary": "1-2 sentences saying what this is, in the SAME language as the content", '
        '"tags": ["3 to 6 short lowercase topic tags"], '
        f'"type": one of {json.dumps(list(CONTENT_TYPES))}, '
        '"collection": "the best collection name", "new_collection": true|false}\n'
        f"Existing collections: {existing}. Prefer an existing one (exact name, new_collection false); "
        "only when none fits, propose a short new name (1-2 words, Title Case, English) with new_collection true.\n"
        "Never invent facts that are not in the content."
    )
    body = (
        f"Kind: {kind}\nSite: {site or '-'}\nTitle: {title or '-'}\nDescription: {description or '-'}\n"
        + (f"Owner's note: {note}\n" if note else "")
        + f"Content excerpt:\n{(text or '')[:3500]}"
    )
    return system, body


def clean_annotation(raw: dict[str, Any] | None, *, fallback: dict[str, Any],
                     collections: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Validate the LLM JSON; anything missing or malformed comes from ``fallback`` (the heuristics)."""
    if not isinstance(raw, dict):
        return fallback
    out = dict(fallback)
    summary = raw.get("summary")
    if isinstance(summary, str) and summary.strip():
        out["summary"] = re.sub(r"\s+", " ", summary).strip()[:600]
    tags = raw.get("tags")
    if isinstance(tags, list):
        clean = []
        for t in tags:
            if isinstance(t, str):
                tag = re.sub(r"\s+", " ", t.strip().lstrip("#").lower())[:40]
                if tag and tag not in clean:
                    clean.append(tag)
        if clean:
            out["tags"] = clean[:6]
    ctype = raw.get("type")
    if isinstance(ctype, str) and ctype.strip().lower() in CONTENT_TYPES:
        out["type"] = ctype.strip().lower()
    col = raw.get("collection")
    if isinstance(col, str) and col.strip():
        name = col.strip()[:60]
        match = next((c for c in collections if str(c.get("name") or "").lower() == name.lower()), None)
        out["collection"] = match["name"] if match else name
        out["collection_id"] = match["id"] if match else None
        out["new_collection"] = match is None
    out["source"] = "llm"
    return out
