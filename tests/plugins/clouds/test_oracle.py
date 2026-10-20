from __future__ import annotations

import base64
import re
import sys
import types
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.sax.saxutils import escape

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from core.interfaces import (
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderKind,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaLimit,
)
from plugins.oracle._signer import OCISigner, normalize_pem
from plugins.oracle.autonomous_db import OracleAutonomousDBAdapter, _clean_sql
from plugins.oracle.block_storage import OracleBlockVolumeStorageAdapter
from plugins.oracle.compute import OCIComputeAdapter
from tests.plugins.clouds.helpers import Recorder, jresp

# ----------------------------------------------------------------------------- OCI signer / compute


def verify_oci_signature(request: httpx.Request, public_key: Any) -> None:
    auth = request.headers["authorization"]
    fields = dict(re.findall(r'(\w+)="([^"]*)"', auth))
    assert fields["version"] == "1" and fields["algorithm"] == "rsa-sha256"
    assert fields["keyId"] == "ocid1.tenancy.oc1..t/ocid1.user.oc1..u/aa:bb"
    names = fields["headers"].split(" ")
    if request.method in ("POST", "PUT"):
        assert names == ["date", "(request-target)", "host", "content-length", "content-type", "x-content-sha256"]
        import hashlib

        assert request.headers["x-content-sha256"] == base64.b64encode(hashlib.sha256(request.content).digest()).decode()
        assert request.headers["content-length"] == str(len(request.content))
    else:
        assert names == ["date", "(request-target)", "host"]
    lines = []
    for name in names:
        if name == "(request-target)":
            lines.append(f"(request-target): {request.method.lower()} {request.url.raw_path.decode()}")
        else:
            lines.append(f"{name}: {request.headers[name]}")
    public_key.verify(base64.b64decode(fields["signature"]), "\n".join(lines).encode(), padding.PKCS1v15(),
                      hashes.SHA256())


def test_normalize_pem(rsa_pem) -> None:
    assert normalize_pem(rsa_pem.replace("\n", "\\n")) == normalize_pem(rsa_pem)
    assert normalize_pem(base64.b64encode(rsa_pem.encode()).decode()).startswith("-----BEGIN PRIVATE KEY-----")


OCI_SECRETS_BASE = {"tenancy_ocid": "ocid1.tenancy.oc1..t", "user_ocid": "ocid1.user.oc1..u", "fingerprint": "aa:bb"}

ARM = {"id": "ocid1.instance.oc1.eu-frankfurt-1.arm", "displayName": "oracle-arm", "lifecycleState": "RUNNING",
       "shape": "VM.Standard.A1.Flex", "region": "eu-frankfurt-1", "availabilityDomain": "AD-1",
       "shapeConfig": {"ocpus": 2.0, "memoryInGBs": 12.0}, "freeformTags": {"env": "prod"}}
VM1 = {"id": "ocid1.instance.oc1.eu-frankfurt-1.vm1", "displayName": "amd-vm1", "lifecycleState": "STOPPED",
       "shape": "VM.Standard.E2.1.Micro", "region": "eu-frankfurt-1"}
DEAD = {"id": "ocid1.instance.oc1.eu-frankfurt-1.dead", "displayName": "old", "lifecycleState": "TERMINATED"}


