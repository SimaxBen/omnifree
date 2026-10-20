from __future__ import annotations

import base64
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from automation import deploy as dp
from automation.deploy import Deployer, check_chat_response, generate_ssh_keypair, render_qr, verify_failover
from automation.deploy_common import (
    DeployConfig,
    DeployError,
    DeployState,
    build_env_file,
    compose_variables,
    make_bundle,
    render_cloud_init,
)
from tests.automation.fakes import FakeCloud, FakeRemote, FakeRunner, fake_oci_module, write_file

ROOT = Path(__file__).resolve().parents[2]
CHAIN = ["freellmapi", "groq", "cerebras", "gemini", "ollama"]


# ---------------------------------------------------------------------- deploy assets


def test_cloud_init_files_are_valid_and_secret_free() -> None:
    for path in sorted((ROOT / "deploy" / "cloud-init").glob("*.yaml")):
        decoded = base64.b64decode(render_cloud_init(path)).decode("utf-8")
        assert decoded.startswith("#cloud-config")
        doc = yaml.safe_load(decoded)
        assert "runcmd" in doc
        assert "tskey" not in decoded and "${" not in decoded


def test_compose_files_reference_vault_keys() -> None:
    pihole = compose_variables((ROOT / "deploy/pihole/docker-compose.yml").read_text(encoding="utf-8"))
    assert pihole["PIHOLE_PASSWORD"].required and pihole["BIND_IP"].required
    assert pihole["PIHOLE_IMAGE"].has_default
    frps = compose_variables((ROOT / "deploy/frps/docker-compose.yml").read_text(encoding="utf-8"))
    assert frps["FRPS_TOKEN"].required
    assert "FRPS_TOKEN" in (ROOT / "deploy/frps/frps.toml").read_text(encoding="utf-8")
    instinct = compose_variables((ROOT / "deploy/instinct/docker-compose.yml").read_text(encoding="utf-8"))
    assert instinct["HOST_BIND"].required
    for f in ("deploy/blockstore/docker-compose.yml", "deploy/healthmonitor/docker-compose.yml",
              "deploy/hub/cloudbuild.yaml", "deploy/deploy.yaml"):
        assert yaml.safe_load((ROOT / f).read_text(encoding="utf-8"))


def test_compose_dollar_escapes_are_ignored() -> None:
    assert compose_variables("cmd: for m in $${MODELS}; do echo $$m; done ${REAL:-x}") .keys() == {"REAL"}


def test_build_env_file_quotes_and_requires() -> None:
    compose = "a: ${NEEDED:?x}\nb: ${OPT:-d}\nc: ${PLAIN}\n"
    text, unset = build_env_file(compose, {"NEEDED": "p@ss word'q", "PLAIN": "v"}.get, {"EXTRA": "1"})
    assert "NEEDED='p@ss word'\"'\"'q'" in text and "PLAIN=v" in text and "EXTRA=1" in text
    assert unset == ["OPT"]
    with pytest.raises(DeployError, match="NEEDED"):
        build_env_file(compose, {"PLAIN": "v"}.get)


def test_make_bundle_skips_caches_and_secrets(tmp_path: Path) -> None:
    write_file(tmp_path / "core" / "a.py", "x = 1\n")
    write_file(tmp_path / "core" / "__pycache__" / "a.cpython-312.pyc", "junk")
    write_file(tmp_path / "core" / ".env", "SECRET=1")
    data, missing = make_bundle(tmp_path, ["core", "instinct"])
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert names == ["core/a.py"] and missing == ["instinct"]


# ---------------------------------------------------------------------- helpers


def _deployer(tmp_path: Path, secrets: dict[str, str], *, remote: FakeRemote | None = None,
              runner: FakeRunner | None = None, transport: httpx.MockTransport | None = None,
              root: Path = ROOT, state: DeployState | None = None) -> tuple[Deployer, list[str]]:
    cfg = DeployConfig.load(ROOT / "deploy" / "deploy.yaml", ROOT / "config" / "providers.yaml", root=root)
    cfg.deploy["ssh"] = {"user": "ubuntu", "private_key": str(tmp_path / "id"), "public_key": str(tmp_path / "id.pub")}
    hosts: list[str] = []

    async def factory(address: str, user: str, key: Path) -> FakeRemote:
        hosts.append(address)
        assert remote is not None
        remote.host = address
        return remote

    echo: list[str] = []
    d = Deployer(cfg, state or DeployState(tmp_path / "state.json"), secrets.get, runner=runner or FakeRunner(),  # type: ignore[arg-type]
                 echo=echo.append, remote_factory=factory, http_transport=transport)
    return d, hosts


