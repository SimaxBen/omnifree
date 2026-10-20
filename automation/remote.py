"""SSH access to the VMs for automation.deploy.

Backends, imported lazily: ``asyncssh`` when installed, otherwise ``paramiko`` (run in a
worker thread). Host keys are trusted on first use and pinned in ``data/known_hosts``.
Secrets are always uploaded as files with mode 600 (never placed on a command line).
"""

from __future__ import annotations

import asyncio
import io
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from automation.deploy_common import PROJECT_ROOT, DeployError

__all__ = ["CommandResult", "Remote", "SSHRemote", "connect"]

KNOWN_HOSTS = PROJECT_ROOT / "data" / "known_hosts"


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class Remote(Protocol):
    host: str

    async def run(self, command: str, *, input_text: str | None = None, check: bool = True) -> CommandResult: ...
    async def put_bytes(self, data: bytes, remote_path: str, mode: int = 0o644) -> None: ...
    async def close(self) -> None: ...


async def run_script(remote: Remote, script: str, *, sudo: bool = True, check: bool = True) -> CommandResult:
    """Run a bash script fed on stdin (``set -euo pipefail`` is prepended)."""
    body = "set -euo pipefail\n" + script
    return await remote.run(("sudo " if sudo else "") + "bash -s", input_text=body, check=check)


async def put_secret_file(remote: Remote, data: str | bytes, remote_path: str) -> None:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    await remote.put_bytes(payload, remote_path, mode=0o600)


class SSHRemote:
    def __init__(self, host: str, user: str, key_path: Path, *, port: int = 22, timeout_s: float = 20.0,
                 known_hosts: Path = KNOWN_HOSTS) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port
        self.timeout_s = timeout_s
        self.known_hosts = known_hosts
        self._backend: str | None = None
        self._conn: Any = None

    # ------------------------------------------------------------------ connect
    async def connect(self) -> SSHRemote:
        if not self.key_path.is_file():
            raise DeployError(f"SSH private key {self.key_path} not found (run: python -m automation.deploy ssh-keygen)")
        try:
            import asyncssh  # noqa: F401

            self._backend = "asyncssh"
            await self._connect_asyncssh()
        except ImportError:
            self._backend = "paramiko"
            await asyncio.to_thread(self._connect_paramiko)
        return self

    async def _connect_asyncssh(self) -> None:
        import asyncssh

        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        known: Any = str(self.known_hosts) if self._host_pinned() else None
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(self.host, port=self.port, username=self.user,
                                 client_keys=[str(self.key_path)], known_hosts=known),
                timeout=self.timeout_s,
            )
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
            raise DeployError(f"ssh {self.user}@{self.host}: {exc}") from exc
        if known is None:  # trust on first use: pin the presented key
            key = self._conn.get_server_host_key()
            if key is not None:
                entry = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
                with self.known_hosts.open("a", encoding="utf-8") as fh:
                    fh.write(f"{entry} {key.export_public_key().decode().strip()}\n")

    def _host_pinned(self) -> bool:
        if not self.known_hosts.exists():
            return False
        entry = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
        return any(line.split(" ", 1)[0] == entry for line in self.known_hosts.read_text(encoding="utf-8").splitlines())

    def _connect_paramiko(self) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise DeployError("install an SSH library: pip install asyncssh (or paramiko)") from exc
        client = paramiko.SSHClient()
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if self.known_hosts.exists():
            client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.user, key_filename=str(self.key_path),
                           timeout=self.timeout_s, banner_timeout=self.timeout_s, auth_timeout=self.timeout_s,
                           look_for_keys=False, allow_agent=False)
        except (OSError, paramiko.SSHException) as exc:
            raise DeployError(f"ssh {self.user}@{self.host}: {exc}") from exc
        client.save_host_keys(str(self.known_hosts))
        self._conn = client

    # ------------------------------------------------------------------ operations
    async def run(self, command: str, *, input_text: str | None = None, check: bool = True) -> CommandResult:
        if self._conn is None:
            raise DeployError("not connected")
        if self._backend == "asyncssh":
            res = await self._conn.run(command, input=input_text, check=False)
            result = CommandResult(int(res.exit_status or 0), str(res.stdout or ""), str(res.stderr or ""))
        else:
            result = await asyncio.to_thread(self._run_paramiko, command, input_text)
        if check and result.exit_status != 0:
            first_line = command.splitlines()[0][:120]
            raise DeployError(f"[{self.host}] '{first_line}' exited {result.exit_status}: "
                              f"{(result.stderr or result.stdout).strip()[-1500:]}")
        return result

    def _run_paramiko(self, command: str, input_text: str | None) -> CommandResult:
        stdin, stdout, stderr = self._conn.exec_command(command)
        if input_text is not None:
            stdin.write(input_text)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return CommandResult(stdout.channel.recv_exit_status(), out, err)

    async def put_bytes(self, data: bytes, remote_path: str, mode: int = 0o644) -> None:
        if self._backend == "asyncssh":
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "wb") as fh:
                    await fh.write(data)
                await sftp.chmod(remote_path, mode)
        else:
            await asyncio.to_thread(self._put_paramiko, data, remote_path, mode)

    def _put_paramiko(self, data: bytes, remote_path: str, mode: int) -> None:
        sftp = self._conn.open_sftp()
        try:
            sftp.putfo(io.BytesIO(data), remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    async def close(self) -> None:
        if self._conn is None:
            return
        if self._backend == "asyncssh":
            self._conn.close()
            await self._conn.wait_closed()
        else:
            await asyncio.to_thread(self._conn.close)
        self._conn = None


async def connect(host: str, user: str, key_path: Path, **kw: Any) -> SSHRemote:
    return await SSHRemote(host, user, key_path, **kw).connect()


def q(value: str) -> str:
    return shlex.quote(value)