async def test_oci_compute(make_ctx, rsa_pem, rsa_key) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        verify_oci_signature(request, rsa_key.public_key())
        assert request.url.host == "iaas.eu-frankfurt-1.oraclecloud.com"
        path, params = request.url.path, request.url.params
        if path == "/20160918/instances":
            assert params["compartmentId"] == "ocid1.tenancy.oc1..t"
            if params.get("page") == "p2":
                return jresp([VM1, DEAD])
            return jresp([ARM], headers={"opc-next-page": "p2"})
        if path == "/20160918/vnicAttachments":
            if params["instanceId"] == ARM["id"]:
                return jresp([{"lifecycleState": "ATTACHED", "vnicId": "ocid1.vnic.arm"}])
            return jresp([])
        if path == "/20160918/vnics/ocid1.vnic.arm":
            return jresp({"publicIp": "130.61.0.1", "privateIp": "10.0.0.5"})
        if path == f"/20160918/instances/{VM1['id']}" and request.method == "GET":
            return jresp(VM1)
        if path == f"/20160918/instances/{VM1['id']}" and request.method == "POST":
            action = params["action"]
            return jresp({**VM1, "lifecycleState": "STARTING" if action == "START" else "STOPPING"})
        if path == "/20160918/instances/ocid1.instance.oc1..missing":
            return jresp({"code": "NotAuthorizedOrNotFound"}, status=404)
        raise AssertionError(f"{request.method} {path}")

    rec = Recorder(handler)
    adapter = OCIComputeAdapter(make_ctx(
        "oracle_compute", ProviderKind.COMPUTE,
        settings={"region": "eu-frankfurt-1", "instances": {"oracle-arm": {"role": "llm-host"}}},
        secrets={**OCI_SECRETS_BASE, "private_key": rsa_pem.replace("\n", "\\n")}, handler=rec))
    instances = await adapter.list_instances()
    assert [i.name for i in instances] == ["oracle-arm", "amd-vm1"]  # terminated hidden
    arm = instances[0]
    assert arm.state == "running" and arm.public_ip == "130.61.0.1" and arm.private_ip == "10.0.0.5"
    assert arm.tags == {"env": "prod", "role": "llm-host"} and arm.meta["ocpus"] == 2.0
    assert instances[1].state == "stopped" and instances[1].public_ip is None
    assert (await adapter.status(VM1["id"])).name == "amd-vm1"
    started = await adapter.start("amd-vm1")  # by display name
    assert started.state == "starting"
    stopped = await adapter.stop(VM1["id"])
    assert stopped.state == "stopping"
    actions = [r.url.params["action"] for r in rec.requests if r.method == "POST"]
    assert actions == ["START", "SOFTSTOP"]
    with pytest.raises(ProviderError) as exc:
        await adapter.status("ocid1.instance.oc1..missing")
    assert exc.value.status_code == 404
    with pytest.raises(ProviderError):
        await adapter.status("no-such-name")
    assert (await adapter.health()).ok


async def test_oci_compute_errors(make_ctx, rsa_pem) -> None:
    adapter = OCIComputeAdapter(make_ctx("oracle_compute", ProviderKind.COMPUTE, settings={"region": "r"},
                                secrets=OCI_SECRETS_BASE))
    with pytest.raises(NotConfigured):
        await adapter.list_instances()
    rec = Recorder(lambda r: jresp({"code": "NotAuthenticated"}, status=401))
    adapter = OCIComputeAdapter(make_ctx("oracle_compute", ProviderKind.COMPUTE, settings={"region": "r"},
                                secrets={**OCI_SECRETS_BASE, "private_key": rsa_pem}, handler=rec))
    with pytest.raises(ProviderAuthError):
        await adapter.list_instances()
    bad_key = OCIComputeAdapter(make_ctx("oracle_compute", ProviderKind.COMPUTE, settings={"region": "r"},
                                secrets={**OCI_SECRETS_BASE, "private_key": "garbage"}, handler=rec))
    with pytest.raises(ProviderError):
        await bad_key.list_instances()


def test_signer_body_headers(rsa_pem, rsa_key) -> None:
    signer = OCISigner("ocid1.tenancy.oc1..t", "ocid1.user.oc1..u", "aa:bb", rsa_pem)
    headers = signer.sign("POST", "https://iaas.r.oraclecloud.com/20160918/instances/x?action=START", b"",
                          date="Thu, 05 Jan 2014 21:31:40 GMT")
    request = httpx.Request("POST", "https://iaas.r.oraclecloud.com/20160918/instances/x?action=START",
                            headers=headers, content=b"")
    verify_oci_signature(request, rsa_key.public_key())


# ----------------------------------------------------------------------------- Autonomous DB (fake oracledb)


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.description: list[tuple[str]] | None = None
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.db.executed.append((sql, params))
        if "boom" in sql:
            raise self.db.module.DatabaseError("ORA-00942: table or view does not exist")
        if "authfail" in sql:
            raise self.db.module.DatabaseError("ORA-01017: invalid credential or not authorized")
        if "netfail" in sql:
            raise self.db.module.OperationalError("DPY-6005: cannot connect to database")
        for pattern, (cols, rows) in self.db.responses.items():
            if pattern in sql:
                self.description = [(c,) for c in cols] if cols else None
                self._rows = list(rows)
                if not cols:
                    self.rowcount = len(rows) or 3
                return
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchmany(self, n: int) -> list[tuple[Any, ...]]:
        return self._rows[:n]