# ---------------------------------------------------------------------- verify


def test_check_chat_response_rules() -> None:
    h = httpx.Headers({"X-Routed-Via": "groq"})
    routed, failed, v = check_chat_response(200, h, {"routed_via": "groq", "attempts": ["freellmapi:quota (429)"]},
                                            CHAIN, enforce_order=True)
    assert routed == "groq" and failed == ["freellmapi"] and v == []
    _, _, v = check_chat_response(200, h, {"routed_via": "groq", "attempts": ["gemini:unavailable"]}, CHAIN,
                                  enforce_order=True)
    assert any("out of order" in x for x in v)
    _, _, v = check_chat_response(200, httpx.Headers(), {"routed_via": "groq"}, CHAIN, enforce_order=True)
    assert any("missing X-Routed-Via" in x for x in v)
    _, _, v = check_chat_response(200, httpx.Headers({"X-Routed-Via": "freellmapi:groq"}),
                                  {"routed_via": "freellmapi:groq"}, CHAIN, enforce_order=True)
    assert v == []


async def test_verify_failover_against_mock_hub() -> None:
    seen: list[dict[str, Any]] = []
    responses = iter([
        ("freellmapi:groq", []),
        ("groq", ["freellmapi:quota (rate limited)"]),
        ("gemini", ["freellmapi:quota", "groq:quota", "cerebras:not_configured (missing)"]),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/llm/chat"
        assert request.headers["authorization"] == "Bearer tok"
        seen.append(json.loads(request.content))
        routed, attempts = next(responses)
        return httpx.Response(200, headers={"X-Routed-Via": routed},
                              json={"model": "m", "content": "OK", "routed_via": routed, "attempts": attempts})

    report = await verify_failover("https://hub.example", "tok", CHAIN, count=3,
                                   transport=httpx.MockTransport(handler), echo=lambda _: None)
    assert report.passed, report.summary()
    assert report.failovers == 2 and report.routed["gemini"] == 1
    assert seen[0]["max_tokens"] == 8


async def test_verify_reports_http_failures() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={"detail": "no provider"}))
    report = await verify_failover("https://hub", None, CHAIN, count=2, transport=transport, echo=lambda _: None)
    assert not report.passed and len(report.failures) == 2


async def test_deployer_verify_expect_failover(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(
        200, headers={"X-Routed-Via": "freellmapi"}, json={"model": "m", "routed_via": "freellmapi", "attempts": []}))
    d, _ = _deployer(tmp_path, {"HUB_API_TOKEN": "t"}, transport=transport)
    report = await d.verify(hub_url="https://hub", count=2, expect_failover=True)
    assert not report.passed and "expected at least one failover" in report.violations[0]


# ---------------------------------------------------------------------- qr + ssh keys


def test_render_qr_with_fake_qrcode(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeQR:
        def __init__(self, **kw: Any) -> None:
            self.data = ""

        def add_data(self, data: str) -> None:
            self.data = data

        def make(self, fit: bool) -> None:
            return None

        def get_matrix(self) -> list[list[bool]]:
            return [[True, False], [False, True]]

    fake = SimpleNamespace(QRCode=FakeQR, constants=SimpleNamespace(ERROR_CORRECT_M=0))
    monkeypatch.setitem(sys.modules, "qrcode", fake)
    out = io.StringIO()
    render_qr("https://omnifree-edge.example.workers.dev", out=out)
    lines = out.getvalue().splitlines()
    assert lines[0] == "https://omnifree-edge.example.workers.dev"
    assert lines[2] in ("██  ", "##  ")


def test_render_qr_real_library() -> None:
    pytest.importorskip("qrcode")
    out = io.StringIO()
    render_qr("https://example.org", out=out)
    assert len(out.getvalue().splitlines()) > 20


def test_render_qr_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "qrcode", None)
    with pytest.raises(DeployError, match="pip install qrcode"):
        render_qr("https://x", out=io.StringIO())


