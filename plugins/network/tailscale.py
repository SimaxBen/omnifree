"""Tailscale adapter (API v2): tailnet devices map to instances with state ``online``/``offline``.

Online detection uses ``connectedToControl`` when the API returns it (``fields=all``) and
otherwise ``lastSeen`` within ``settings.online_threshold_s`` (default 300 s).
No power capability.

Settings: ``tailnet`` (default ``-`` = the API key's tailnet), ``api_base``
(default ``https://api.tailscale.com/api/v2``), ``online_threshold_s``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar
from urllib.parse import quote

from core.interfaces import ComputeAdapter, HealthStatus, PluginMeta, ProviderError, VMInstance


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class TailscaleAdapter(ComputeAdapter):
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Tailscale",
        description="Tailscale tailnet devices (mesh node status).",
        required_secrets=("api_key",),
        docs_url="https://tailscale.com/api",
    )

    def _base(self) -> str:
        return str(self.setting("api_base", "https://api.tailscale.com/api/v2")).rstrip("/")

    async def _call(self, path: str, **kwargs: Any) -> Any:
        response = await self.request(
            "GET", self._base() + path,
            headers={"Authorization": f"Bearer {self.require_secret('api_key')}"}, **kwargs,
        )
        return response.json()

    def _instance(self, device: dict[str, Any]) -> VMInstance:
        last_seen = _parse_dt(device.get("lastSeen"))
        if "connectedToControl" in device:
            online = bool(device["connectedToControl"])
        else:
            threshold = float(self.setting("online_threshold_s", 300))
            online = bool(last_seen and (datetime.now(timezone.utc) - last_seen).total_seconds() <= threshold)
        addresses = [str(a) for a in device.get("addresses") or []]
        ipv4 = next((a for a in addresses if "." in a), None)
        full_name = str(device.get("name") or "")
        return VMInstance(
            id=str(device.get("nodeId") or device.get("id")),
            name=str(device.get("hostname") or full_name.split(".", 1)[0] or device.get("id")),
            provider=self.name,
            state="online" if online else "offline",
            tailscale_ip=ipv4 or (addresses[0] if addresses else None),
            tags={t.removeprefix("tag:"): "" for t in device.get("tags") or []},
            meta={
                "device_id": device.get("id"),
                "dns_name": full_name or None,
                "os": device.get("os"),
                "addresses": addresses,
                "client_version": device.get("clientVersion"),
                "update_available": device.get("updateAvailable"),
                "last_seen": device.get("lastSeen"),
                "authorized": device.get("authorized"),
                "key_expiry": device.get("expires"),
                "key_expiry_disabled": device.get("keyExpiryDisabled"),
            },
        )

    async def _devices(self) -> list[dict[str, Any]]:
        tailnet = quote(str(self.setting("tailnet", "-")), safe="")
        payload = await self._call(f"/tailnet/{tailnet}/devices", params={"fields": "all"})
        return payload.get("devices") or []

    # ------------------------------------------------------------------ port
    async def health(self) -> HealthStatus:
        return await self.probe(self._devices)

    async def list_instances(self) -> list[VMInstance]:
        return sorted((self._instance(d) for d in await self._devices()), key=lambda i: i.name)

    async def status(self, instance_id: str) -> VMInstance:
        """``instance_id`` may be the node id, the legacy device id, the hostname or the MagicDNS name."""
        for device in await self._devices():
            names = {str(device.get(k)) for k in ("nodeId", "id", "hostname", "name") if device.get(k)}
            names.add(str(device.get("name") or "").split(".", 1)[0])
            if instance_id in names:
                return self._instance(device)
        raise ProviderError(f"device {instance_id} not found", provider=self.name, status_code=404)
