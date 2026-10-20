"""OmniFree deployment orchestrator.

Subcommands (run from the project root, ``OMNIFREE_VAULT_PASSPHRASE`` exported)::

    python -m automation.deploy ssh-keygen                 # ed25519 key for the VMs (once)
    python -m automation.deploy oci-provision [--only oracle-arm]
    python -m automation.deploy ssh-bootstrap [--hosts oracle-arm,amd-vm1,amd-vm2] [--via public]
    python -m automation.deploy deploy-arm                 # FreeLLMAPI + Ollama + Instinct (+ WebDAV)
    python -m automation.deploy deploy-pihole [--set-tailnet-dns]
    python -m automation.deploy deploy-edge [--wireguard]  # frps on AMD VM2
    python -m automation.deploy deploy-hub [--project P]   # Cloud Run via gcloud
    python -m automation.deploy deploy-worker              # Cloudflare Worker edge router
    python -m automation.deploy deploy-pwa [--target pages|vercel|netlify]
    python -m automation.deploy deploy-monitor [--target gce|koyeb]   # gce (e2-micro) is the default;
                                                                     # koyeb only for legacy free orgs
    python -m automation.deploy budgets --email you@example.com
    python -m automation.deploy lock-ssh                   # close public :22 once Tailscale works
    python -m automation.deploy verify [--count 8] [--burst 0] [--expect-failover]
    python -m automation.deploy qr [--svg data/omnifree-qr.svg]
    python -m automation.deploy status

Secrets are read through ``core.config.Settings.secret`` (env var first, then the vault).
Non-secret settings: ``deploy/deploy.yaml`` + ``config/providers.yaml``. Outputs (instance ids,
IPs, URLs) are kept in ``data/deploy_state.json`` so later steps find earlier results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

import httpx
import yaml

from automation.deploy_common import (
    PROJECT_ROOT,
    CommandRunner,
    DeployConfig,
    DeployError,
    DeployState,
    SecretLookup,
    build_env_file,
    make_bundle,
)
from automation.remote import Remote, connect, put_secret_file, q, run_script

__all__ = [
    "Deployer",
    "VerifyReport",
    "verify_failover",
    "render_qr",
    "generate_ssh_keypair",
    "main",
]

RemoteFactory = Callable[[str, str, Path], Awaitable[Remote]]
ROUTED_VIA_HEADER = "X-Routed-Via"
TAILSCALE_API = "https://api.tailscale.com/api/v2"


# ====================================================================== verify


@dataclass
class VerifyReport:
    total: int = 0
    ok: int = 0
    failures: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    routed: Counter[str] = field(default_factory=Counter)
    failovers: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures and not self.violations and self.ok == self.total

    def summary(self) -> str:
        lines = [f"{self.ok}/{self.total} requests succeeded; {self.failovers} needed failover"]
        lines += [f"  routed via {name}: {n}" for name, n in self.routed.most_common()]
        lines += [f"  FAIL {f}" for f in self.failures]
        lines += [f"  VIOLATION {v}" for v in self.violations]
        return "\n".join(lines)


def _provider_of(entry: str) -> str:
    return entry.split(":", 1)[0].strip()


def check_chat_response(
    status: int, headers: httpx.Headers, body: Any, chain: Sequence[str], *, enforce_order: bool
) -> tuple[str | None, list[str], list[str]]:
    """Return (routed_via, failed providers, violations) for one /llm/chat response."""
    violations: list[str] = []
    routed = headers.get(ROUTED_VIA_HEADER, "").strip()
    if not routed:
        violations.append(f"missing {ROUTED_VIA_HEADER} header")
    body_routed = body.get("routed_via") if isinstance(body, dict) else None
    if routed and body_routed and body_routed != routed:
        violations.append(f"header {routed!r} != body routed_via {body_routed!r}")
    routed = routed or (body_routed or "")
    attempts = [str(a) for a in (body.get("attempts") or [])] if isinstance(body, dict) else []
    failed = [_provider_of(a) for a in attempts]
    base = _provider_of(routed) if routed else ""
    if chain and base and base not in chain:
        violations.append(f"routed via {base!r}, which is not an enabled LLM provider")
    if base and base in failed:
        violations.append(f"{base} is both the serving provider and a failed attempt")
    if enforce_order and chain and base in chain:
        for f in failed:
            if f in chain and chain.index(f) > chain.index(base):
                violations.append(f"failover out of order: {f} tried before {base} but ranks lower")
    return (routed or None), failed, violations


async def verify_failover(
    hub_url: str,
    token: str | None,
    chain: Sequence[str],
    *,
    count: int = 8,
    burst: int = 0,
    model: str | None = None,
    chat_path: str = "/llm/chat",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 120.0,
    echo: Callable[[str], None] = print,
) -> VerifyReport:
    """Hit the hub chat endpoint and assert the failover contract on every response.

    Checks per response: HTTP 200, ``X-Routed-Via`` present and equal to the body's ``routed_via``,
    serving provider is an enabled LLM plugin, never also listed as failed, and (when no model is
    pinned, so the plain chain applies) every failed attempt ranks above the serving provider.
    ``burst`` > 0 additionally fires that many concurrent requests to push the top provider into
    its rate limit, which exercises real 429 failover.
    """
    report = VerifyReport()
    url = hub_url.rstrip("/") + chat_path
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    enforce_order = not model or model == "auto"

    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:

        async def one(i: int) -> None:
            payload = {
                "messages": [{"role": "user", "content": f"Reply with the single word OK. (probe {i})"}],
                "max_tokens": 8,
                "temperature": 0,
            }
            if model:
                payload["model"] = model
            report.total += 1
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                report.failures.append(f"#{i}: {type(exc).__name__}: {exc}")
                return
            try:
                body: Any = resp.json()
            except ValueError:
                body = None
            if resp.status_code != 200:
                report.failures.append(f"#{i}: HTTP {resp.status_code} {resp.text[:200]}")
                return
            routed, failed, violations = check_chat_response(resp.status_code, resp.headers, body, chain,
                                                             enforce_order=enforce_order)
            report.ok += 1
            if routed:
                report.routed[routed] += 1
            if failed:
                report.failovers += 1
            report.violations.extend(f"#{i}: {v}" for v in violations)
            echo(f"  #{i}: {routed or '?'}" + (f" (after {', '.join(failed)})" if failed else ""))

        for i in range(1, count + 1):
            await one(i)
        if burst > 0:
            echo(f"  burst of {burst} concurrent requests")
            await asyncio.gather(*(one(count + j) for j in range(1, burst + 1)))
    return report


# ====================================================================== QR + ssh keys


def render_qr(url: str, out: TextIO = sys.stdout, svg_path: Path | None = None) -> None:
    try:
        import qrcode
    except ImportError as exc:
        raise DeployError("the qr command needs the qrcode package: pip install qrcode") from exc
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        "█".encode(encoding)
        dark, light = "██", "  "
    except (UnicodeEncodeError, LookupError):
        dark, light = "##", "  "
    out.write(url + "\n\n")
    for row in matrix:
        out.write("".join(dark if cell else light for cell in row) + "\n")
    out.flush()
    if svg_path is not None:
        import qrcode.image.svg

        svg_path.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(str(svg_path))


def generate_ssh_keypair(private_path: Path, public_path: Path, *, overwrite: bool = False) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if private_path.exists() and not overwrite:
        return public_path.read_text(encoding="utf-8").strip() if public_path.exists() else ""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(private_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(private)
    public_text = public.decode("ascii") + " omnifree"
    public_path.write_text(public_text + "\n", encoding="utf-8")
    return public_text


# ====================================================================== deployer


class Deployer:
    def __init__(
        self,
        config: DeployConfig,
        state: DeployState,
        secret: SecretLookup,
        *,
        runner: CommandRunner | None = None,
        echo: Callable[[str], None] = print,
        remote_factory: RemoteFactory | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        host_overrides: dict[str, str] | None = None,
        via: str = "tailnet",
    ) -> None:
        self.cfg = config
        self.state = state
        self.secret = secret
        self.echo = echo
        self.runner = runner or CommandRunner(echo=echo)
        self.remote_factory = remote_factory
        self.http_transport = http_transport
        self.host_overrides = host_overrides or {}
        self.via = via

    # ------------------------------------------------------------------ helpers
    def require(self, name: str) -> str:
        value = self.secret(name)
        if not value:
            raise DeployError(f"secret {name} is not set (python -m core.vault set {name}, or the browser provisioner)")
        self.runner.add_secret(value)
        return value

    def optional(self, name: str) -> str | None:
        value = self.secret(name)
        if value:
            self.runner.add_secret(value)
        return value

    def ssh_paths(self) -> tuple[str, Path, Path]:
        ssh = self.cfg.section("ssh")
        user = str(ssh.get("user", "ubuntu"))
        priv = Path(os.path.expanduser(str(ssh.get("private_key", "~/.ssh/omnifree_ed25519"))))
        pub = Path(os.path.expanduser(str(ssh.get("public_key", str(priv) + ".pub"))))
        return user, priv, pub

    def address(self, name: str, via: str | None = None) -> str:
        if name in self.host_overrides:
            return self.host_overrides[name]
        if (via or self.via) == "public":
            ip = (self.state.instances.get(name) or {}).get("public_ip")
            if not ip:
                raise DeployError(f"no public IP recorded for {name}; run oci-provision or pass --host {name}=IP")
            return str(ip)
        return name  # Tailscale MagicDNS name (providers.yaml hosts)

    async def remote(self, name: str, via: str | None = None) -> Remote:
        user, priv, _ = self.ssh_paths()
        address = self.address(name, via)
        self.echo(f"ssh {user}@{address} ({name})")
        if self.remote_factory is not None:
            return await self.remote_factory(address, user, priv)
        timeout = float(self.cfg.section("ssh").get("connect_timeout_s", 20))
        return await connect(address, user, priv, timeout_s=timeout)

    async def tailscale_ip(self, remote: Remote) -> str:
        out = (await remote.run("tailscale ip -4")).stdout.strip().splitlines()
        if not out or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", out[0].strip()):
            raise DeployError(f"[{remote.host}] Tailscale is not up (run ssh-bootstrap first)")
        return out[0].strip()

    async def upload_bundle(self, remote: Remote, entries: Sequence[str]) -> None:
        data, missing = make_bundle(self.cfg.root, entries)
        for m in missing:
            self.echo(f"  (skipping missing {m})")
        rd = self.cfg.remote_dir
        await remote.put_bytes(data, "/tmp/omnifree-bundle.tgz")
        await remote.run(f"mkdir -p {q(rd)} && tar -xzf /tmp/omnifree-bundle.tgz -C {q(rd)} && rm -f /tmp/omnifree-bundle.tgz")

    async def compose_up(
        self,
        remote: Remote,
        compose_dir: str,
        overrides: dict[str, str],
        *,
        profiles: Sequence[str] = (),
        build: bool = False,
        upload: bool = True,
        env_text: str | None = None,
    ) -> None:
        local = self.cfg.path(compose_dir)
        compose_file = local / "docker-compose.yml"
        if not compose_file.is_file():
            raise DeployError(f"{compose_file} not found")
        if upload:
            await self.upload_bundle(remote, [compose_dir])
        if env_text is None:
            env_text, unset = build_env_file(compose_file.read_text(encoding="utf-8"), self.secret, overrides)
            for name in unset:
                self.echo(f"  {name} not set; compose default applies")
        remote_dir = f"{self.cfg.remote_dir}/{Path(compose_dir).as_posix()}"
        await put_secret_file(remote, env_text, f"{remote_dir}/.env")
        flags = "".join(f" --profile {q(p)}" for p in profiles)
        await remote.run(f"cd {q(remote_dir)} && sudo docker compose{flags} up -d{' --build' if build else ''}"
                         " --remove-orphans")
        ps = await remote.run(f"cd {q(remote_dir)} && sudo docker compose{flags} ps --format '{{{{.Name}}}} {{{{.Status}}}}'",
                              check=False)
        for line in ps.stdout.strip().splitlines():
            self.echo(f"  {line}")

    # ------------------------------------------------------------------ ssh-keygen / oci
    def ssh_keygen(self, overwrite: bool = False) -> str:
        _, priv, pub = self.ssh_paths()
        existed = priv.exists()
        public = generate_ssh_keypair(priv, pub, overwrite=overwrite)
        self.echo(f"{'kept existing' if existed and not overwrite else 'wrote'} {priv} / {pub}")
        return public

    def _provisioner(self, oci_module: Any = None) -> Any:
        from automation.deploy_oci import OCIProvisioner

        _, _, pub = self.ssh_paths()
        if not pub.is_file():
            raise DeployError(f"SSH public key {pub} not found (run: python -m automation.deploy ssh-keygen)")
        return OCIProvisioner(self.cfg, self.state, self.secret, ssh_public_key=pub.read_text(encoding="utf-8"),
                              oci_module=oci_module, echo=self.echo)

    async def oci_provision(self, only: list[str] | None = None, oci_module: Any = None) -> dict[str, Any]:
        results = await self._provisioner(oci_module).provision(only)
        for name, info in results.items():
            if info and name != "block_volume":
                self.echo(f"{name}: public {info.get('public_ip')} private {info.get('private_ip')}")
        return results

    async def lock_ssh(self, oci_module: Any = None) -> None:
        await self._provisioner(oci_module).lock_ssh()

    # ------------------------------------------------------------------ ssh-bootstrap
    async def ssh_bootstrap(self, hosts: Sequence[str], via: str = "public") -> None:
        authkey = self.require("TAILSCALE_AUTHKEY")
        block = dict(self.cfg.section("oci").get("block_volume") or {})
        user, _, _ = self.ssh_paths()
        for name in hosts:
            remote = await self.remote(name, via)
            try:
                await remote.run("cloud-init status --wait >/dev/null 2>&1 || true", check=False)
                self.echo("  docker")
                await run_script(remote, f"""
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
usermod -aG docker {q(user)}
systemctl enable --now docker
""")
                self.echo("  tailscale")
                await run_script(remote, """
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
iptables -C INPUT -i tailscale0 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -i tailscale0 -j ACCEPT
command -v netfilter-persistent >/dev/null && netfilter-persistent save || true
""")
                running = await remote.run("tailscale status --json 2>/dev/null | grep -q '\"BackendState\": *\"Running\"'",
                                           check=False)
                if running.exit_status != 0:
                    key_path = "/tmp/omnifree-ts-authkey"
                    await put_secret_file(remote, authkey, key_path)
                    await run_script(remote, f"""