class FakeConnection:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.call_timeout = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    async def commit(self) -> None:
        self.db.commits += 1

    async def rollback(self) -> None:
        self.db.rollbacks += 1


class FakeAcquire:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    async def __aenter__(self) -> FakeConnection:
        return FakeConnection(self.db)

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakePool:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self.closed = False

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.db)

    async def close(self, force: bool = False) -> None:
        self.closed = True


class FakeDB:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.pool_kwargs: dict[str, Any] = {}
        self.pools: list[FakePool] = []
        self.responses: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {
            "FROM dual": (["1"], [(1,)]),
            "FROM user_tables UNION ALL": (["TABLE_NAME", "NUM_ROWS", "'TABLE'"],
                                           [("NOTES", 10, "table"), ("V_NOTES", None, "view")]),
            "FROM user_tab_columns": (["COLUMN_NAME", "DATA_TYPE", "DATA_LENGTH", "DATA_PRECISION", "DATA_SCALE",
                                       "NULLABLE", "DATA_DEFAULT"],
                                      [("ID", "NUMBER", 22, 10, 0, "N", None),
                                       ("BODY", "VARCHAR2", 4000, None, None, "Y", "'x' ")]),
            "constraint_type = 'P'": (["COLUMN_NAME"], [("ID",)]),
            "FROM user_tables t WHERE": (["NUM_ROWS", "BYTES"], [(10, 65536)]),
            "NVL(SUM(bytes), 0)": (["SUM"], [(1048576,)]),
            "SELECT * FROM notes": (["ID", "BODY"], [(i, f"n{i}") for i in range(5)]),
            "INSERT INTO notes": ([], []),
        }
        module = types.ModuleType("oracledb")

        class Error(Exception):
            pass

        class DatabaseError(Error):
            pass

        class OperationalError(DatabaseError):
            pass

        module.Error = Error
        module.DatabaseError = DatabaseError
        module.OperationalError = OperationalError
        module.defaults = types.SimpleNamespace(fetch_lobs=True)

        def create_pool_async(**kwargs: Any) -> FakePool:
            self.pool_kwargs = kwargs
            pool = FakePool(self)
            self.pools.append(pool)
            return pool

        module.create_pool_async = create_pool_async
        self.module = module


ADB_SECRETS = {"user": "ADMIN", "password": "pw", "dsn": "(description=(address=(protocol=tcps)))"}


@pytest.fixture
def fake_oracledb(monkeypatch) -> FakeDB:
    db = FakeDB()
    monkeypatch.setitem(sys.modules, "oracledb", db.module)
    return db


async def test_adb_operations(make_ctx, fake_oracledb) -> None:
    adapter = OracleAutonomousDBAdapter(make_ctx(
        "oracle_adb_1", ProviderKind.DB, settings={"wallet_dir": None}, secrets=ADB_SECRETS,
        limits=[QuotaLimit(metric="bytes", window="total", limit=21474836480)]))
    assert (await adapter.health()).ok
    assert fake_oracledb.module.defaults.fetch_lobs is False
    assert fake_oracledb.pool_kwargs["dsn"] == ADB_SECRETS["dsn"] and "wallet_location" not in fake_oracledb.pool_kwargs
    tables = await adapter.list_tables()
    assert [(t.name, t.kind, t.row_count) for t in tables] == [("NOTES", "table", 10), ("V_NOTES", "view", None)]
    info = await adapter.describe("notes")
    assert info.name == "NOTES" and info.size_bytes == 65536 and info.row_count == 10
    assert info.columns[0].primary_key and info.columns[0].type == "NUMBER(10,0)" and not info.columns[0].nullable
    assert info.columns[1].type == "VARCHAR2(4000)" and info.columns[1].default == "'x'"
    res = await adapter.query("SELECT * FROM notes WHERE id > :id;", {"id": 0}, limit=3)
    assert res.columns == ["ID", "BODY"] and res.row_count == 3 and res.truncated
    assert fake_oracledb.executed[-1] == ("SELECT * FROM notes WHERE id > :id", {"id": 0})
    out = await adapter.execute("INSERT INTO notes VALUES (:1, :2)", [9, "z"])
    assert out.affected_rows == 3 and fake_oracledb.commits == 1
    usage = await adapter.usage()
    assert usage.used == 1048576 and usage.limit == 21474836480 and usage.source == "provider"
    await adapter.close()
    assert fake_oracledb.pools[0].closed