def test_generate_ssh_keypair(tmp_path: Path) -> None:
    pub = generate_ssh_keypair(tmp_path / "k", tmp_path / "k.pub")
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(" omnifree")
    assert b"OPENSSH PRIVATE KEY" in (tmp_path / "k").read_bytes()
    assert generate_ssh_keypair(tmp_path / "k", tmp_path / "k.pub") == pub  # idempotent


# ---------------------------------------------------------------------- OCI


async def test_oci_provision_creates_network_instances_and_volume(tmp_path: Path) -> None:
    cloud = FakeCloud(capacity_failures=1, existing=["amd-vm1"])
    secrets = {"OCI_TENANCY_OCID": "ocid1.tenancy.oc1..t", "OCI_USER_OCID": "ocid1.user.oc1..u",
               "OCI_FINGERPRINT": "aa:bb", "OCI_PRIVATE_KEY_PEM": "-----BEGIN PRIVATE KEY-----"}
    d, _ = _deployer(tmp_path, secrets)
    write_file(tmp_path / "id.pub", "ssh-ed25519 AAAA test")
    results = await d.oci_provision(oci_module=fake_oci_module(cloud))
    launched = {x.display_name: x for x in cloud.launched}
    assert set(launched) == {"oracle-arm", "amd-vm2"}  # amd-vm1 already existed
    arm = launched["oracle-arm"]
    assert arm.availability_domain == "AD-2"  # AD-1 was out of capacity
    assert arm.shape_config.ocpus == 2 and arm.shape_config.memory_in_gbs == 12
    assert arm.metadata["ssh_authorized_keys"] == "ssh-ed25519 AAAA test"
    assert base64.b64decode(arm.metadata["user_data"]).startswith(b"#cloud-config")
    assert arm.create_vnic_details.hostname_label == "oraclearm"
    ports = sorted(r.tcp_options.destination_port_range.min for r in cloud.security_updates[0].ingress_security_rules
                   if getattr(r, "tcp_options", None))
    assert ports == [22, 7000]
    assert cloud.attachments[0].device == "/dev/oracleoci/oraclevdb"
    assert results["oracle-arm"]["public_ip"] == "203.0.113.10"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["instances"]["oracle-arm"]["id"] == "ocid1.instance.oracle-arm"
    assert state["block_volume"]["id"] == "vol1"

    await d.lock_ssh(oci_module=fake_oci_module(cloud))
    last = cloud.security_updates[-1].ingress_security_rules
    assert not any(getattr(r, "tcp_options", None) and r.tcp_options.destination_port_range.min == 22 for r in last)


def test_always_free_a1_is_two_ocpu_twelve_gb() -> None:
    from automation.deploy_oci import A1_SHAPE, check_always_free_a1

    cfg = yaml.safe_load((ROOT / "deploy" / "deploy.yaml").read_text(encoding="utf-8"))
    arm = cfg["oci"]["instances"]["oracle-arm"]
    assert (arm["shape"], arm["ocpus"], arm["memory_gb"]) == (A1_SHAPE, 2, 12)
    check_always_free_a1(cfg["oci"]["instances"])  # the shipped layout fits
    check_always_free_a1({"a": {"shape": A1_SHAPE, "ocpus": 1, "memory_gb": 6},
                          "b": {"shape": A1_SHAPE, "ocpus": 1, "memory_gb": 6}})
    with pytest.raises(DeployError, match="2 OCPU / 12 GB"):
        check_always_free_a1({"arm": {"shape": A1_SHAPE, "ocpus": 4, "memory_gb": 24}})
    with pytest.raises(DeployError):
        check_always_free_a1({"a": {"shape": A1_SHAPE}, "b": {"shape": A1_SHAPE, "ocpus": 1, "memory_gb": 1}})
    assert cfg["monitor"]["gce_instance"] == "gcp-monitor"


async def test_oci_missing_secrets(tmp_path: Path) -> None:
    d, _ = _deployer(tmp_path, {})
    write_file(tmp_path / "id.pub", "ssh-ed25519 AAAA test")
    with pytest.raises(DeployError, match="OCI_TENANCY_OCID"):
        await d.oci_provision(oci_module=fake_oci_module(FakeCloud()))


