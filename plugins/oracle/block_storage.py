"""Oracle Always Free 200GB block volume as object storage.

The volume is mounted on the ``oracle-arm`` VM. Two access modes (``settings.mode``):

* ``webdav`` (default): an agent endpoint on that VM exposes the mount over WebDAV, reachable only
  through Tailscale, e.g. ``rclone serve webdav /mnt/omnifree-block --addr :8090 --user U --pass P``.
  Settings ``webdav_url``; optional secrets ``webdav_user``/``webdav_password`` (HTTP basic auth).
  Usage comes from the RFC 4331 ``quota-used-bytes``/``quota-available-bytes`` properties when the
  server reports them (rclone does for local disks).
* ``local``: the process runs on the host itself and uses ``settings.mount_path`` directly;
  usage comes from ``shutil.disk_usage``.

Keys are relative paths (``a/b/c.txt``); ``..`` segments are rejected. Plain WebDAV/filesystems
have no custom object metadata, so ``metadata`` passed to ``put`` is not persisted.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from core.interfaces import (
    HealthStatus,
    NotConfigured,
    PluginMeta,
    ProviderError,
    QuotaExhausted,
    QuotaInfo,
    StorageAdapter,
    StoredObject,
    raise_for_response,
    translate_transport_error,
)

_DAV = "{DAV:}"
_PROPFIND_ALL = (
    '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getcontenttype/><d:getetag/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)
_PROPFIND_QUOTA = (
    '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:quota-used-bytes/><d:quota-available-bytes/></d:prop></d:propfind>"
)


def _safe_key(key: str, provider: str) -> str:
    parts = PurePosixPath(key.replace("\\", "/").lstrip("/")).parts
    if not parts or any(p in ("..", ".") for p in parts):
        raise ProviderError(f"invalid key: {key!r}", provider=provider, status_code=400)
    return "/".join(parts)


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


class OracleBlockVolumeStorageAdapter(StorageAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Oracle block volume",
        description="200GB Always Free block volume on oracle-arm (WebDAV agent or local mount).",
        optional_secrets=("webdav_user", "webdav_password"),
        docs_url="https://rclone.org/commands/rclone_serve_webdav/",
    )

    # ------------------------------------------------------------------ mode helpers
    def _mode(self) -> str:
        mode = str(self.setting("mode", "webdav")).lower()
        if mode not in ("webdav", "local"):
            raise NotConfigured("setting 'mode' must be 'webdav' or 'local'", provider=self.name)
        return mode

    def build_http_client(self) -> httpx.AsyncClient:
        user, password = self.secret("webdav_user"), self.secret("webdav_password")
        return httpx.AsyncClient(
            timeout=self.context.timeout_s,
            transport=self.context.http_transport,
            follow_redirects=True,
            auth=httpx.BasicAuth(user, password or "") if user else None,
        )

    def _base(self) -> str:
        return str(self.require_setting("webdav_url")).rstrip("/")

    def _url(self, key: str = "") -> str:
        return f"{self._base()}/{quote(key, safe='/-_.~')}" if key else self._base() + "/"

    def _root(self) -> Path:
        return Path(str(self.require_setting("mount_path")))

    def _local_path(self, key: str) -> Path:
        root = self._root().resolve()
        path = (root / _safe_key(key, self.name)).resolve()
        if root != path and root not in path.parents:
            raise ProviderError(f"invalid key: {key!r}", provider=self.name, status_code=400)
        return path

    async def _dav(self, method: str, url: str, *, allow: tuple[int, ...] = (), **kwargs: Any) -> httpx.Response:
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.name) from exc
        if response.status_code in allow:
            return response
        if response.status_code == 404:
            raise ProviderError(f"not found: {url}", provider=self.name, status_code=404)
        if response.status_code == 507:  # Insufficient Storage
            raise QuotaExhausted("volume full (HTTP 507)", provider=self.name, status_code=507)
        raise_for_response(response, self.name)
        return response

    def _parse_multistatus(self, body: bytes) -> list[tuple[str, bool, StoredObject]]:
        """Return (key, is_collection, object) for each <response>."""
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise ProviderError(f"invalid PROPFIND XML: {exc}", provider=self.name) from exc
        base_path = unquote(urlsplit(self._base()).path).rstrip("/")
        out: list[tuple[str, bool, StoredObject]] = []
        for resp in root.iter(f"{_DAV}response"):
            href = unquote(urlsplit(resp.findtext(f"{_DAV}href") or "").path)
            key = href[len(base_path):] if href.startswith(base_path) else href
            key = key.strip("/")
            prop = None
            for propstat in resp.iter(f"{_DAV}propstat"):
                status = propstat.findtext(f"{_DAV}status") or ""
                if " 200 " in status or status.endswith(" 200"):
                    prop = propstat.find(f"{_DAV}prop")
                    break
            if prop is None:
                prop = resp.find(f".//{_DAV}prop")
            if prop is None:
                continue
            rtype = prop.find(f"{_DAV}resourcetype")
            is_dir = rtype is not None and rtype.find(f"{_DAV}collection") is not None
            length = prop.findtext(f"{_DAV}getcontentlength") or "0"
            out.append(
                (
                    key,
                    is_dir,
                    StoredObject(
                        key=key,
                        size=int(length) if length.isdigit() else 0,
                        content_type=prop.findtext(f"{_DAV}getcontenttype") or None,
                        etag=(prop.findtext(f"{_DAV}getetag") or "").strip('"') or None,
                        last_modified=_parse_http_date(prop.findtext(f"{_DAV}getlastmodified")),
                        backend=self.name,
                    ),
                )
            )
        return out

    async def _mkcols(self, key: str) -> None:
        parts = key.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            # 405 = already exists, 201 = created
            await self._dav("MKCOL", self._url("/".join(parts[:i])) + "/", allow=(201, 405))

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        if self._mode() == "local":
            async def check_local() -> None:
                if not await asyncio.to_thread(self._root().is_dir):
                    raise ProviderError(f"mount_path {self._root()} is not a directory", provider=self.name)
            return await self.probe(check_local)
        return await self.probe(
            lambda: self._dav("PROPFIND", self._url(), headers={"Depth": "0", "Content-Type": "application/xml"},
                              content=_PROPFIND_QUOTA)
        )

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        key = _safe_key(key, self.name)
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        if self._mode() == "local":
            path = self._local_path(key)
            await asyncio.to_thread(self._write_local, path, data)
            return StoredObject(key=key, size=len(data), content_type=ctype, backend=self.name,
                                last_modified=datetime.now(timezone.utc))
        headers = {"Content-Type": ctype}
        response = await self._dav("PUT", self._url(key), content=data, headers=headers, allow=(409,))
        if response.status_code == 409:  # parent collection missing
            await self._mkcols(key)
            response = await self._dav("PUT", self._url(key), content=data, headers=headers)
        return StoredObject(
            key=key,
            size=len(data),
            content_type=ctype,
            etag=(response.headers.get("etag") or "").strip('"') or None,
            last_modified=datetime.now(timezone.utc),
            backend=self.name,
        )

    @staticmethod
    def _write_local(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".omnifree-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    async def get(self, key: str) -> bytes:
        key = _safe_key(key, self.name)
        if self._mode() == "local":
            path = self._local_path(key)
            if not await asyncio.to_thread(path.is_file):
                raise ProviderError(f"not found: {key}", provider=self.name, status_code=404)
            return await asyncio.to_thread(path.read_bytes)
        return (await self._dav("GET", self._url(key))).content

    async def delete(self, key: str) -> None:
        key = _safe_key(key, self.name)
        if self._mode() == "local":
            path = self._local_path(key)
            await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
            return
        await self._dav("DELETE", self._url(key), allow=(404,))

    async def stat(self, key: str) -> StoredObject | None:
        key = _safe_key(key, self.name)
        if self._mode() == "local":
            return await asyncio.to_thread(self._stat_local, key)
        response = await self._dav("PROPFIND", self._url(key), allow=(404,),
                                   headers={"Depth": "0", "Content-Type": "application/xml"}, content=_PROPFIND_ALL)
        if response.status_code == 404:
            return None
        entries = [e for e in self._parse_multistatus(response.content) if not e[1]]
        if not entries:
            return None
        obj = entries[0][2]
        obj.key = key
        return obj

    def _stat_local(self, key: str) -> StoredObject | None:
        path = self._local_path(key)
        if not path.is_file():
            return None
        st = path.stat()
        return StoredObject(
            key=key,
            size=st.st_size,
            content_type=mimetypes.guess_type(key)[0],
            last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            backend=self.name,
        )

    async def list(self, prefix: str = "", limit: int = 1000) -> list[StoredObject]:
        prefix = prefix.replace("\\", "/").lstrip("/")
        if self._mode() == "local":
            return await asyncio.to_thread(self._list_local, prefix, limit)
        start_dir = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        out: list[StoredObject] = []
        pending = [start_dir]
        visited: set[str] = set()
        while pending and len(out) < limit:
            directory = pending.pop(0)
            if directory in visited:
                continue
            visited.add(directory)
            url = self._url(directory) + ("/" if directory else "")
            response = await self._dav("PROPFIND", url, allow=(404,),
                                       headers={"Depth": "1", "Content-Type": "application/xml"},
                                       content=_PROPFIND_ALL)
            if response.status_code == 404:
                continue
            for key, is_dir, obj in self._parse_multistatus(response.content):
                if key == directory:
                    continue
                if is_dir:
                    if key.startswith(prefix) or prefix.startswith(key + "/"):
                        pending.append(key)
                elif key.startswith(prefix):
                    out.append(obj)
        out.sort(key=lambda o: o.key)
        return out[:limit]

    def _list_local(self, prefix: str, limit: int) -> list[StoredObject]:
        root = self._root()
        if not root.is_dir():
            return []
        out: list[StoredObject] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                key = path.relative_to(root).as_posix()
                if filename.startswith(".omnifree-") or not key.startswith(prefix):
                    continue
                st = path.stat()
                out.append(StoredObject(
                    key=key, size=st.st_size, content_type=mimetypes.guess_type(key)[0],
                    last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), backend=self.name,
                ))
        out.sort(key=lambda o: o.key)
        return out[:limit]

    async def quota(self) -> QuotaInfo:
        capacity = self.setting("capacity_bytes")
        if self._mode() == "local":
            usage = await asyncio.to_thread(shutil.disk_usage, self._root())
            return QuotaInfo(provider=self.name, used=float(usage.used), limit=float(usage.total), unit="bytes",
                             window="total", source="provider", extra={"free": usage.free})
        response = await self._dav("PROPFIND", self._url(), headers={"Depth": "0", "Content-Type": "application/xml"},
                                   content=_PROPFIND_QUOTA)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ProviderError(f"invalid PROPFIND XML: {exc}", provider=self.name) from exc
        used_text = root.findtext(f".//{_DAV}quota-used-bytes")
        avail_text = root.findtext(f".//{_DAV}quota-available-bytes")
        if used_text and used_text.strip().isdigit():
            used = float(used_text)
            if avail_text and avail_text.strip().isdigit():
                limit = used + float(avail_text)
            else:
                limit = float(capacity) if capacity is not None else None
            return QuotaInfo(provider=self.name, used=used, limit=limit, unit="bytes", window="total",
                             source="provider")
        return await super().quota()