async def test_adb_errors_and_wallet(make_ctx, fake_oracledb, monkeypatch) -> None:
    adapter = OracleAutonomousDBAdapter(make_ctx("oracle_adb_2", ProviderKind.DB,
                                        settings={"wallet_dir": "/opt/wallet"},
                                        secrets={**ADB_SECRETS, "wallet_password": "wpw"}))
    with pytest.raises(ProviderError) as exc:
        await adapter.query("SELECT boom FROM x")
    assert exc.value.status_code == 400 and "ORA-00942" in str(exc.value)
    assert fake_oracledb.pool_kwargs["wallet_location"] == "/opt/wallet"
    assert fake_oracledb.pool_kwargs["wallet_password"] == "wpw"
    with pytest.raises(ProviderAuthError):
        await adapter.query("SELECT authfail FROM x")
    with pytest.raises(ProviderUnavailable):
        await adapter.query("SELECT netfail FROM x")
    with pytest.raises(NotConfigured):
        await OracleAutonomousDBAdapter(make_ctx("oracle_adb_2", ProviderKind.DB, secrets={"user": "u"})).query("x")
    monkeypatch.setitem(sys.modules, "oracledb", None)
    missing = OracleAutonomousDBAdapter(make_ctx("oracle_adb_2", ProviderKind.DB, secrets=ADB_SECRETS))
    with pytest.raises(NotConfigured, match="pip install oracledb"):
        await missing.query("SELECT 1 FROM dual")
    assert not (await missing.health()).ok


def test_clean_sql() -> None:
    assert _clean_sql("SELECT 1 FROM dual;") == "SELECT 1 FROM dual"
    assert _clean_sql("BEGIN NULL; END;") == "BEGIN NULL; END;"


# ----------------------------------------------------------------------------- block volume: local mode


async def test_block_storage_local(make_ctx, tmp_path: Path) -> None:
    adapter = OracleBlockVolumeStorageAdapter(make_ctx("oracle_block_storage", ProviderKind.STORAGE,
                                              settings={"mode": "local", "mount_path": str(tmp_path)}))
    assert (await adapter.health()).ok
    obj = await adapter.put("music/song.mp3", b"abc")
    assert obj.size == 3 and obj.content_type == "audio/mpeg"
    assert (tmp_path / "music" / "song.mp3").read_bytes() == b"abc"
    await adapter.put("docs/x.txt", b"hello")
    assert await adapter.get("docs/x.txt") == b"hello"
    stat = await adapter.stat("docs/x.txt")
    assert stat is not None and stat.size == 5
    assert await adapter.stat("docs/none.txt") is None
    assert [o.key for o in await adapter.list()] == ["docs/x.txt", "music/song.mp3"]
    assert [o.key for o in await adapter.list("music/")] == ["music/song.mp3"]
    with pytest.raises(ProviderError) as exc:
        await adapter.get("docs/none.txt")
    assert exc.value.status_code == 404
    with pytest.raises(ProviderError):
        await adapter.put("../escape.txt", b"x")
    quota = await adapter.quota()
    assert quota.source == "provider" and quota.limit and quota.used > 0
    await adapter.delete("docs/x.txt")
    await adapter.delete("docs/x.txt")
    assert not (tmp_path / "docs" / "x.txt").exists()


# ----------------------------------------------------------------------------- block volume: WebDAV mode