# ---------------------------------------------------------------------- SSH steps


async def test_ssh_bootstrap_never_puts_authkey_on_command_line(tmp_path: Path) -> None:
    remote = FakeRemote()
    state = DeployState(tmp_path / "state.json", {"instances": {"oracle-arm": {"public_ip": "203.0.113.10"}}, "urls": {}})
    d, hosts = _deployer(tmp_path, {"TAILSCALE_AUTHKEY": "tskey-auth-SECRET123"}, remote=remote, state=state)
    remote.responses["tailscale status"] = SimpleNamespace(exit_status=1, stdout="", stderr="")  # type: ignore[assignment]
    await d.ssh_bootstrap(["oracle-arm"], via="public")
    assert hosts == ["203.0.113.10"]
    assert "tskey-auth-SECRET123" not in remote.all_text()
    data, mode = remote.files["/tmp/omnifree-ts-authkey"]
    assert data == b"tskey-auth-SECRET123" and mode == 0o600
    text = remote.all_text()
    assert "tailscale up --auth-key=file:/tmp/omnifree-ts-authkey --hostname=oracle-arm" in text
    assert "mkfs.ext4" in text and "/dev/oracleoci/oraclevdb" in text
    assert state.instances["oracle-arm"]["tailscale_ip"] == "100.64.0.7"


async def test_deploy_pihole_writes_env_and_sets_tailnet_dns(tmp_path: Path) -> None:
    remote = FakeRemote()
    calls: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer tskey-api-x"
        return httpx.Response(200, json={})

    d, hosts = _deployer(tmp_path, {"PIHOLE_PASSWORD": "hunter2", "TAILSCALE_API_KEY": "tskey-api-x"},
                         remote=remote, transport=httpx.MockTransport(handler))
    ip = await d.deploy_pihole(set_tailnet_dns=True)
    assert ip == "100.64.0.7" and hosts == ["amd-vm1"]
    env, mode = remote.files["/opt/omnifree/deploy/pihole/.env"]
    assert mode == 0o600 and b"PIHOLE_PASSWORD=hunter2" in env and b"BIND_IP=100.64.0.7" in env
    assert any("docker compose up -d --remove-orphans" in c for c, _ in remote.commands)
    assert "hunter2" not in remote.all_text()
    assert calls == [("/api/v2/tailnet/-/dns/nameservers", {"dns": ["100.64.0.7"]}),
                     ("/api/v2/tailnet/-/dns/preferences", {"magicDNS": True})]


async def test_deploy_edge_with_wireguard_profile(tmp_path: Path) -> None:
    remote = FakeRemote()
    state = DeployState(tmp_path / "s.json", {"instances": {"amd-vm2": {"public_ip": "198.51.100.2"}}, "urls": {}})
    d, _ = _deployer(tmp_path, {"FRPS_TOKEN": "frp-secret"}, remote=remote, state=state)
    await d.deploy_edge(wireguard=True)
    env, _ = remote.files["/opt/omnifree/deploy/frps/.env"]
    assert b"FRPS_TOKEN=frp-secret" in env and b"WG_SERVERURL=198.51.100.2" in env
    assert any("--profile wireguard up -d" in c for c, _ in remote.commands)


