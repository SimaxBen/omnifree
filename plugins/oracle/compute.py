"""OCI Always Free compute instances (A1 ARM + two AMD micro VMs) over the signed Core Services REST API.

Requests are signed with the API signing key (see ``_signer.py``) and sent on ``httpx.AsyncClient``,
so no ``oci`` SDK is needed (the SDK is synchronous and ~100MB).

* list: ``GET /20160918/instances?compartmentId=...`` (paginated via ``opc-next-page``; TERMINATED
  instances are hidden unless ``settings.include_terminated``)
* status: ``GET /20160918/instances/{id}`` + VNIC lookup for public/private IPs
* start/stop: ``POST /20160918/instances/{id}?action=START|SOFTSTOP`` (``settings.stop_action``)

Instance ids are OCIDs; ``status/start/stop`` also accept a display name (e.g. ``oracle-arm``).
Settings: ``region`` (required), ``compartment_ocid`` (defaults to the tenancy = root compartment),
``instances`` (display name -> role/shape, used for tags), ``resolve_ips`` (default true),
``stop_action`` (``SOFTSTOP`` default, or ``STOP``), ``endpoint`` (override of
``https://iaas.{region}.oraclecloud.com``).
Optional secret alias ``private_key_passphrase`` is used when the key is encrypted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from core.interfaces import (
    Capability,
    ComputeAdapter,
    HealthStatus,
    PluginMeta,
    ProviderError,
    VMInstance,
    raise_for_response,
    translate_transport_error,
)
from plugins.oracle._signer import OCISigner

_API_VERSION = "20160918"

_STATE = {
    "PROVISIONING": "provisioning",
    "STARTING": "starting",
    "RUNNING": "running",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "CREATING_IMAGE": "running",
    "MOVING": "provisioning",
    "TERMINATING": "stopping",
    "TERMINATED": "terminated",
}


class OCIComputeAdapter(ComputeAdapter):
    capabilities: ClassVar[frozenset[str]] = frozenset({Capability.POWER})
    meta: ClassVar[PluginMeta] = PluginMeta(
        display_name="Oracle Cloud compute",
        description="OCI Always Free A1 ARM and AMD micro instances.",
        required_secrets=("tenancy_ocid", "user_ocid", "fingerprint", "private_key"),
        optional_secrets=("private_key_passphrase",),
        docs_url="https://docs.oracle.com/en-us/iaas/api/#/en/iaas/20160918/Instance/",
    )

    _signer: OCISigner | None = None

    def _get_signer(self) -> OCISigner:
        if self._signer is None:
            self._signer = OCISigner(
                tenancy_ocid=self.require_secret("tenancy_ocid"),
                user_ocid=self.require_secret("user_ocid"),
                fingerprint=self.require_secret("fingerprint"),
                private_key_pem=self.require_secret("private_key"),
                passphrase=self.secret("private_key_passphrase"),
            )
        return self._signer

    def _endpoint(self) -> str:
        region = self.require_setting("region")
        return str(self.setting("endpoint", f"https://iaas.{region}.oraclecloud.com")).rstrip("/")

    def _compartment(self) -> str:
        return str(self.setting("compartment_ocid") or self.require_secret("tenancy_ocid"))

    async def _oci(self, method: str, path: str, params: dict[str, Any] | None = None,
                   body: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self._endpoint()}/{_API_VERSION}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        content = json.dumps(body).encode("utf-8") if body is not None else (b"" if method in ("POST", "PUT") else None)
        try:
            headers = self._get_signer().sign(method, url, content)
        except ValueError as exc:  # bad PEM / passphrase
            raise ProviderError(f"cannot load OCI private key: {exc}", provider=self.name) from exc
        try:
            response = await self.http.request(method, url, headers=headers, content=content)
        except httpx.HTTPError as exc:
            raise translate_transport_error(exc, self.name) from exc
        if response.status_code == 404:
            raise ProviderError(f"not found: {path}", provider=self.name, status_code=404)
        raise_for_response(response, self.name)
        return response

    async def _vnic_ips(self, instance_id: str) -> tuple[str | None, str | None]:
        attachments = (await self._oci(
            "GET", "vnicAttachments", {"compartmentId": self._compartment(), "instanceId": instance_id}
        )).json()
        for att in attachments:
            if att.get("lifecycleState") == "ATTACHED" and att.get("vnicId"):
                vnic = (await self._oci("GET", f"vnics/{att['vnicId']}")).json()
                return vnic.get("publicIp"), vnic.get("privateIp")
        return None, None

    def _to_instance(self, item: dict[str, Any], ips: tuple[str | None, str | None] = (None, None)) -> VMInstance:
        name = str(item.get("displayName", ""))
        configured = (self.setting("instances") or {}).get(name) or {}
        tags = {str(k): str(v) for k, v in (item.get("freeformTags") or {}).items()}
        if configured.get("role"):
            tags.setdefault("role", str(configured["role"]))
        shape_cfg = item.get("shapeConfig") or {}
        return VMInstance(
            id=str(item.get("id", "")),
            name=name,
            provider=self.name,
            state=_STATE.get(str(item.get("lifecycleState", "")), "unknown"),  # type: ignore[arg-type]
            region=item.get("region") or self.setting("region"),
            shape=item.get("shape"),
            public_ip=ips[0],
            private_ip=ips[1],
            tags=tags,
            meta={
                "availability_domain": item.get("availabilityDomain"),
                "lifecycle_state": item.get("lifecycleState"),
                "ocpus": shape_cfg.get("ocpus"),
                "memory_gb": shape_cfg.get("memoryInGBs"),
                "time_created": item.get("timeCreated"),
            },
        )

    async def _with_ips(self, item: dict[str, Any]) -> VMInstance:
        ips: tuple[str | None, str | None] = (None, None)
        if self.setting("resolve_ips", True) and item.get("lifecycleState") not in ("TERMINATED", "TERMINATING"):
            try:
                ips = await self._vnic_ips(str(item["id"]))
            except ProviderError:
                ips = (None, None)  # IPs are informational; never fail the listing for them
        return self._to_instance(item, ips)

    async def _list_raw(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"compartmentId": self._compartment(), "limit": 100}
        while True:
            response = await self._oci("GET", "instances", params)
            items.extend(response.json())
            page = response.headers.get("opc-next-page")
            if not page:
                break
            params["page"] = page
        if not self.setting("include_terminated", False):
            items = [i for i in items if i.get("lifecycleState") != "TERMINATED"]
        return items

    async def _resolve_id(self, instance_id: str) -> str:
        if instance_id.startswith("ocid1."):
            return instance_id
        for item in await self._list_raw():
            if item.get("displayName") == instance_id:
                return str(item["id"])
        raise ProviderError(f"instance not found: {instance_id}", provider=self.name, status_code=404)

    async def health(self) -> HealthStatus:
        return await self.probe(
            lambda: self._oci("GET", "instances", {"compartmentId": self._compartment(), "limit": 1})
        )

    async def list_instances(self) -> list[VMInstance]:
        return list(await asyncio.gather(*(self._with_ips(i) for i in await self._list_raw())))

    async def status(self, instance_id: str) -> VMInstance:
        ocid = await self._resolve_id(instance_id)
        return await self._with_ips((await self._oci("GET", f"instances/{ocid}")).json())

    async def _action(self, instance_id: str, action: str) -> VMInstance:
        ocid = await self._resolve_id(instance_id)
        response = await self._oci("POST", f"instances/{ocid}", {"action": action})
        return self._to_instance(response.json())

    async def start(self, instance_id: str) -> VMInstance:
        return await self._action(instance_id, "START")

    async def stop(self, instance_id: str) -> VMInstance:
        action = str(self.setting("stop_action", "SOFTSTOP")).upper()
        if action not in ("STOP", "SOFTSTOP"):
            raise ProviderError("stop_action must be STOP or SOFTSTOP", provider=self.name, status_code=400)
        return await self._action(instance_id, action)