class FakeWebDAV:
    """Minimal in-memory WebDAV server rooted at /dav."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {""}

    def _propresponse(self, path: str, is_dir: bool) -> str:
        href = "/dav/" + path + ("/" if is_dir and path else "")
        props = "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else (
            f"<d:resourcetype/><d:getcontentlength>{len(self.files[path])}</d:getcontentlength>"
            "<d:getcontenttype>text/plain</d:getcontenttype><d:getetag>\"e\"</d:getetag>"
            "<d:getlastmodified>Mon, 19 Oct 2026 10:00:00 GMT</d:getlastmodified>")
        return (f"<d:response><d:href>{escape(href.replace(' ', '%20'))}</d:href><d:propstat><d:prop>{props}</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"dav:secret").decode()
        path = unquote(request.url.path)
        assert path.startswith("/dav")
        key = path[len("/dav"):].strip("/")
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        if request.method == "PUT":
            if parent not in self.dirs:
                return httpx.Response(409)
            self.files[key] = request.content
            return httpx.Response(201, headers={"etag": '"new"'})
        if request.method == "MKCOL":
            if key in self.dirs:
                return httpx.Response(405)
            self.dirs.add(key)
            return httpx.Response(201)
        if request.method == "GET":
            return httpx.Response(200, content=self.files[key]) if key in self.files else httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(204) if self.files.pop(key, None) is not None else httpx.Response(404)
        if request.method == "PROPFIND":
            body = request.content.decode()
            if "quota-used-bytes" in body:
                used = sum(len(v) for v in self.files.values())
                xml = ('<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/</d:href><d:propstat><d:prop>'
                       f"<d:quota-used-bytes>{used}</d:quota-used-bytes>"
                       "<d:quota-available-bytes>1000</d:quota-available-bytes>"
                       "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>")
                return httpx.Response(207, content=xml.encode())
            if key in self.files:
                responses = [self._propresponse(key, False)]
            elif key in self.dirs:
                responses = [self._propresponse(key, True)]
                if request.headers.get("depth") == "1":
                    prefix = key + "/" if key else ""
                    for d in sorted(self.dirs):
                        if d and d.startswith(prefix) and "/" not in d[len(prefix):]:
                            responses.append(self._propresponse(d, True))
                    for f in sorted(self.files):
                        if f.startswith(prefix) and "/" not in f[len(prefix):]:
                            responses.append(self._propresponse(f, False))
            else:
                return httpx.Response(404)
            return httpx.Response(207, content=('<d:multistatus xmlns:d="DAV:">' + "".join(responses)
                                                + "</d:multistatus>").encode())
        return httpx.Response(405)


async def test_block_storage_webdav(make_ctx) -> None:
    dav = FakeWebDAV()
    rec = Recorder(dav)
    adapter = OracleBlockVolumeStorageAdapter(make_ctx(
        "oracle_block_storage", ProviderKind.STORAGE,
        settings={"mode": "webdav", "webdav_url": "http://oracle-arm:8090/dav", "capacity_bytes": 2000},
        secrets={"webdav_user": "dav", "webdav_password": "secret"}, handler=rec))
    assert (await adapter.health()).ok
    obj = await adapter.put("a/b/hello world.txt", b"hi there", "text/plain")
    assert obj.etag == "new" and dav.files["a/b/hello world.txt"] == b"hi there"
    assert "MKCOL /dav/a/" in rec.paths() and "MKCOL /dav/a/b/" in rec.paths()
    await adapter.put("top.txt", b"x")
    assert await adapter.get("a/b/hello world.txt") == b"hi there"
    stat = await adapter.stat("a/b/hello world.txt")
    assert stat is not None and stat.size == 8 and stat.key == "a/b/hello world.txt" and stat.last_modified
    assert await adapter.stat("nope.txt") is None
    assert [o.key for o in await adapter.list()] == ["a/b/hello world.txt", "top.txt"]
    assert [o.key for o in await adapter.list("a/b/")] == ["a/b/hello world.txt"]
    assert [o.key for o in await adapter.list("zzz/")] == []
    quota = await adapter.quota()
    assert quota.used == 9 and quota.limit == 1009 and quota.source == "provider"
    await adapter.delete("top.txt")
    await adapter.delete("top.txt")
    with pytest.raises(ProviderError) as exc:
        await adapter.get("top.txt")
    assert exc.value.status_code == 404


async def test_block_storage_webdav_full_and_bad_mode(make_ctx) -> None:
    rec = Recorder(lambda r: httpx.Response(507))
    adapter = OracleBlockVolumeStorageAdapter(make_ctx("oracle_block_storage", ProviderKind.STORAGE,
                                              settings={"webdav_url": "http://h/dav"}, handler=rec))
    with pytest.raises(QuotaExhausted):
        await adapter.put("x.txt", b"x")
    bad = OracleBlockVolumeStorageAdapter(make_ctx("oracle_block_storage", ProviderKind.STORAGE,
                                          settings={"mode": "ftp"}))
    with pytest.raises(NotConfigured):
        await bad.get("x")