async def test_deploy_arm_renders_injector_and_starts_stacks(tmp_path: Path) -> None:
    remote = FakeRemote()

    def side_effect(argv: list[str]) -> None:
        if "render" in argv:
            write_file(Path(argv[argv.index("--out") + 1]), '{"keys": []}')
            write_file(Path(argv[argv.index("--env-out") + 1]), "ENCRYPTION_KEY=abc\nHOST_BIND=100.64.0.7\n")

    runner = FakeRunner(side_effect=side_effect)
    secrets = {"FREELLMAPI_ENCRYPTION_KEY": "e" * 64, "INSTINCT_API_TOKEN": "inst", "HUB_API_TOKEN": "hubtok",
               "FREELLMAPI_API_KEY": "freellmapi-abc", "ORACLE_BLOCK_WEBDAV_USER": "u",
               "ORACLE_BLOCK_WEBDAV_PASSWORD": "p"}
    d, hosts = _deployer(tmp_path, secrets, remote=remote, runner=runner)
    await d.deploy_arm(hub_url="https://hub.run.app")
    assert hosts == ["oracle-arm"]
    render = runner.calls[0]["argv"]
    assert render[1:4] == ["-m", "plugins.llm_pool.freellmapi_injector", "render"]
    assert render[render.index("--host-bind") + 1] == "100.64.0.7"
    env, mode = remote.files["/opt/omnifree/deploy/oracle-arm/.env"]
    assert mode == 0o600 and b"OLLAMA_MODELS_TO_PULL='qwen2.5:7b llama3:8b'" in env
    assert remote.files["/opt/omnifree/deploy/oracle-arm/secrets/freellmapi.config.json"][1] == 0o600
    inst_env = remote.files["/opt/omnifree/deploy/instinct/.env"][0].decode()
    assert "HOST_BIND=100.64.0.7" in inst_env and "HUB_URL=https://hub.run.app" in inst_env
    assert "INSTINCT_API_TOKEN=inst" in inst_env and "FREELLMAPI_API_KEY=freellmapi-abc" in inst_env
    cmds = [c for c, _ in remote.commands]
    assert any(c.startswith("cd /opt/omnifree/deploy/oracle-arm && sudo docker compose up -d") for c in cmds)
    assert any("deploy/instinct && sudo docker compose up -d --build" in c for c in cmds)
    bundle = remote.files.get("/tmp/omnifree-bundle.tgz")
    assert bundle is not None
    names = tarfile.open(fileobj=io.BytesIO(bundle[0])).getnames()
    assert "core/vault.py" in names and not any(n.endswith(".env") for n in names)


async def test_deploy_arm_requires_encryption_key(tmp_path: Path) -> None:
    d, _ = _deployer(tmp_path, {}, remote=FakeRemote())
    with pytest.raises(DeployError, match="FREELLMAPI_ENCRYPTION_KEY"):
        await d.deploy_arm()


# ---------------------------------------------------------------------- gcloud / wrangler


def test_deploy_hub_command_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = write_file(tmp_path / ".env.vault", '{"format": "omnifree-vault"}')
    monkeypatch.setenv("OMNIFREE_VAULT_PATH", str(vault))
    monkeypatch.setenv("OMNIFREE_VAULT_PASSPHRASE", "pp-secret")
    runner = FakeRunner(
        outputs={"gcloud projects describe": "123456", "gcloud run services describe": "https://omnifree-hub-x.run.app",
                 "gcloud secrets versions list": "projects/p/secrets/s/versions/3\nprojects/p/secrets/s/versions/2"},
        failing=["gcloud artifacts repositories describe", "gcloud secrets describe"],
    )
    sa = json.dumps({"type": "service_account", "project_id": "omni-proj"})
    d, _ = _deployer(tmp_path, {"GCP_SERVICE_ACCOUNT_JSON": sa, "TAILSCALE_AUTHKEY": "tskey-auth-1"}, runner=runner)
    url = d.deploy_hub(tag="t1", cors=["https://pwa.example"])
    assert url == "https://omnifree-hub-x.run.app"
    joined = runner.joined()
    assert any(j.startswith("gcloud artifacts repositories create omnifree") for j in joined)
    build = next(j for j in joined if j.startswith("gcloud builds submit"))
    assert "_IMAGE=europe-west1-docker.pkg.dev/omni-proj/omnifree/hub:t1" in build
    deploy = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["gcloud", "run", "deploy"])
    secrets_flag = next(a for a in deploy if a.startswith("--set-secrets="))
    assert "OMNIFREE_VAULT_PASSPHRASE=omnifree-vault-passphrase:latest" in secrets_flag
    assert "/secrets/vault/env.vault=omnifree-env-vault:latest" in secrets_flag
    assert "TAILSCALE_AUTHKEY=omnifree-tailscale-authkey:latest" in secrets_flag
    env_flag = next(a for a in deploy if a.startswith("--set-env-vars="))
    assert env_flag.startswith("--set-env-vars=^##^") and "OMNIFREE_CORS_ORIGINS=https://pwa.example" in env_flag
    assert "--project=omni-proj" in deploy
    adds = [c for c in runner.calls if c["argv"][:4] == ["gcloud", "secrets", "versions", "add"]]
    assert {c["input"] for c in adds} == {'{"format": "omnifree-vault"}', "pp-secret", "tskey-auth-1"}
    assert not any("pp-secret" in a for c in runner.calls for a in c["argv"])  # never on argv
    assert any("versions destroy 2" in j for j in joined)  # old versions destroyed (free tier)
    assert json.loads((tmp_path / "state.json").read_text())["urls"]["hub"] == url