trap 'rm -f {key_path}' EXIT
tailscale up --auth-key=file:{key_path} --hostname={q(name)}
""")
                ts_ip = await self.tailscale_ip(remote)
                self.state.instances.setdefault(name, {})["tailscale_ip"] = ts_ip
                self.state.save()
                self.echo(f"  tailscale ip {ts_ip}")
                if block and str(block.get("attach_to")) == name:
                    await self.mount_block_volume(remote, str(block.get("device", "/dev/oracleoci/oraclevdb")),
                                                  str(block.get("mount_path", "/mnt/omnifree-block")), user)
                await remote.run(f"sudo mkdir -p {q(self.cfg.remote_dir)} && sudo chown {q(user)}:{q(user)} "
                                 f"{q(self.cfg.remote_dir)}")
            finally:
                await remote.close()

    async def mount_block_volume(self, remote: Remote, device: str, mount_path: str, user: str) -> None:
        self.echo(f"  block volume {device} -> {mount_path}")
        await run_script(remote, f"""
DEV={q(device)}
for i in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 2; done
[ -e "$DEV" ] || {{ echo "block device $DEV not found (is the volume attached?)" >&2; exit 1; }}
blkid "$DEV" >/dev/null 2>&1 || mkfs.ext4 -q -L omnifree-block "$DEV"
mkdir -p {q(mount_path)}
UUID=$(blkid -s UUID -o value "$DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID {mount_path} ext4 defaults,_netdev,nofail 0 2" >> /etc/fstab
mountpoint -q {q(mount_path)} || mount {q(mount_path)}
chown {q(user)}:{q(user)} {q(mount_path)}
""")

    # ------------------------------------------------------------------ deploy-arm
    def _injector_render(self, host_bind: str, workdir: Path) -> tuple[str, str]:
        """Run the llm_pool injector locally; returns (config json, compose .env text)."""
        out, env_out = workdir / "freellmapi.config.json", workdir / ".env"
        self.runner.run([sys.executable, "-m", "plugins.llm_pool.freellmapi_injector", "render",
                         "--out", str(out), "--env-out", str(env_out), "--host-bind", host_bind, "--only-enabled"],
                        cwd=self.cfg.root)
        return out.read_text(encoding="utf-8"), env_out.read_text(encoding="utf-8")

    async def deploy_arm(self, *, hub_url: str | None = None, skip_webdav: bool = False) -> None:
        arm = self.cfg.section("arm")
        compose_dir = str(arm.get("compose_dir", "deploy/oracle-arm"))
        if not (self.cfg.path(compose_dir) / "docker-compose.yml").is_file():
            raise DeployError(f"{compose_dir}/docker-compose.yml is missing (owned by the llm_pool builder)")
        injector = self.cfg.root / "plugins" / "llm_pool" / "freellmapi_injector.py"
        if not injector.is_file():
            raise DeployError("plugins/llm_pool/freellmapi_injector.py is missing; it renders the FreeLLMAPI config")
        if not self.secret("FREELLMAPI_ENCRYPTION_KEY"):
            raise DeployError("FREELLMAPI_ENCRYPTION_KEY missing: python -m plugins.llm_pool.freellmapi_injector genkey")
        name = self.cfg.host_alias("oracle_arm")
        remote = await self.remote(name)
        try:
            host_bind = await self.tailscale_ip(remote)
            await self.upload_bundle(remote, list(arm.get("bundle") or [compose_dir]))

            # 1. FreeLLMAPI + Ollama (llm_pool compose): config + .env rendered by the injector.
            with tempfile.TemporaryDirectory(prefix="omnifree-arm-") as tmp:
                config_json, env_text = self._injector_render(host_bind, Path(tmp))
            models = self.cfg.plugin_settings("ollama").get("models") or []
            if models and "OLLAMA_MODELS_TO_PULL" not in env_text:
                env_text = env_text.rstrip("\n") + f"\nOLLAMA_MODELS_TO_PULL='{' '.join(map(str, models))}'\n"
            rd = f"{self.cfg.remote_dir}/{compose_dir}"
            await remote.run(f"mkdir -p {q(rd)}/secrets")
            await put_secret_file(remote, config_json, f"{rd}/secrets/freellmapi.config.json")
            self.echo("  FreeLLMAPI + Ollama")
            await self.compose_up(remote, compose_dir, {}, upload=False, env_text=env_text)

            # 2. Instinct agent.
            self.echo("  Instinct agent")
            hub = hub_url or self.state.urls.get("hub") or ""
            await self.compose_up(remote, "deploy/instinct", {
                "HOST_BIND": host_bind,
                "HUB_URL": hub,
                "FREELLMAPI_API_KEY": self.optional("FREELLMAPI_API_KEY") or "",
                "INSTINCT_API_TOKEN": self.require("INSTINCT_API_TOKEN"),
                "HUB_API_TOKEN": self.require("HUB_API_TOKEN"),
            }, build=True, upload=False)

            # 3. WebDAV over the block volume (oracle_block_storage plugin).
            if not skip_webdav:
                await self._deploy_webdav(remote, host_bind)
        finally:
            await remote.close()
        if not self.secret("FREELLMAPI_API_KEY"):
            self.echo("Next: open http://oracle-arm:3001 (Tailscale), copy the unified key, then run\n"
                      "  python -m automation.browser_provisioner run --only freellmapi\n"
                      "and deploy-arm again so Instinct gets FREELLMAPI_API_KEY.")

    async def _deploy_webdav(self, remote: Remote, host_bind: str) -> None:
        settings = self.cfg.plugin_settings("oracle_block_storage")
        port = urlparse(str(settings.get("webdav_url") or "http://oracle-arm:8091")).port or 80
        if port == 8090:
            self.echo("  !! WebDAV skipped: providers.yaml oracle_block_storage.settings.webdav_url uses port 8090, "
                      "which Instinct already uses on oracle-arm. Change it (e.g. http://oracle-arm:8091) and rerun.")
            return
        self.echo(f"  WebDAV on {host_bind}:{port}")
        await self.compose_up(remote, "deploy/blockstore", {
            "BIND_IP": host_bind, "WEBDAV_PORT": str(port),
            "BLOCK_MOUNT": str(settings.get("mount_path") or "/mnt/omnifree-block"),
        }, upload=False)

    # ------------------------------------------------------------------ deploy-pihole / deploy-edge
    async def deploy_pihole(self, *, set_tailnet_dns: bool = False) -> str:
        section = self.cfg.section("pihole")
        name = str(section.get("host") or self.cfg.host_alias("oracle_amd_vm1"))
        self.require("PIHOLE_PASSWORD")
        remote = await self.remote(name)
        try:
            ts_ip = await self.tailscale_ip(remote)
            await self.compose_up(remote, str(section.get("compose_dir", "deploy/pihole")), {"BIND_IP": ts_ip})
        finally:
            await remote.close()
        if set_tailnet_dns:
            await self.set_tailnet_dns(ts_ip)
        self.echo(f"Pi-hole: http://{name}/admin (DNS {ts_ip})")
        return ts_ip

    async def set_tailnet_dns(self, nameserver: str) -> None:
        key = self.require("TAILSCALE_API_KEY")
        tailnet = str(self.cfg.plugin_settings("tailscale").get("tailnet") or "-")
        async with httpx.AsyncClient(base_url=TAILSCALE_API, transport=self.http_transport, timeout=20,
                                     headers={"Authorization": f"Bearer {key}"}) as client:
            r1 = await client.post(f"/tailnet/{tailnet}/dns/nameservers", json={"dns": [nameserver]})
            r2 = await client.post(f"/tailnet/{tailnet}/dns/preferences", json={"magicDNS": True})
        for r in (r1, r2):
            if r.status_code >= 400:
                raise DeployError(f"Tailscale API {r.request.url.path}: HTTP {r.status_code} {r.text[:200]}")
        self.echo(f"tailnet DNS -> {nameserver} (MagicDNS on)")

    async def deploy_edge(self, *, wireguard: bool | None = None) -> None:
        section = self.cfg.section("edge")
        name = str(section.get("host") or self.cfg.host_alias("oracle_amd_vm2"))
        self.require("FRPS_TOKEN")
        use_wg = bool(section.get("wireguard")) if wireguard is None else wireguard
        overrides: dict[str, str] = {}
        public_ip = (self.state.instances.get(name) or {}).get("public_ip")
        if public_ip:
            overrides["WG_SERVERURL"] = str(public_ip)
        remote = await self.remote(name)
        try:
            await self.compose_up(remote, str(section.get("compose_dir", "deploy/frps")), overrides,
                                  profiles=["wireguard"] if use_wg else [])
        finally:
            await remote.close()
        self.echo(f"frps on {public_ip or name}:7000 (token FRPS_TOKEN)")

    # ------------------------------------------------------------------ deploy-hub (Cloud Run)
    def gcp_project(self, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        configured = self.cfg.plugin_settings("gcp_cloud_run").get("project_id")
        if configured:
            return str(configured)
        sa = self.secret("GCP_SERVICE_ACCOUNT_JSON")
        if sa:
            try:
                project = json.loads(sa).get("project_id")
            except ValueError:
                project = None
            if project:
                return str(project)
        raise DeployError("GCP project unknown: pass --project, set gcp_cloud_run.settings.project_id, "
                          "or store GCP_SERVICE_ACCOUNT_JSON")

    def gcloud(self, *args: str, project: str, capture: bool = False, check: bool = True,
               input_text: str | None = None) -> str:
        proc = self.runner.run(["gcloud", *args, f"--project={project}", "--quiet"], capture=capture, check=check,
                               input_text=input_text)
        return (proc.stdout or "").strip() if capture else ""

    def gcloud_activate(self, project: str) -> None:
        sa = self.require("GCP_SERVICE_ACCOUNT_JSON")
        with tempfile.TemporaryDirectory(prefix="omnifree-gcp-") as tmp:
            key_file = Path(tmp) / "sa.json"
            key_file.write_text(sa, encoding="utf-8")
            self.runner.run(["gcloud", "auth", "activate-service-account", f"--key-file={key_file}",
                             f"--project={project}", "--quiet"])

    def _put_secret(self, name: str, value: str, project: str, runtime_sa: str) -> None:
        exists = self.runner.run(["gcloud", "secrets", "describe", name, f"--project={project}", "--quiet"],
                                 capture=True, check=False).returncode == 0
        if not exists:
            self.gcloud("secrets", "create", name, "--replication-policy=automatic", project=project)
        else:
            current = self.runner.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}",
                                       f"--project={project}", "--quiet"], capture=True, check=False)
            if current.returncode == 0 and current.stdout == value:
                self.echo(f"  secret {name} unchanged")
                return
        self.gcloud("secrets", "versions", "add", name, "--data-file=-", project=project, input_text=value)
        versions = self.gcloud("secrets", "versions", "list", name, "--filter=state:ENABLED",
                               "--sort-by=~createTime", "--format=value(name)", project=project, capture=True)
        for old in [v.rsplit("/", 1)[-1] for v in versions.splitlines()[1:] if v.strip()]:
            self.gcloud("secrets", "versions", "destroy", old, f"--secret={name}", project=project)
        self.gcloud("secrets", "add-iam-policy-binding", name, f"--member=serviceAccount:{runtime_sa}",
                    "--role=roles/secretmanager.secretAccessor", project=project, capture=True)

    def deploy_hub(self, *, project: str | None = None, region: str | None = None, cors: Sequence[str] = (),
                   activate_sa: bool = False, tag: str | None = None, tailscale: bool = True) -> str:
        hub = self.cfg.section("hub")
        project = self.gcp_project(project)
        run_settings = self.cfg.plugin_settings("gcp_cloud_run")
        region = region or str(run_settings.get("region") or "europe-west1")
        service = str(hub.get("service") or (run_settings.get("services") or ["omnifree-hub"])[0])
        repo = str(hub.get("artifact_repo", "omnifree"))
        if activate_sa:
            self.gcloud_activate(project)

        self.gcloud("services", "enable", "run.googleapis.com", "cloudbuild.googleapis.com",
                    "artifactregistry.googleapis.com", "secretmanager.googleapis.com", project=project)
        if self.runner.run(["gcloud", "artifacts", "repositories", "describe", repo, f"--location={region}",
                            f"--project={project}", "--quiet"], capture=True, check=False).returncode != 0:
            self.gcloud("artifacts", "repositories", "create", repo, "--repository-format=docker",
                        f"--location={region}", "--description=OmniFree images", project=project)

        number = self.gcloud("projects", "describe", project, "--format=value(projectNumber)",
                             project=project, capture=True)
        runtime_sa = f"{number}-compute@developer.gserviceaccount.com"

        vault_file = Path(os.environ.get("OMNIFREE_VAULT_PATH") or self.cfg.root / ".env.vault")
        passphrase = os.environ.get("OMNIFREE_VAULT_PASSPHRASE")
        if not vault_file.is_file() or not passphrase:
            raise DeployError("deploy-hub ships the encrypted vault to Secret Manager: need .env.vault and "
                              "OMNIFREE_VAULT_PASSPHRASE")
        self.runner.add_secret(passphrase)
        secrets_flags = [f"OMNIFREE_VAULT_PASSPHRASE={hub.get('passphrase_secret', 'omnifree-vault-passphrase')}:latest",
                         f"/secrets/vault/env.vault={hub.get('vault_secret', 'omnifree-env-vault')}:latest"]
        self._put_secret(str(hub.get("vault_secret", "omnifree-env-vault")), vault_file.read_text(encoding="utf-8"),
                         project, runtime_sa)
        self._put_secret(str(hub.get("passphrase_secret", "omnifree-vault-passphrase")), passphrase, project, runtime_sa)
        ts_key = self.optional("TAILSCALE_HUB_AUTHKEY") or self.optional("TAILSCALE_AUTHKEY")
        if tailscale and ts_key:
            ts_secret = str(hub.get("tailscale_secret", "omnifree-tailscale-authkey"))
            self._put_secret(ts_secret, ts_key, project, runtime_sa)
            secrets_flags.append(f"TAILSCALE_AUTHKEY={ts_secret}:latest")
        elif tailscale:
            self.echo("  !! no TAILSCALE_HUB_AUTHKEY/TAILSCALE_AUTHKEY: the hub will not reach tailnet services")

        tag = tag or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        image = f"{region}-docker.pkg.dev/{project}/{repo}/hub:{tag}"
        self.runner.run(["gcloud", "builds", "submit", str(self.cfg.root), f"--project={project}",
                         f"--config={self.cfg.path('deploy/hub/cloudbuild.yaml')}",
                         f"--substitutions=_IMAGE={image}", "--quiet"])

        origins = [o for o in [*cors, self.state.urls.get("edge", ""), self.state.urls.get("pwa", "")] if o]
        env_vars = {"OMNIFREE_ENVIRONMENT": "production", "OMNIFREE_VAULT_PATH": "/secrets/vault/env.vault",
                    "OMNIFREE_VAULT_REQUIRED": "true"}
        if origins:
            env_vars["OMNIFREE_CORS_ORIGINS"] = ",".join(dict.fromkeys(origins))
        env_flag = "^##^" + "##".join(f"{k}={v}" for k, v in env_vars.items())
        self.gcloud("run", "deploy", service, f"--image={image}", f"--region={region}", "--platform=managed",
                    "--allow-unauthenticated", "--port=8080", "--execution-environment=gen2",
                    f"--min-instances={hub.get('min_instances', 0)}", f"--max-instances={hub.get('max_instances', 2)}",
                    f"--memory={hub.get('memory', '512Mi')}", f"--cpu={hub.get('cpu', '1')}",
                    f"--set-env-vars={env_flag}", f"--set-secrets={','.join(secrets_flags)}", project=project)
        url = self.gcloud("run", "services", "describe", service, f"--region={region}", "--format=value(status.url)",
                          project=project, capture=True)
        if url:
            self.state.set_url("hub", url)
            self.echo(f"hub: {url}")
        return url

    # ------------------------------------------------------------------ deploy-hub (Hugging Face Space)
    # Human-only credentials that the hub never reads; never copied into Space secrets.
    _HF_SECRET_DENYLIST = frozenset({
        "B2_ACCOUNT_PASSWORD", "HF_ACCOUNT_PASSWORD", "HF_TOKEN", "OCI_CONSOLE_PASSWORD", "SUPABASE_DB_PASSWORD",
        "FREELLMAPI_ADMIN_PASSWORD", "GITHUB_TOKEN_CLASSIC",
    })

    def hub_secret_names(self, providers: dict[str, Any] | None = None) -> list[str]:
        """Vault keys the hub can use: secrets of enabled plugins plus the hub/instinct/MCP tokens."""
        names: dict[str, None] = {"HUB_API_TOKEN": None, "INSTINCT_API_TOKEN": None, "MCP_API_TOKEN": None}
        for spec in ((providers or self.cfg.providers).get("plugins") or {}).values():
            if not isinstance(spec, dict) or not spec.get("enabled", True):
                continue
            for group in ("secrets", "optional_secrets"):
                for key in (spec.get(group) or {}).values():
                    names[str(key)] = None
        # Email receipt import (hub_api/routers/inbox.py): the account list plus each account's password key.
        names.update(dict.fromkeys(("INBOX_ACCOUNTS", "GMAIL_IMAP_USER", "GMAIL_APP_PASSWORD")))
        names.update(dict.fromkeys(self._inbox_password_keys()))
        return [n for n in names if n not in self._HF_SECRET_DENYLIST]

    def _inbox_password_keys(self) -> list[str]:
        try:
            entries = json.loads(self.optional("INBOX_ACCOUNTS") or "[]")
        except ValueError:
            return []
        if not isinstance(entries, list):
            return []
        return [str(e["password_key"]).strip() for e in entries if isinstance(e, dict) and e.get("password_key")]

    def _hf_space_dir(self, target: Path) -> Path:
        import shutil

        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
        for rel in ("core", "plugins", "hub_api", "config"):
            shutil.copytree(self.cfg.path(rel), target / rel, ignore=ignore)
        shutil.copy2(self.cfg.path("requirements.txt"), target / "requirements.txt")
        hub = self.cfg.section("hub")
        extra = str(hub.get("hf_extra_pip", "asyncpg motor pymongo"))
        dockerfile = self.cfg.path("hub_api/Dockerfile").read_text(encoding="utf-8")
        dockerfile = dockerfile.replace('ARG EXTRA_PIP=""', f'ARG EXTRA_PIP="{extra}"')
        (target / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (target / "README.md").write_text(
            "---\ntitle: OmniFree Hub\nemoji: 🛰️\ncolorFrom: indigo\ncolorTo: gray\nsdk: docker\napp_port: 8080\n"
            "pinned: false\n---\n\nOmniFree hub_api (FastAPI). Source: https://github.com/SimaxBen/omnifree\n",
            encoding="utf-8")
        return target

    def deploy_hub_hf(self, *, space: str | None = None, cors: Sequence[str] = (), wait_s: float = 900.0) -> str:
        import time

        from huggingface_hub import HfApi  # lazy: only this deploy target needs it

        hub = self.cfg.section("hub")
        token = self.require("HF_TOKEN")
        api = HfApi(token=token)
        owner = str(hub.get("hf_owner") or api.whoami()["name"])
        repo_id = space or f"{owner}/{hub.get('hf_space', 'omnifree-hub')}"
        api.create_repo(repo_id, repo_type="space", space_sdk="docker", private=False, exist_ok=True)

        sent = 0
        for name in self.hub_secret_names():
            value = self.optional(name)
            if value:
                api.add_space_secret(repo_id, name, value)
                sent += 1
        origins = [o for o in [*cors, self.state.urls.get("edge", ""), self.state.urls.get("pwa", "")] if o]
        variables = {"OMNIFREE_ENVIRONMENT": "production", "OMNIFREE_VAULT_REQUIRED": "false"}
        if origins:
            variables["OMNIFREE_CORS_ORIGINS"] = ",".join(dict.fromkeys(origins))
        for key, value in variables.items():
            api.add_space_variable(repo_id, key, value)
        self.echo(f"  {sent} secrets + {len(variables)} variables set on {repo_id}")

        staging = self._hf_space_dir(self.cfg.path("data/hf-space"))
        api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(staging),
                          commit_message="deploy hub_api", delete_patterns=["*"])
        url = f"https://{repo_id.replace('/', '-').replace('_', '-').lower()}.hf.space"
        deadline = time.monotonic() + wait_s
        stage = ""
        while time.monotonic() < deadline:
            stage = str(api.get_space_runtime(repo_id).stage)
            if stage in ("RUNNING", "BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"):
                break
            time.sleep(15)
        self.echo(f"  space stage: {stage}")
        if stage != "RUNNING":
            raise DeployError(f"Space {repo_id} ended in {stage}; see https://huggingface.co/spaces/{repo_id}?logs=build")
        self.state.set_url("hub", url)
        self.echo(f"hub: {url}")
        return url

    # ------------------------------------------------------------------ deploy-hub (Render free web service)
    def _hub_env_vars(self, cors: Sequence[str], providers: dict[str, Any] | None = None) -> dict[str, str]:
        hub = self.cfg.section("hub")
        env = {name: value for name in self.hub_secret_names(providers) if (value := self.optional(name))}
        origins = [o for o in [*cors, self.state.urls.get("edge", ""), self.state.urls.get("pwa", ""),
                               *self.state.data.get("pwa_mirrors", [])] if o]
        env.update({"OMNIFREE_ENVIRONMENT": "production", "OMNIFREE_VAULT_REQUIRED": "false",
                    # Docker build arg (Render passes env vars to `docker build`): optional SDKs.
                    "EXTRA_PIP": str(hub.get("extra_pip", "asyncpg motor pymongo"))})
        if origins:
            env["OMNIFREE_CORS_ORIGINS"] = ",".join(dict.fromkeys(origins))
        return env

    def deploy_hub_render(self, *, cors: Sequence[str] = (), region: str | None = None,
                          wait_s: float = 1200.0, poll_s: float = 15.0) -> str:
        import time

        hub = self.cfg.section("hub")
        api = "https://api.render.com/v1"
        headers = {"Authorization": f"Bearer {self.require('RENDER_API_KEY')}", "Accept": "application/json"}
        name = str(hub.get("service") or "omnifree-hub")
        repo = str(hub.get("repo_url") or "https://github.com/SimaxBen/omnifree")
        env = self._hub_env_vars(cors)
        env_list = [{"key": k, "value": v} for k, v in env.items()]
        with httpx.Client(timeout=60, headers=headers) as http:
            owner = http.get(f"{api}/owners", params={"limit": 20}).raise_for_status().json()[0]["owner"]["id"]
            found = http.get(f"{api}/services", params={"name": name, "ownerId": owner, "limit": 20}).raise_for_status()
            existing = [s["service"] for s in found.json() if s["service"]["name"] == name]
            if existing:
                service = existing[0]
                http.put(f"{api}/services/{service['id']}/env-vars", json=env_list).raise_for_status()
                deploy = http.post(f"{api}/services/{service['id']}/deploys", json={"clearCache": "do_not_clear"})
                deploy_id = deploy.raise_for_status().json()["id"]
            else:
                body = {
                    "type": "web_service", "name": name, "ownerId": owner, "repo": repo, "branch": "main",
                    "autoDeploy": "yes", "envVars": env_list,
                    "serviceDetails": {
                        "runtime": "docker", "plan": "free", "region": region or str(hub.get("render_region", "frankfurt")),
                        "healthCheckPath": "/healthz",
                        "envSpecificDetails": {"dockerfilePath": "./hub_api/Dockerfile", "dockerContext": "."},
                    },
                }
                created = http.post(f"{api}/services", json=body)
                if created.status_code >= 400:
                    raise DeployError(f"Render create service failed: {created.status_code} {created.text[:400]}")
                payload = created.json()
                service, deploy_id = payload["service"], payload.get("deployId")
            url = str(service.get("serviceDetails", {}).get("url") or "")
            self.echo(f"  render service {service['id']} ({len(env)} env vars), deploy {deploy_id}")
            deadline = time.monotonic() + wait_s
            status = ""
            while deploy_id and time.monotonic() < deadline:
                status = http.get(f"{api}/services/{service['id']}/deploys/{deploy_id}").raise_for_status().json()["status"]
                if status in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
                    break
                time.sleep(poll_s)
            self.echo(f"  deploy status: {status}")
            if deploy_id and status != "live":
                raise DeployError(f"Render deploy ended in {status!r}: https://dashboard.render.com/web/{service['id']}")
            if not url:
                url = str(http.get(f"{api}/services/{service['id']}").raise_for_status()
                          .json().get("serviceDetails", {}).get("url") or "")
        if url:
            self.state.set_url("hub", url)
            self.echo(f"hub: {url}")
        return url

    # ------------------------------------------------------------------ deploy-hub (Vercel Python function)
    def serverless_providers(self) -> dict[str, Any]:
        """providers.yaml for the serverless hub: disable plugins it cannot use (missing secrets / no server)."""
        import copy

        doc = copy.deepcopy(self.cfg.providers)
        unreachable = set(self.cfg.section("hub").get("serverless_disable") or [])
        for name, spec in (doc.get("plugins") or {}).items():
            if not isinstance(spec, dict) or not spec.get("enabled", True):
                continue
            missing = [key for key in (spec.get("secrets") or {}).values() if not self.secret(str(key))]
            if name in unreachable or missing:
                spec["enabled"] = False
        return doc

    def _vercel_project(self, name: str) -> dict[str, Any]:
        """Get or create a Vercel project (idempotent); returns the project JSON (``id``, ``accountId``)."""
        team = self.optional("VERCEL_TEAM_ID")
        params = {"teamId": team} if team else {}
        with httpx.Client(base_url="https://api.vercel.com", timeout=60, params=params,
                          headers={"Authorization": f"Bearer {self.require('VERCEL_TOKEN')}"}) as http:
            got = http.get(f"/v9/projects/{name}")
            if got.status_code == 404:
                got = http.post("/v10/projects", json={"name": name, "framework": None})
            return dict(got.raise_for_status().json())

    def _netlify_site(self, name: str) -> str:
        """Get or create a Netlify site by name; returns its site id."""
        with httpx.Client(base_url="https://api.netlify.com/api/v1", timeout=60,
                          headers={"Authorization": f"Bearer {self.require('NETLIFY_TOKEN')}"}) as http:
            found = [s for s in http.get("/sites", params={"name": name, "filter": "all"}).raise_for_status().json()
                     if s.get("name") == name]
            if found:
                site = found[0]
            else:
                created = http.post("/sites", json={"name": name})
                if created.status_code >= 400:
                    raise DeployError(f"Netlify site {name!r} could not be created: "
                                      f"{created.status_code} {created.text[:200]}")
                site = created.json()
            if site.get("sso_login"):
                # New team sites default to "Netlify team login" visitor access: a public mirror must be open.
                http.patch(f"/sites/{site['id']}", json={"sso_login": False}).raise_for_status()
            return str(site["id"])

    @staticmethod
    def _spa_copy(dist: Path, target: Path) -> Path:
        """Copy the PWA build and add SPA fallbacks for Vercel (vercel.json) and Netlify (_redirects)."""
        import shutil

        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(dist, target)
        (target / "vercel.json").write_text(json.dumps(
            {"rewrites": [{"source": "/((?!assets/|icons/|sw.js|manifest).*)", "destination": "/index.html"}]},
            indent=2) + "\n", encoding="utf-8")
        (target / "_redirects").write_text("/*    /index.html   200\n", encoding="utf-8")
        return target

    def _vercel_hub_dir(self, target: Path) -> Path:
        import shutil

        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
        for rel in ("core", "plugins", "hub_api", "config"):
            shutil.copytree(self.cfg.path(rel), target / rel, ignore=ignore)
        trimmed = self.serverless_providers()
        (target / "config" / "providers.yaml").write_text(
            "# Generated by deploy-hub --target vercel from config/providers.yaml (unreachable plugins disabled).\n"
            + yaml.safe_dump(trimmed, sort_keys=False, allow_unicode=True), encoding="utf-8")
        runtime = [line for line in self.cfg.path("requirements.txt").read_text(encoding="utf-8").splitlines()
                   if line.strip() and not re.match(r"(pytest|respx|uvicorn)", line.strip())]
        extra = str(self.cfg.section("hub").get("extra_pip", "asyncpg motor pymongo")).split()
        (target / "requirements.txt").write_text("\n".join([*runtime, *extra]) + "\n", encoding="utf-8")
        (target / ".python-version").write_text("3.12\n", encoding="utf-8")
        (target / "api").mkdir()
        (target / "api" / "index.py").write_text(
            '"""Vercel entry point: every path is rewritten here (see vercel.json)."""\n'
            "from hub_api.serverless import app  # noqa: F401\n", encoding="utf-8")
        (target / "vercel.json").write_text(json.dumps({
            "rewrites": [{"source": "/(.*)", "destination": "/api/index"}],
            "functions": {"api/index.py": {"maxDuration": 60}},
        }, indent=2) + "\n", encoding="utf-8")
        return target

    def deploy_hub_vercel(self, *, cors: Sequence[str] = (), project: str | None = None) -> str:
        hub = self.cfg.section("hub")
        token = self.require("VERCEL_TOKEN")
        team = self.optional("VERCEL_TEAM_ID")
        name = project or str(hub.get("vercel_project", "omnifree-hub"))
        params = {"teamId": team} if team else {}
        env = self._hub_env_vars(cors, self.serverless_providers())
        env.pop("EXTRA_PIP", None)  # baked into requirements.txt instead
        proj = self._vercel_project(name)
        with httpx.Client(base_url="https://api.vercel.com", timeout=60, params=params,
                          headers={"Authorization": f"Bearer {token}"}) as http:
            body = [{"key": k, "value": v, "type": "encrypted", "target": ["production", "preview"]}
                    for k, v in env.items()]
            http.post(f"/v10/projects/{proj['id']}/env", params={**params, "upsert": "true"},
                      json=body).raise_for_status()
        self.echo(f"  vercel project {name}: {len(env)} env vars")
        staging = self._vercel_hub_dir(self.cfg.path("data/vercel-hub"))
        run_env = {"VERCEL_PROJECT_ID": proj["id"], "VERCEL_ORG_ID": team or proj.get("accountId", "")}
        cmd = [*self._cli("vercel@latest", "vercel"), "deploy", str(staging), "--prod", "--yes", f"--token={token}"]
        if team:
            cmd.append(f"--scope={team}")
        proc = self.runner.run(cmd, env=run_env, capture=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        self.echo(out.strip()[-600:])
        url = f"https://{name}.vercel.app"
        self.state.set_url("hub", url)
        self.echo(f"hub: {url}")
        return url

    # ------------------------------------------------------------------ edge worker / PWA
    @staticmethod
    def _cli(package: str, binary: str) -> list[str]:
        """Use an installed CLI when it is on PATH (avoids npx lock races), else ``npx --yes <package>``."""
        import shutil

        return [binary] if shutil.which(binary) else ["npx", "--yes", package]

    def _cloudflare_env(self) -> dict[str, str]:
        return {"CLOUDFLARE_API_TOKEN": self.require("CLOUDFLARE_API_TOKEN"),
                "CLOUDFLARE_ACCOUNT_ID": self.require("CLOUDFLARE_ACCOUNT_ID")}

    def _npm_install(self, directory: Path) -> None:
        if (directory / "node_modules").is_dir():
            return
        self.runner.run(["npm", "ci" if (directory / "package-lock.json").is_file() else "install", "--no-audit",
                         "--no-fund"], cwd=directory)

    def deploy_worker(self, *, hub_url: str | None = None, pwa_origins: Sequence[str] = ()) -> str:
        directory = self.cfg.path(str(self.cfg.section("worker").get("dir", "deploy/cloudflare-worker")))
        hub = hub_url or self.state.urls.get("hub")
        if not hub:
            raise DeployError("hub URL unknown: run deploy-hub first or pass --hub-url")
        origins = list(pwa_origins) or [u for u in (self.state.urls.get("pwa"), *self.state.data.get("pwa_mirrors", []))
                                        if u]
        if not origins:
            raise DeployError("PWA origin unknown: run deploy-pwa first or pass --pwa-origin")
        self._npm_install(directory)
        proc = self.runner.run([*self._cli("wrangler@4", "wrangler"), "deploy", "--var", f"HUB_ORIGIN:{hub}",
                                "--var", f"PWA_ORIGINS:{','.join(origins)}"],
                               cwd=directory, env=self._cloudflare_env(), capture=True)
        out = proc.stdout or ""
        self.echo(out.strip()[-1500:])
        match = re.search(r"https://[A-Za-z0-9.\-]+\.workers\.dev", out)
        if match:
            self.state.set_url("edge", match.group(0))
            self.echo(f"edge: {match.group(0)}")
            return match.group(0)
        return ""

    def build_pwa(self, hub_base: str) -> Path:
        pwa = self.cfg.section("pwa")
        directory = self.cfg.path(str(pwa.get("dir", "mobile_app")))
        if not (directory / "package.json").is_file():
            raise DeployError(f"{directory}/package.json not found (mobile_app builder)")
        self._npm_install(directory)
        self.runner.run(["npm", "run", "build"], cwd=directory,
                        env={str(pwa.get("hub_env_var", "VITE_HUB_URL")): hub_base})
        dist = self.cfg.path(str(pwa.get("dist", "mobile_app/dist")))
        if not (dist / "index.html").is_file():
            raise DeployError(f"build produced no {dist}/index.html")
        return dist

    def _ensure_pages_project(self, project: str, env: dict[str, str]) -> None:
        """Create the Cloudflare Pages project over the API if it does not exist (idempotent)."""
        base = f"https://api.cloudflare.com/client/v4/accounts/{env['CLOUDFLARE_ACCOUNT_ID']}/pages/projects"
        headers = {"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"}
        with httpx.Client(timeout=30, headers=headers) as http:
            if http.get(f"{base}/{project}").status_code == 200:
                return
            r = http.post(base, json={"name": project, "production_branch": "main"})
            if r.status_code >= 400:
                raise DeployError(f"Pages project create failed: {r.status_code} {r.text[:300]}")

    def deploy_pwa(self, *, target: str = "pages", hub_url: str | None = None) -> str:
        pwa = self.cfg.section("pwa")
        edge = self.state.urls.get("edge")
        hub_base = hub_url or (f"{edge}/api" if edge else self.state.urls.get("hub"))
        if not hub_base:
            raise DeployError("hub URL unknown: deploy-hub (and ideally deploy-worker) first, or pass --hub-url")
        dist = self.build_pwa(hub_base)
        if target == "pages":
            project = str(pwa.get("pages_project", "omnifree-pwa"))
            env = self._cloudflare_env()
            self._ensure_pages_project(project, env)
            # Neutral cwd: recent wrangler versions auto-generate a wrangler.jsonc in the working directory.
            with tempfile.TemporaryDirectory() as neutral:
                proc = self.runner.run([*self._cli("wrangler@4", "wrangler"), "pages", "deploy", str(dist),
                                        f"--project-name={project}", "--branch=main", "--commit-dirty=true"],
                                       env=env, capture=True, cwd=neutral)
            self.echo((proc.stdout or "").strip()[-800:])
            url = f"https://{project}.pages.dev"
        elif target == "vercel":
            project = str(pwa.get("vercel_project", "omnifree-pwa"))
            proj = self._vercel_project(project)
            team = self.optional("VERCEL_TEAM_ID")
            site = self._spa_copy(dist, self.cfg.path("data/pwa-vercel"))
            cmd = [*self._cli("vercel@latest", "vercel"), "deploy", str(site), "--prod", "--yes",
                   f"--token={self.require('VERCEL_TOKEN')}"]
            if team:
                cmd.append(f"--scope={team}")
            self.runner.run(cmd, capture=True,
                            env={"VERCEL_PROJECT_ID": proj["id"], "VERCEL_ORG_ID": team or proj.get("accountId", "")})
            url = f"https://{project}.vercel.app"
        elif target == "netlify":
            name = str((self.cfg.plugin_settings("netlify").get("sites") or ["omnifree-pwa"])[0])
            site = self._netlify_site(name)
            folder = self._spa_copy(dist, self.cfg.path("data/pwa-netlify"))
            proc = self.runner.run([*self._cli("netlify-cli", "netlify"), "deploy", "--prod", f"--dir={folder}",
                                    f"--site={site}", "--json"],
                                   env={"NETLIFY_AUTH_TOKEN": self.require("NETLIFY_TOKEN")}, capture=True)
            try:
                url = str(json.loads(proc.stdout or "{}").get("url") or "")
            except ValueError:
                url = ""
        else:
            raise DeployError(f"unknown PWA target {target}")
        if url:
            if target == "pages" or not self.state.urls.get("pwa"):
                self.state.set_url("pwa", url)
            else:
                mirrors = self.state.data.setdefault("pwa_mirrors", [])
                if url not in mirrors:
                    mirrors.append(url)
                self.state.save()
            self.echo(f"PWA ({target}): {url}")
        return url

    # ------------------------------------------------------------------ deploy-monitor
    def _monitor_env(self) -> dict[str, str]:
        env = {"HUB_URL": self.state.urls.get("hub", ""), "EDGE_URL": self.state.urls.get("edge", ""),
               "PWA_URL": self.state.urls.get("pwa", "")}
        supa = self.cfg.plugin_settings("supabase").get("project_url")
        if supa:
            env["SUPABASE_URL"] = str(supa)
        for name in ("HUB_API_TOKEN", "HEALTHMONITOR_STATUS_TOKEN", "HEALTHMONITOR_ALERT_WEBHOOK_URL",
                     "HEALTHMONITOR_HEARTBEAT_URL",
                     "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SUPABASE_SERVICE_ROLE_KEY"):
            value = self.optional(name)
            if value:
                env[name] = value
        return {k: v for k, v in env.items() if v}

    async def deploy_monitor(self, *, target: str = "gce", project: str | None = None, repo: str | None = None) -> None:
        mon = self.cfg.section("monitor")
        if target == "gce":
            project = self.gcp_project(project)
            name = str(mon.get("gce_instance", "gcp-monitor"))
            zone = str(mon.get("gce_zone", "us-central1-a"))
            _, _, pub = self.ssh_paths()
            user, _, _ = self.ssh_paths()
            exists = self.runner.run(["gcloud", "compute", "instances", "describe", name, f"--zone={zone}",
                                      f"--project={project}", "--quiet"], capture=True, check=False).returncode == 0
            if not exists:
                ssh_key = pub.read_text(encoding="utf-8").strip()
                with tempfile.TemporaryDirectory(prefix="omnifree-gce-") as tmp:
                    keys_file = Path(tmp) / "ssh-keys"
                    keys_file.write_text(f"{user}:{ssh_key}\n", encoding="utf-8")
                    self.gcloud("compute", "instances", "create", name, f"--zone={zone}", "--machine-type=e2-micro",
                                "--image-family=ubuntu-2404-lts-amd64", "--image-project=ubuntu-os-cloud",
                                "--boot-disk-size=30GB", "--boot-disk-type=pd-standard",
                                f"--metadata-from-file=user-data={self.cfg.path('deploy/cloud-init/gcp-monitor.yaml')},"
                                f"ssh-keys={keys_file}", project=project)
            ip = self.gcloud("compute", "instances", "describe", name, f"--zone={zone}",
                             "--format=value(networkInterfaces[0].accessConfigs[0].natIP)", project=project, capture=True)
            self.state.instances.setdefault(name, {})["public_ip"] = ip
            self.state.save()
            self.echo(f"{name}: {ip}. If it is new, run: python -m automation.deploy ssh-bootstrap --hosts {name}")
            remote = await self.remote(name)
            try:
                await self.upload_bundle(remote, ["core", "healthmonitor", "deploy/healthmonitor"])
                env_text = "".join(f"{k}={v}\n" for k, v in self._monitor_env().items())
                await self.compose_up(remote, "deploy/healthmonitor", {}, build=True, upload=False,
                                      env_text="# Generated by automation.deploy (secrets, mode 600)\n" + env_text)
            finally:
                await remote.close()
        elif target == "koyeb":
            self.echo("warning: Koyeb has no free tier for new signups since 2026 (Pro starts at $29/mo). "
                      "Use --target gce (GCP e2-micro, Always Free) unless your org kept the legacy free instance.")
            token = self.require("KOYEB_API_TOKEN")
            gh = self.cfg.plugin_settings("github_actions")
            repo = repo or (f"{gh.get('owner')}/{gh.get('repo')}" if gh.get("owner") else None)
            if not repo:
                raise DeployError("pass --repo owner/name (Koyeb builds the Dockerfile from GitHub)")
            app = str(mon.get("koyeb_app", "omnifree-worker"))
            env = {"KOYEB_TOKEN": token}
            cmd = ["koyeb", "app", "init", app, f"--git=github.com/{repo}", "--git-branch=main",
                   "--git-builder=docker", "--git-docker-dockerfile=deploy/healthmonitor/Dockerfile",
                   "--instance-type=free", "--ports=8000:http", "--routes=/:8000"]
            for key, value in self._monitor_env().items():
                if key in ("HUB_URL", "EDGE_URL", "PWA_URL", "SUPABASE_URL"):
                    cmd.append(f"--env={key}={value}")
                else:
                    secret_name = "omnifree-" + key.lower().replace("_", "-")
                    self.runner.run(["koyeb", "secrets", "create", secret_name, f"--value={value}"], env=env,
                                    check=False, capture=True)
                    cmd.append(f"--env={key}=@{secret_name}")
            if self.runner.run(cmd, env=env, check=False, capture=True).returncode != 0:
                self.runner.run(["koyeb", "services", "redeploy", f"{app}/{app}"], env=env)
            self.echo(f"Koyeb app {app} deployed from github.com/{repo}")
        else:
            raise DeployError(f"unknown monitor target {target}")

    # ------------------------------------------------------------------ budgets
    async def budgets(self, *, email: str, amount: float = 1.0, project: str | None = None,
                      oci_module: Any = None, boto3_module: Any = None, skip: Sequence[str] = ()) -> dict[str, str]:
        results: dict[str, str] = {}
        if "oci" not in skip:
            try:
                prov = self._provisioner(oci_module)
                results["oci"] = "ok " + await prov.ensure_budget(amount, email)
            except DeployError as exc:
                results["oci"] = f"error: {exc}"
        if "gcp" not in skip:
            try:
                p = self.gcp_project(project)
                account = self.gcloud("billing", "projects", "describe", p, "--format=value(billingAccountName)",
                                      project=p, capture=True).rsplit("/", 1)[-1]
                if not account:
                    raise DeployError("project has no billing account linked")
                listed = self.runner.run(["gcloud", "billing", "budgets", "list", f"--billing-account={account}",
                                          "--format=value(displayName)", "--quiet"], capture=True, check=False)
                if "omnifree-budget" in (listed.stdout or ""):
                    results["gcp"] = "exists"
                else:
                    self.runner.run(["gcloud", "billing", "budgets", "create", f"--billing-account={account}",
                                     "--display-name=omnifree-budget", f"--budget-amount={amount:g}USD",
                                     "--threshold-rule=percent=0.5", "--threshold-rule=percent=1.0", "--quiet"])
                    results["gcp"] = "ok (alerts go to billing admins)"
            except DeployError as exc:
                results["gcp"] = f"error: {exc}"
        if "aws" not in skip:
            results["aws"] = await asyncio.to_thread(self._aws_budget, email, amount, boto3_module)
        results["b2"] = ("manual: Backblaze has no budget API; set caps at "
                         "https://secure.backblaze.com/b2_caps_alerts.htm")
        for k, v in results.items():
            self.echo(f"{k:4} {v}")
        return results

    def _aws_budget(self, email: str, amount: float, boto3_module: Any = None) -> str:
        try:
            boto3 = boto3_module
            if boto3 is None:
                import boto3  # type: ignore[no-redef]
        except ImportError:
            return "error: pip install boto3"
        try:
            session = boto3.session.Session(aws_access_key_id=self.require("AWS_ACCESS_KEY_ID"),
                                            aws_secret_access_key=self.require("AWS_SECRET_ACCESS_KEY"),
                                            region_name="us-east-1")
        except DeployError as exc:
            return f"error: {exc}"
        account = session.client("sts").get_caller_identity()["Account"]
        client = session.client("budgets")
        try:
            client.create_budget(
                AccountId=account,
                Budget={"BudgetName": "omnifree-budget", "BudgetLimit": {"Amount": f"{amount:g}", "Unit": "USD"},
                        "TimeUnit": "MONTHLY", "BudgetType": "COST"},
                NotificationsWithSubscribers=[{
                    "Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
                                     "Threshold": 100.0, "ThresholdType": "PERCENTAGE"},
                    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
                }],
            )
        except Exception as exc:
            if "DuplicateRecord" in type(exc).__name__ or "DuplicateRecord" in str(exc):
                return "exists"
            return f"error: {exc}"
        return "ok"

    # ------------------------------------------------------------------ verify / qr
    async def verify(self, *, hub_url: str | None = None, count: int = 8, burst: int = 0, model: str | None = None,
                     expect_failover: bool = False) -> VerifyReport:
        hub = hub_url or self.state.urls.get("hub")
        if not hub:
            raise DeployError("hub URL unknown: pass --hub-url or run deploy-hub")
        chain = self.cfg.llm_chain()
        self.echo(f"verifying {hub}{self.cfg.section('hub').get('chat_path', '/llm/chat')} "
                  f"(chain: {' > '.join(chain)})")
        report = await verify_failover(hub, self.secret("HUB_API_TOKEN"), chain, count=count, burst=burst, model=model,
                                       chat_path=str(self.cfg.section("hub").get("chat_path", "/llm/chat")),
                                       transport=self.http_transport, echo=self.echo)
        if expect_failover and report.failovers == 0 and len(report.routed) < 2:
            report.violations.append("expected at least one failover (try --burst 40) but every request was "
                                     "served by the first provider")
        self.echo(report.summary())
        return report

    def final_url(self, explicit: str | None = None) -> str:
        url = explicit or self.state.urls.get("edge") or self.state.urls.get("pwa") or self.state.urls.get("hub")
        if not url:
            raise DeployError("no deployed URL recorded; pass --url")
        return url


# ====================================================================== CLI


def _parse_hosts(values: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        name, sep, addr = v.partition("=")
        if not sep or not name or not addr:
            raise SystemExit(f"--host expects NAME=ADDRESS, got {v!r}")
        out[name] = addr
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m automation.deploy", description="OmniFree deployment orchestrator")
    p.add_argument("--config", default=str(PROJECT_ROOT / "deploy" / "deploy.yaml"))
    p.add_argument("--providers", default=str(PROJECT_ROOT / "config" / "providers.yaml"))
    p.add_argument("--state", default=str(PROJECT_ROOT / "data" / "deploy_state.json"))
    p.add_argument("--host", action="append", default=[], metavar="NAME=ADDR", help="override a host address")
    p.add_argument("--via", choices=["tailnet", "public"], default="tailnet", help="reach VMs by MagicDNS or public IP")
    p.add_argument("--dry-run", action="store_true", help="print CLI commands without running them")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ssh-keygen", help="create the ed25519 key pair from deploy.yaml ssh paths")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("oci-provision", help="create VCN, ARM A1 + 2 AMD micro VMs, block volume")
    s.add_argument("--only", help="comma-separated instance names")
    s = sub.add_parser("ssh-bootstrap", help="docker + tailscale up + block volume mount")
    s.add_argument("--hosts", default="oracle-arm,amd-vm1,amd-vm2")
    s.add_argument("--bootstrap-via", choices=["tailnet", "public"], default="public")
    sub.add_parser("lock-ssh", help="remove the public tcp/22 rule from the OCI security list")
    s = sub.add_parser("deploy-arm", help="FreeLLMAPI + Ollama + Instinct (+ WebDAV) on oracle-arm")
    s.add_argument("--hub-url")
    s.add_argument("--skip-webdav", action="store_true")
    s = sub.add_parser("deploy-pihole", help="Pi-hole on AMD VM1")
    s.add_argument("--set-tailnet-dns", action="store_true", help="make Pi-hole the tailnet DNS server")
    s = sub.add_parser("deploy-edge", help="frps (+ WireGuard) on AMD VM2")
    s.add_argument("--wireguard", action="store_true", default=None)
    s = sub.add_parser("deploy-hub", help="build + deploy hub_api to Cloud Run (gcloud), Vercel (serverless), Render or a Hugging Face Space")
    s.add_argument("--target", choices=["cloudrun", "vercel", "render", "hf"], default="cloudrun")
    s.add_argument("--space", help="hf target: owner/name of the Space (default <user>/omnifree-hub)")
    s.add_argument("--project")
    s.add_argument("--region")
    s.add_argument("--cors", action="append", default=[])
    s.add_argument("--activate-sa", action="store_true", help="gcloud auth with GCP_SERVICE_ACCOUNT_JSON first")
    s.add_argument("--tag")
    s.add_argument("--no-tailscale", action="store_true")
    s = sub.add_parser("deploy-worker", help="Cloudflare Worker edge router (wrangler)")
    s.add_argument("--hub-url")
    s.add_argument("--pwa-origin", action="append", default=[])
    s = sub.add_parser("deploy-pwa", help="build mobile_app and publish it")
    s.add_argument("--target", choices=["pages", "vercel", "netlify"], default="pages")
    s.add_argument("--hub-url", help="base URL baked into the PWA (default: <edge>/api or the hub URL)")
    s = sub.add_parser("deploy-monitor",
                       help="health monitor on the GCP e2-micro (default) or Koyeb (optional, paid for new signups)")
    s.add_argument("--target", choices=["gce", "koyeb"], default="gce")
    s.add_argument("--project")
    s.add_argument("--repo", help="owner/name (Koyeb, legacy free orgs only)")
    s = sub.add_parser("budgets", help="$1 budget alerts on OCI, GCP and AWS")
    s.add_argument("--email", default=os.environ.get("OMNIFREE_BILLING_EMAIL"))
    s.add_argument("--amount", type=float, default=1.0)
    s.add_argument("--project")
    s.add_argument("--skip", action="append", default=[], choices=["oci", "gcp", "aws"])
    s = sub.add_parser("verify", help="hit /llm/chat repeatedly and check the failover chain")
    s.add_argument("--hub-url")
    s.add_argument("--count", type=int, default=8)
    s.add_argument("--burst", type=int, default=0)
    s.add_argument("--model")
    s.add_argument("--expect-failover", action="store_true")
    s = sub.add_parser("qr", help="print the final URL and a QR code")
    s.add_argument("--url")
    s.add_argument("--svg", help="also write an SVG QR code to this path")
    sub.add_parser("status", help="print data/deploy_state.json")
    return p


async def _dispatch(args: argparse.Namespace, deployer: Deployer) -> int:
    cmd = args.cmd
    if cmd == "ssh-keygen":
        print(deployer.ssh_keygen(overwrite=args.force))
    elif cmd == "oci-provision":
        await deployer.oci_provision([x for x in (args.only or "").split(",") if x] or None)
    elif cmd == "ssh-bootstrap":
        await deployer.ssh_bootstrap([h for h in args.hosts.split(",") if h], via=args.bootstrap_via)
    elif cmd == "lock-ssh":
        await deployer.lock_ssh()
    elif cmd == "deploy-arm":
        await deployer.deploy_arm(hub_url=args.hub_url, skip_webdav=args.skip_webdav)
    elif cmd == "deploy-pihole":
        await deployer.deploy_pihole(set_tailnet_dns=args.set_tailnet_dns)
    elif cmd == "deploy-edge":
        await deployer.deploy_edge(wireguard=args.wireguard)
    elif cmd == "deploy-hub" and args.target == "vercel":
        await asyncio.to_thread(deployer.deploy_hub_vercel, cors=args.cors)
    elif cmd == "deploy-hub" and args.target == "render":
        await asyncio.to_thread(deployer.deploy_hub_render, cors=args.cors, region=args.region)
    elif cmd == "deploy-hub" and args.target == "hf":
        await asyncio.to_thread(deployer.deploy_hub_hf, space=args.space, cors=args.cors)
    elif cmd == "deploy-hub":
        await asyncio.to_thread(deployer.deploy_hub, project=args.project, region=args.region, cors=args.cors,
                                activate_sa=args.activate_sa, tag=args.tag, tailscale=not args.no_tailscale)
    elif cmd == "deploy-worker":
        await asyncio.to_thread(deployer.deploy_worker, hub_url=args.hub_url, pwa_origins=args.pwa_origin)
    elif cmd == "deploy-pwa":
        await asyncio.to_thread(deployer.deploy_pwa, target=args.target, hub_url=args.hub_url)
    elif cmd == "deploy-monitor":
        await deployer.deploy_monitor(target=args.target, project=args.project, repo=args.repo)
    elif cmd == "budgets":
        if not args.email:
            raise DeployError("pass --email (or set OMNIFREE_BILLING_EMAIL) for budget alert notifications")
        results = await deployer.budgets(email=args.email, amount=args.amount, project=args.project, skip=args.skip)
        return 0 if not any(v.startswith("error") for v in results.values()) else 1
    elif cmd == "verify":
        report = await deployer.verify(hub_url=args.hub_url, count=args.count, burst=args.burst, model=args.model,
                                       expect_failover=args.expect_failover)
        return 0 if report.passed else 1
    elif cmd == "qr":
        render_qr(deployer.final_url(args.url), svg_path=Path(args.svg) if args.svg else None)
    elif cmd == "status":
        print(json.dumps(deployer.state.data, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    # CLI output (wrangler/vercel logs) contains emoji; a legacy Windows console codepage must not crash us.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    from core.config import get_settings

    settings = get_settings()
    config = DeployConfig.load(Path(args.config), Path(args.providers))
    state = DeployState.load(Path(args.state))
    runner = CommandRunner(dry_run=args.dry_run)
    deployer = Deployer(config, state, settings.secret, runner=runner, host_overrides=_parse_hosts(args.host),
                        via=args.via)
    try:
        return asyncio.run(_dispatch(args, deployer))
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
