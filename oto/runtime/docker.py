"""Docker Engine driver (via aiodocker)."""

from __future__ import annotations

import json
import logging

import aiodocker
from aiodocker.exceptions import DockerError

from .base import ContainerInfo, ContainerSpec, ImageUnavailable, ManagedObjects, NotFound, RuntimeError_

log = logging.getLogger(__name__)

SANDBOX_PORT = "8000/tcp"


def _wrap(e: DockerError, what: str) -> RuntimeError_:
    if e.status == 404:
        return NotFound(f"{what}: {e.message}")
    return RuntimeError_(f"{what}: {e.message}")


class DockerRuntime:
    def __init__(self, url: str | None = None):
        self._docker = aiodocker.Docker(url=url) if url else aiodocker.Docker()

    async def close(self) -> None:
        await self._docker.close()

    # ── images ────────────────────────────────────────────────────────────

    async def resolve_image(self, image: str, pull_policy: str) -> str:
        if pull_policy == "always-on-policy-save":
            await self._pull(image)
        try:
            info = await self._docker.images.inspect(image)
            return info["Id"]
        except DockerError as e:
            if e.status != 404:
                raise _wrap(e, f"inspect image {image}") from e
        if pull_policy == "never":
            raise ImageUnavailable(f"Image {image} is not present locally and pulling is disabled")
        await self._pull(image)
        return (await self._docker.images.inspect(image))["Id"]

    async def _pull(self, image: str) -> None:
        log.info("Pulling image %s", image)
        try:
            await self._docker.images.pull(image)
        except DockerError as e:
            raise ImageUnavailable(f"Failed to pull {image}: {e.message}") from e

    # ── volumes ───────────────────────────────────────────────────────────

    async def ensure_volume(self, name: str, labels: dict[str, str]) -> None:
        try:
            await self._docker.volumes.create({"Name": name, "Labels": labels})
        except DockerError as e:
            raise _wrap(e, f"create volume {name}") from e

    async def remove_volume(self, name: str) -> None:
        try:
            await aiodocker.volumes.DockerVolume(self._docker, name).delete()
        except DockerError as e:
            if e.status != 404:
                raise _wrap(e, f"remove volume {name}") from e

    async def volume_exists(self, name: str) -> bool:
        try:
            await aiodocker.volumes.DockerVolume(self._docker, name).show()
            return True
        except DockerError as e:
            if e.status == 404:
                return False
            raise _wrap(e, f"inspect volume {name}") from e

    # ── networks ──────────────────────────────────────────────────────────

    async def ensure_network(self, name: str, internal: bool, labels: dict[str, str], icc: bool = True) -> None:
        try:
            await self._docker.networks.get(name)
            return
        except DockerError as e:
            if e.status != 404:
                raise _wrap(e, f"inspect network {name}") from e
        config = {
            "Name": name,
            "Driver": "bridge",
            "Internal": internal,
            "Labels": labels,
            "CheckDuplicate": True,
            "Options": {"com.docker.network.bridge.enable_icc": "true" if icc else "false"},
        }
        try:
            await self._docker.networks.create(config)
        except DockerError as e:
            if e.status == 409:  # created concurrently
                return
            raise _wrap(e, f"create network {name}") from e

    async def remove_network(self, name: str) -> None:
        try:
            net = await self._docker.networks.get(name)
            await net.delete()
        except DockerError as e:
            if e.status != 404:
                raise _wrap(e, f"remove network {name}") from e

    async def connect_network(self, network: str, container: str, aliases: list[str] | None = None) -> None:
        try:
            net = await self._docker.networks.get(network)
            cfg: dict = {"Container": container}
            if aliases:
                cfg["EndpointConfig"] = {"Aliases": aliases}
            await net.connect(cfg)
        except DockerError as e:
            # 403 "already exists in network" / "endpoint with name ... already exists"
            if e.status in (403, 409) or "already exists" in str(e.message):
                return
            raise _wrap(e, f"connect {container} to {network}") from e

    async def disconnect_network(self, network: str, container: str) -> None:
        try:
            net = await self._docker.networks.get(network)
            await net.disconnect({"Container": container, "Force": True})
        except DockerError as e:
            if e.status in (404, 403, 500):
                return
            raise _wrap(e, f"disconnect {container} from {network}") from e

    # ── containers ────────────────────────────────────────────────────────

    async def create_container(self, spec: ContainerSpec) -> str:
        host_config: dict = {
            "NetworkMode": spec.network,
            "RestartPolicy": {"Name": "no"},
            "CapAdd": spec.cap_add or None,
            "CapDrop": spec.cap_drop or None,
            "SecurityOpt": spec.security_opt or None,
            "ReadonlyRootfs": spec.read_only_rootfs,
            "Init": False,
        }
        if spec.volume:
            host_config["Mounts"] = [{"Type": "volume", "Source": spec.volume, "Target": spec.mount_path}]
        if spec.nano_cpus:
            host_config["NanoCpus"] = spec.nano_cpus
        if spec.memory_bytes:
            host_config["Memory"] = spec.memory_bytes
            host_config["MemorySwap"] = spec.memory_bytes
        if spec.pids_limit:
            host_config["PidsLimit"] = spec.pids_limit
        if spec.runtime:
            host_config["Runtime"] = spec.runtime
        if spec.publish_host:
            host_config["PortBindings"] = {SANDBOX_PORT: [{"HostIp": spec.publish_host, "HostPort": ""}]}
        host_config = {k: v for k, v in host_config.items() if v is not None}

        config = {
            "Image": spec.image,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": spec.labels,
            "ExposedPorts": {SANDBOX_PORT: {}},
            "HostConfig": host_config,
            "NetworkingConfig": {
                "EndpointsConfig": {spec.network: {"Aliases": spec.network_aliases}},
            },
        }
        try:
            container = await self._docker.containers.create(config=config, name=spec.name)
        except DockerError as e:
            if e.status == 409:
                # Same deterministic name already exists (crash between create and DB commit).
                existing = await self._docker.containers.get(spec.name)
                return existing.id
            raise _wrap(e, f"create container {spec.name}") from e
        for extra in spec.extra_networks:
            await self.connect_network(extra, container.id)
        return container.id

    async def start_container(self, container_id: str) -> None:
        try:
            await self._docker.containers.container(container_id).start()
        except DockerError as e:
            if e.status == 304:  # already started
                return
            raise _wrap(e, f"start container {container_id}") from e

    async def stop_container(self, container_id: str, timeout: int = 10) -> None:
        try:
            await self._docker.containers.container(container_id).stop(t=timeout)
        except DockerError as e:
            if e.status in (304, 404):
                return
            raise _wrap(e, f"stop container {container_id}") from e

    async def remove_container(self, container_id: str) -> None:
        try:
            await self._docker.containers.container(container_id).delete(force=True, v=False)
        except DockerError as e:
            if e.status == 404:
                return
            raise _wrap(e, f"remove container {container_id}") from e

    async def inspect_container(self, container_id: str) -> ContainerInfo:
        try:
            c = await self._docker.containers.get(container_id)
        except DockerError as e:
            raise _wrap(e, f"inspect container {container_id}") from e
        return self._info(c._container)

    @staticmethod
    def _info(d: dict) -> ContainerInfo:
        state = d.get("State") or {}
        ns = d.get("NetworkSettings") or {}
        networks = {name: (cfg or {}).get("IPAddress", "") for name, cfg in (ns.get("Networks") or {}).items()}
        port = None
        bindings = (ns.get("Ports") or {}).get(SANDBOX_PORT) or []
        for b in bindings:
            if b.get("HostPort"):
                port = int(b["HostPort"])
                break
        return ContainerInfo(
            id=d["Id"],
            name=(d.get("Name") or "").lstrip("/"),
            running=bool(state.get("Running")),
            status=state.get("Status", "unknown"),
            exit_code=state.get("ExitCode"),
            oom_killed=bool(state.get("OOMKilled")),
            image_id=d.get("Image", ""),
            labels=(d.get("Config") or {}).get("Labels") or {},
            networks=networks,
            published_port=port,
        )

    async def list_managed(self, label: str) -> ManagedObjects:
        filters = {"label": [label]}
        containers = []
        for c in await self._docker.containers.list(all="true", filters=json.dumps(filters)):
            try:
                containers.append(await self.inspect_container(c.id))
            except NotFound:
                continue
        vols = await self._docker.volumes.list(filters=filters)
        volumes = {v["Name"]: v.get("Labels") or {} for v in (vols.get("Volumes") or [])}
        nets = await self._docker.networks.list(filters=filters)
        networks = {n["Name"]: n.get("Labels") or {} for n in nets}
        return ManagedObjects(containers=containers, volumes=volumes, networks=networks)