def test_deploy_worker_and_pwa(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    write_file(root / "mobile_app" / "package.json", "{}")
    write_file(root / "mobile_app" / "dist" / "index.html", "<html></html>")
    write_file(root / "deploy" / "cloudflare-worker" / "package.json", "{}")
    (root / "deploy" / "cloudflare-worker" / "node_modules").mkdir(parents=True)
    runner = FakeRunner(outputs={"npx wrangler deploy": "Deployed omnifree-edge https://omnifree-edge.me.workers.dev"})
    state = DeployState(tmp_path / "s.json", {"instances": {}, "urls": {"hub": "https://hub.run.app"}})
    d, _ = _deployer(tmp_path, {"CLOUDFLARE_API_TOKEN": "cf-tok", "CLOUDFLARE_ACCOUNT_ID": "acc"}, runner=runner,
                     root=root, state=state)
    pwa = d.deploy_pwa(target="pages")
    assert pwa == "https://omnifree-pwa.pages.dev"
    build = next(c for c in runner.calls if c["argv"][:3] == ["npm", "run", "build"])
    assert build["env"] == {"VITE_HUB_URL": "https://hub.run.app"}
    pages = next(c for c in runner.calls if "pages" in c["argv"] and "deploy" in c["argv"])
    assert pages["env"]["CLOUDFLARE_API_TOKEN"] == "cf-tok" and "--project-name=omnifree-pwa" in pages["argv"]
    edge = d.deploy_worker()
    assert edge == "https://omnifree-edge.me.workers.dev"
    wr = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["npx", "wrangler", "deploy"])
    assert "HUB_ORIGIN:https://hub.run.app" in wr and "PWA_ORIGINS:https://omnifree-pwa.pages.dev" in wr
    assert d.final_url() == edge


async def test_budgets_gcp_aws_oci(tmp_path: Path) -> None:
    runner = FakeRunner(outputs={"gcloud billing projects describe": "billingAccounts/0000-1111"})
    created: dict[str, Any] = {}

    class FakeBudgets:
        def create_budget(self, **kw: Any) -> None:
            created.update(kw)

    class FakeSTS:
        def get_caller_identity(self) -> dict[str, str]:
            return {"Account": "123"}

    class FakeSession:
        def __init__(self, **kw: Any) -> None:
            assert kw["aws_access_key_id"] == "AKIA"

        def client(self, name: str) -> Any:
            return FakeSTS() if name == "sts" else FakeBudgets()

    boto3 = SimpleNamespace(session=SimpleNamespace(Session=FakeSession))
    cloud = FakeCloud()
    secrets = {"GCP_SERVICE_ACCOUNT_JSON": json.dumps({"project_id": "p1"}), "AWS_ACCESS_KEY_ID": "AKIA",
               "AWS_SECRET_ACCESS_KEY": "s", "OCI_TENANCY_OCID": "t", "OCI_USER_OCID": "u", "OCI_FINGERPRINT": "f",
               "OCI_PRIVATE_KEY_PEM": "k"}
    d, _ = _deployer(tmp_path, secrets, runner=runner)
    write_file(tmp_path / "id.pub", "ssh-ed25519 AAAA")
    results = await d.budgets(email="me@example.com", oci_module=fake_oci_module(cloud), boto3_module=boto3)
    assert results["aws"] == "ok" and created["Budget"]["BudgetLimit"] == {"Amount": "1", "Unit": "USD"}
    assert results["gcp"].startswith("ok")
    assert any("--budget-amount=1USD" in j and "--billing-account=0000-1111" in j for j in runner.joined())
    assert results["oci"].startswith("ok") and cloud.alert_rules[0].recipients == "me@example.com"
    assert cloud.budgets[0].details.amount == 1.0
    assert results["b2"].startswith("manual")


