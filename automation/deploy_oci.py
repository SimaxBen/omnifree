"""OCI provisioning for automation.deploy (``oci`` SDK, imported lazily).

Creates, idempotently (looked up by display name first):

* VCN ``omnifree-vcn`` + internet gateway + default route + security list rules from
  ``deploy/deploy.yaml`` ``oci.public_ingress`` + public subnet;
* the three Always Free instances (one VM.Standard.A1.Flex ARM with 2 OCPU / 12 GB, which is the
  whole Always Free A1 allowance since the 2026 cut from 4 OCPU / 24 GB, + two E2.1.Micro), each with its cloud-init
  ``user_data`` and your SSH public key; A1 launches retry across availability domains on
  "Out of host capacity" for ``capacity_retry_minutes``;
* the block volume, paravirtualized-attached to oracle-arm at a consistent device path;
* (``budgets``) a $1 monthly budget with an alert rule.

The SDK is synchronous; every call goes through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from automation.deploy_common import DeployConfig, DeployError, DeployState, SecretLookup, render_cloud_init

__all__ = ["OCIProvisioner", "oci_config_from_secrets", "check_always_free_a1", "A1_SHAPE",
           "A1_FREE_OCPUS", "A1_FREE_MEMORY_GB"]

# Always Free Ampere A1: 1,500 OCPU-hours + 9,000 GB-hours per month = 2 OCPU / 12 GB in total
# (docs.oracle.com Always Free resources, checked 2026-10-19).
A1_SHAPE = "VM.Standard.A1.Flex"
A1_FREE_OCPUS = 2.0
A1_FREE_MEMORY_GB = 12.0


def check_always_free_a1(instances: dict[str, Any]) -> None:
    """Refuse an A1 layout that would leave the Always Free allowance (2 OCPU / 12 GB in total)."""
    ocpus = memory = 0.0
    for spec in instances.values():
        if str((spec or {}).get("shape")) != A1_SHAPE:
            continue
        ocpus += float(spec.get("ocpus", A1_FREE_OCPUS))
        memory += float(spec.get("memory_gb", A1_FREE_MEMORY_GB))
    if ocpus > A1_FREE_OCPUS or memory > A1_FREE_MEMORY_GB:
        raise DeployError(
            f"{A1_SHAPE} instances ask for {ocpus:g} OCPU / {memory:g} GB, but Always Free A1 is "
            f"{A1_FREE_OCPUS:g} OCPU / {A1_FREE_MEMORY_GB:g} GB in total; fix oci.instances in deploy/deploy.yaml"
        )

_LIVE = {"PROVISIONING", "STARTING", "RUNNING", "STOPPING", "STOPPED", "CREATING_IMAGE", "MOVING"}


def _import_oci() -> Any:
    try:
        import oci
    except ImportError as exc:
        raise DeployError("the OCI SDK is required: pip install oci") from exc
    return oci


def oci_config_from_secrets(secret: SecretLookup, region: str) -> dict[str, str]:
    names = {"tenancy": "OCI_TENANCY_OCID", "user": "OCI_USER_OCID", "fingerprint": "OCI_FINGERPRINT",
             "key_content": "OCI_PRIVATE_KEY_PEM"}
    values = {k: secret(v) for k, v in names.items()}
    missing = [names[k] for k, v in values.items() if not v]
    if missing:
        raise DeployError("missing OCI secrets: " + ", ".join(missing))
    return {**{k: str(v) for k, v in values.items()}, "region": region}


def _is_capacity_error(exc: Exception) -> bool:
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')} {exc}".lower()
    return "capacity" in text or "limitexceeded" in text.replace(" ", "")


class OCIProvisioner:
    def __init__(
        self,
        config: DeployConfig,
        state: DeployState,
        secret: SecretLookup,
        *,
        ssh_public_key: str,
        oci_module: Any = None,
        echo: Callable[[str], None] = print,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = config
        self.state = state
        self.secret = secret
        self.ssh_public_key = ssh_public_key.strip()
        self.oci = oci_module or _import_oci()
        self.echo = echo
        self.sleep = sleep
        self.clock = clock
        settings = config.plugin_settings("oracle_compute")
        self.region = str(settings.get("region") or "eu-madrid-1")
        self.sdk_config = oci_config_from_secrets(secret, self.region)
        self.compartment = str(settings.get("compartment_ocid") or self.sdk_config["tenancy"])
        self.oci_section = config.section("oci")
        m = self.oci
        self.compute = m.core.ComputeClient(self.sdk_config)
        self.network = m.core.VirtualNetworkClient(self.sdk_config)
        self.identity = m.identity.IdentityClient(self.sdk_config)
        self.blockstorage = m.core.BlockstorageClient(self.sdk_config)

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _wait(self, client: Any, getter: Callable[[], Any], state: str, max_wait_s: int = 900) -> Any:
        response = await self._call(getter)
        return (await self._call(self.oci.wait_until, client, response, "lifecycle_state", state,
                                 max_wait_seconds=max_wait_s)).data

    # ------------------------------------------------------------------ network
    def _ingress_rules(self) -> list[Any]:
        models = self.oci.core.models
        rules = []
        for rule in self.oci_section.get("public_ingress") or []:
            proto = str(rule.get("protocol", "tcp")).lower()
            port = int(rule["port"])
            rng = models.PortRange(min=port, max=port)
            if proto == "tcp":
                rules.append(models.IngressSecurityRule(protocol="6", source="0.0.0.0/0",
                                                        tcp_options=models.TcpOptions(destination_port_range=rng),
                                                        description=f"omnifree tcp {port}"))
            elif proto == "udp":
                rules.append(models.IngressSecurityRule(protocol="17", source="0.0.0.0/0",
                                                        udp_options=models.UdpOptions(destination_port_range=rng),
                                                        description=f"omnifree udp {port}"))
            else:
                raise DeployError(f"unsupported protocol in oci.public_ingress: {proto}")
        # Path-MTU discovery + intra-VCN ICMP, like OCI's defaults.
        rules.append(models.IngressSecurityRule(protocol="1", source="0.0.0.0/0",
                                                icmp_options=models.IcmpOptions(type=3, code=4)))
        rules.append(models.IngressSecurityRule(protocol="1", source=str(self.oci_section.get("vcn_cidr", "10.42.0.0/16")),
                                                icmp_options=models.IcmpOptions(type=3)))
        return rules

    async def ensure_network(self) -> str:
        models = self.oci.core.models
        name = str(self.oci_section.get("vcn_name", "omnifree-vcn"))
        vcns = (await self._call(self.network.list_vcns, compartment_id=self.compartment, display_name=name)).data
        vcns = [v for v in vcns if v.lifecycle_state not in ("TERMINATING", "TERMINATED")]
        if vcns:
            vcn = vcns[0]
        else:
            self.echo(f"creating VCN {name}")
            created = (await self._call(self.network.create_vcn, models.CreateVcnDetails(
                cidr_blocks=[str(self.oci_section.get("vcn_cidr", "10.42.0.0/16"))],
                compartment_id=self.compartment, display_name=name, dns_label="omnifree"))).data
            vcn = await self._wait(self.network, lambda: self.network.get_vcn(created.id), "AVAILABLE")

        igws = (await self._call(self.network.list_internet_gateways, compartment_id=self.compartment,
                                 vcn_id=vcn.id, display_name="omnifree-igw")).data
        if igws:
            igw = igws[0]
        else:
            self.echo("creating internet gateway")
            igw = (await self._call(self.network.create_internet_gateway, models.CreateInternetGatewayDetails(
                compartment_id=self.compartment, vcn_id=vcn.id, is_enabled=True, display_name="omnifree-igw"))).data

        route = (await self._call(self.network.get_route_table, vcn.default_route_table_id)).data
        if not any(r.destination == "0.0.0.0/0" for r in (route.route_rules or [])):
            await self._call(self.network.update_route_table, vcn.default_route_table_id,
                             models.UpdateRouteTableDetails(route_rules=[*(route.route_rules or []), models.RouteRule(
                                 destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)]))

        await self._call(self.network.update_security_list, vcn.default_security_list_id,
                         models.UpdateSecurityListDetails(
                             ingress_security_rules=self._ingress_rules(),
                             egress_security_rules=[models.EgressSecurityRule(destination="0.0.0.0/0", protocol="all")]))

        subnets = (await self._call(self.network.list_subnets, compartment_id=self.compartment, vcn_id=vcn.id,
                                    display_name="omnifree-public")).data
        subnets = [s for s in subnets if s.lifecycle_state not in ("TERMINATING", "TERMINATED")]
        if subnets:
            subnet = subnets[0]
        else:
            self.echo("creating public subnet")
            created = (await self._call(self.network.create_subnet, models.CreateSubnetDetails(
                compartment_id=self.compartment, vcn_id=vcn.id, display_name="omnifree-public",
                cidr_block=str(self.oci_section.get("subnet_cidr", "10.42.0.0/24")), dns_label="public",
                route_table_id=vcn.default_route_table_id, security_list_ids=[vcn.default_security_list_id],
                prohibit_public_ip_on_vnic=False))).data
            subnet = await self._wait(self.network, lambda: self.network.get_subnet(created.id), "AVAILABLE")
        self.state.data.setdefault("oci", {}).update(vcn_id=vcn.id, subnet_id=subnet.id,
                                                     security_list_id=vcn.default_security_list_id)
        self.state.save()
        return str(subnet.id)

    async def lock_ssh(self) -> None:
        """Remove the public tcp/22 rule once every host is reachable over Tailscale."""
        sl_id = (self.state.data.get("oci") or {}).get("security_list_id")
        if not sl_id:
            raise DeployError("no security list recorded; run oci-provision first")
        models = self.oci.core.models
        current = (await self._call(self.network.get_security_list, sl_id)).data
        kept = [r for r in current.ingress_security_rules
                if not (r.protocol == "6" and r.tcp_options is not None and r.tcp_options.destination_port_range
                        and r.tcp_options.destination_port_range.min == 22)]
        await self._call(self.network.update_security_list, sl_id,
                         models.UpdateSecurityListDetails(ingress_security_rules=kept))
        self.echo(f"public SSH closed ({len(current.ingress_security_rules) - len(kept)} rule(s) removed)")

    # ------------------------------------------------------------------ instances
    async def availability_domains(self) -> list[str]:
        ads = (await self._call(self.identity.list_availability_domains, compartment_id=self.sdk_config["tenancy"])).data
        return [a.name for a in ads]

    async def find_image(self, shape: str) -> str:
        images = (await self._call(
            self.compute.list_images, compartment_id=self.compartment,
            operating_system=str(self.oci_section.get("image_os", "Canonical Ubuntu")),
            operating_system_version=str(self.oci_section.get("image_os_version", "24.04")),
            shape=shape, sort_by="TIMECREATED", sort_order="DESC")).data
        images = [i for i in images if "minimal" not in (i.display_name or "").lower()] or images
        if not images:
            raise DeployError(f"no {self.oci_section.get('image_os')} image for shape {shape} in {self.region}")
        return str(images[0].id)

    def launch_details(self, name: str, spec: dict[str, Any], subnet_id: str, ad: str, image_id: str) -> Any:
        models = self.oci.core.models
        shape_config = None
        if spec.get("ocpus") or str(spec.get("shape")) == A1_SHAPE:
            shape_config = models.LaunchInstanceShapeConfigDetails(
                ocpus=float(spec.get("ocpus", A1_FREE_OCPUS)),
                memory_in_gbs=float(spec.get("memory_gb", A1_FREE_MEMORY_GB)))
        user_data = render_cloud_init(self.cfg.path(str(spec["cloud_init"])))
        return models.LaunchInstanceDetails(
            availability_domain=ad, compartment_id=self.compartment, display_name=name, shape=str(spec["shape"]),
            shape_config=shape_config,
            source_details=models.InstanceSourceViaImageDetails(
                image_id=image_id, boot_volume_size_in_gbs=int(spec.get("boot_volume_gb", 50))),
            create_vnic_details=models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=True, hostname_label="".join(c for c in name if c.isalnum())[:15]),
            metadata={"ssh_authorized_keys": self.ssh_public_key, "user_data": user_data},
            freeform_tags={"project": "omnifree", "role": name},
        )

    async def existing_instance(self, name: str) -> Any | None:
        items = (await self._call(self.compute.list_instances, compartment_id=self.compartment, display_name=name)).data
        live = [i for i in items if i.lifecycle_state in _LIVE]
        return live[0] if live else None

    async def ensure_instance(self, name: str, spec: dict[str, Any], subnet_id: str, ads: list[str]) -> dict[str, Any]:
        inst = await self.existing_instance(name)
        if inst is None:
            image_id = await self.find_image(str(spec["shape"]))
            deadline = self.clock() + 60 * float(self.oci_section.get("capacity_retry_minutes", 30))
            while inst is None:
                last_error: Exception | None = None
                for ad in ads:
                    try:
                        self.echo(f"launching {name} ({spec['shape']}) in {ad}")
                        inst = (await self._call(self.compute.launch_instance,
                                                 self.launch_details(name, spec, subnet_id, ad, image_id))).data
                        break
                    except self.oci.exceptions.ServiceError as exc:
                        if not _is_capacity_error(exc):
                            raise DeployError(f"launch {name} failed: {exc.code} {exc.message}") from exc
                        last_error = exc
                        self.echo(f"  {ad}: out of capacity")
                if inst is None:
                    if self.clock() >= deadline:
                        raise DeployError(f"no capacity for {name} in any AD ({last_error}); retry later or "
                                          "raise oci.capacity_retry_minutes")
                    self.echo("  retrying in 60s (A1 capacity frees up over time)")
                    await self.sleep(60)
        else:
            self.echo(f"{name} already exists ({inst.lifecycle_state})")
        inst = await self._wait(self.compute, lambda: self.compute.get_instance(inst.id), "RUNNING")
        info = {"id": inst.id, "availability_domain": inst.availability_domain, "shape": inst.shape}
        attachments = (await self._call(self.compute.list_vnic_attachments, compartment_id=self.compartment,
                                        instance_id=inst.id)).data
        if attachments:
            vnic = (await self._call(self.network.get_vnic, attachments[0].vnic_id)).data
            info.update(public_ip=vnic.public_ip, private_ip=vnic.private_ip)
        self.state.instances[name] = {**self.state.instances.get(name, {}), **info}
        self.state.save()
        return info

    async def ensure_block_volume(self) -> dict[str, Any] | None:
        spec = dict(self.oci_section.get("block_volume") or {})
        if not spec:
            return None
        models = self.oci.core.models
        target = self.state.instances.get(str(spec.get("attach_to", "oracle-arm")))
        if not target:
            raise DeployError("block volume target instance not provisioned")
        name = str(spec.get("name", "omnifree-block"))
        vols = (await self._call(self.blockstorage.list_volumes, compartment_id=self.compartment, display_name=name)).data
        vols = [v for v in vols if v.lifecycle_state not in ("TERMINATING", "TERMINATED")]
        if vols:
            vol = vols[0]
        else:
            self.echo(f"creating block volume {name} ({spec.get('size_gb', 50)} GB)")
            created = (await self._call(self.blockstorage.create_volume, models.CreateVolumeDetails(
                availability_domain=target["availability_domain"], compartment_id=self.compartment,
                display_name=name, size_in_gbs=int(spec.get("size_gb", 50))))).data
            vol = await self._wait(self.blockstorage, lambda: self.blockstorage.get_volume(created.id), "AVAILABLE")
        attachments = (await self._call(self.compute.list_volume_attachments, compartment_id=self.compartment,
                                        instance_id=target["id"], volume_id=vol.id)).data
        if not any(a.lifecycle_state in ("ATTACHING", "ATTACHED") for a in attachments):
            self.echo(f"attaching {name} to {spec.get('attach_to')}")
            att = (await self._call(self.compute.attach_volume, models.AttachParavirtualizedVolumeDetails(
                instance_id=target["id"], volume_id=vol.id, display_name=f"{name}-attachment",
                device=str(spec.get("device", "/dev/oracleoci/oraclevdb"))))).data
            await self._wait(self.compute, lambda: self.compute.get_volume_attachment(att.id), "ATTACHED")
        info = {"id": vol.id, "device": spec.get("device"), "mount_path": spec.get("mount_path")}
        self.state.data["block_volume"] = info
        self.state.save()
        return info

    async def provision(self, only: list[str] | None = None) -> dict[str, Any]:
        subnet_id = await self.ensure_network()
        ads = await self.availability_domains()
        instances = dict(self.oci_section.get("instances") or {})
        check_always_free_a1(instances)
        results: dict[str, Any] = {}
        for name, spec in instances.items():
            if only and name not in only:
                continue
            results[name] = await self.ensure_instance(name, dict(spec), subnet_id, ads)
        if not only or str((self.oci_section.get("block_volume") or {}).get("attach_to")) in (only or []):
            results["block_volume"] = await self.ensure_block_volume()
        return results

    # ------------------------------------------------------------------ budget
    async def ensure_budget(self, amount_usd: float, email: str) -> str:
        budget_mod = self.oci.budget
        client = budget_mod.BudgetClient(self.sdk_config)
        tenancy = self.sdk_config["tenancy"]
        existing = (await self._call(client.list_budgets, compartment_id=tenancy, display_name="omnifree-budget")).data
        if existing:
            budget = existing[0]
            self.echo("OCI budget omnifree-budget already exists")
        else:
            budget = (await self._call(client.create_budget, budget_mod.models.CreateBudgetDetails(
                compartment_id=tenancy, target_type="COMPARTMENT", targets=[tenancy], amount=float(amount_usd),
                reset_period="MONTHLY", display_name="omnifree-budget",
                description="OmniFree: alert on any spend"))).data
        rules = (await self._call(client.list_alert_rules, budget.id)).data
        if not rules:
            await self._call(client.create_alert_rule, budget.id, budget_mod.models.CreateAlertRuleDetails(
                type="ACTUAL", threshold=100.0, threshold_type="PERCENTAGE", display_name="omnifree-actual",
                recipients=email, message="OmniFree: OCI spend reached the budget"))
        return str(budget.id)