def test_cli_parser_knows_every_subcommand() -> None:
    parser = dp.build_parser()
    for cmd in ("ssh-keygen", "oci-provision", "ssh-bootstrap", "lock-ssh", "deploy-arm", "deploy-pihole",
                "deploy-edge", "deploy-hub", "deploy-worker", "deploy-pwa", "deploy-monitor", "verify", "qr",
                "status"):
        assert parser.parse_args([cmd]).cmd == cmd
    args = parser.parse_args(["--host", "oracle-arm=1.2.3.4", "verify", "--count", "3", "--burst", "10"])
    assert dp._parse_hosts(args.host) == {"oracle-arm": "1.2.3.4"} and args.burst == 10
    assert parser.parse_args(["budgets", "--email", "a@b"]).email == "a@b"


def test_cli_status_and_missing_hub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"instances": {}, "urls": {"hub": "https://h"}}), encoding="utf-8")
    assert dp.main(["--state", str(state), "status"]) == 0
    assert '"hub": "https://h"' in capsys.readouterr().out
    assert dp.main(["--state", str(tmp_path / "none.json"), "qr"]) == 2


# ---------------------------------------------------------------------- deploy-hub --target hf


def test_hub_secret_names_cover_enabled_plugins_and_skip_human_passwords(tmp_path: Path) -> None:
    d, _ = _deployer(tmp_path, {})
    names = d.hub_secret_names()
    assert names[:2] == ["HUB_API_TOKEN", "INSTINCT_API_TOKEN"]
    assert {"GROQ_API_KEY", "NEON_DATABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"} <= set(names)
    assert "MISTRAL_API_KEY" not in names and "SAMBANOVA_API_KEY" not in names  # disabled plugins
    assert not {"HF_TOKEN", "B2_ACCOUNT_PASSWORD", "OCI_CONSOLE_PASSWORD", "SUPABASE_DB_PASSWORD"} & set(names)


def test_hf_space_dir_has_docker_readme_and_sources(tmp_path: Path) -> None:
    d, _ = _deployer(tmp_path, {})
    space = d._hf_space_dir(tmp_path / "space")
    readme = (space / "README.md").read_text(encoding="utf-8")
    assert "sdk: docker" in readme and "app_port: 8080" in readme
    dockerfile = (space / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG EXTRA_PIP="asyncpg motor pymongo"' in dockerfile
    for rel in ("core/vault.py", "hub_api/main.py", "config/providers.yaml", "requirements.txt"):
        assert (space / rel).is_file()
    assert not list(space.rglob("__pycache__"))
    assert not (space / ".env.vault").exists()


def test_hub_env_vars_for_render(tmp_path: Path) -> None:
    state = DeployState(tmp_path / "state.json")
    state.set_url("pwa", "https://omnifree-pwa.pages.dev")
    d, _ = _deployer(tmp_path, {"GROQ_API_KEY": "gsk_x", "HUB_API_TOKEN": "hub", "HF_TOKEN": "hf_no"}, state=state)
    env = d._hub_env_vars(["https://app.example"])
    assert env["GROQ_API_KEY"] == "gsk_x" and env["HUB_API_TOKEN"] == "hub" and "HF_TOKEN" not in env
    assert env["OMNIFREE_CORS_ORIGINS"] == "https://app.example,https://omnifree-pwa.pages.dev"
    assert env["EXTRA_PIP"] == "asyncpg motor pymongo" and env["OMNIFREE_ENVIRONMENT"] == "production"


def test_vercel_hub_dir_bundles_serverless_entry(tmp_path: Path) -> None:
    d, _ = _deployer(tmp_path, {})
    out = d._vercel_hub_dir(tmp_path / "vh")
    assert "from hub_api.serverless import app" in (out / "api" / "index.py").read_text(encoding="utf-8")
    cfg = json.loads((out / "vercel.json").read_text(encoding="utf-8"))
    assert cfg["rewrites"] == [{"source": "/(.*)", "destination": "/api/index"}]
    reqs = (out / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi" in reqs and "asyncpg" in reqs and "pytest" not in reqs and "uvicorn" not in reqs
    assert (out / "hub_api" / "serverless.py").is_file() and not (out / ".env.vault").exists()
