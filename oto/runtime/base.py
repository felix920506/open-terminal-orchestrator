"""Container runtime abstraction. Docker is the only driver in v1."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol


class RuntimeError_(Exception):
    """Runtime operation failed."""


class NotFound(RuntimeError_):
    pass


class ImageUnavailable(RuntimeError_):
    pass


@dataclass
class ContainerSpec:
    name: str
    image: str
    env: dict[str, str]
    labels: dict[str, str]
    network: str
    network_aliases: list[str] = field(default_factory=list)
    extra_networks: list[str] = field(default_factory=list)
    volume: str | None = None
    mount_path: str = "/home/user"
    publish_host: str | None = None  # publish container port 8000 on this host IP (random port)
    nano_cpus: int | None = None
    memory_bytes: int | None = None
    pids_limit: int | None = None
    cap_add: list[str] = field(default_factory=list)
    cap_drop: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    runtime: str | None = None
    read_only_rootfs: bool = False


@dataclass
class ContainerInfo:
    id: str
    name: str
    running: bool
    status: str  # created|running|exited|dead|...
    exit_code: int | None
    oom_killed: bool
    image_id: str
    labels: dict[str, str]
    networks: dict[str, str]  # network name → IP
    published_port: int | None


@dataclass
class ManagedObjects:
    containers: list[ContainerInfo]
    volumes: dict[str, dict[str, str]]  # name → labels
    networks: dict[str, dict[str, str]]  # name → labels


class Runtime(Protocol):
    async def close(self) -> None: ...

    async def resolve_image(self, image: str, pull_policy: str) -> str:
        """Return the local image id for ``image``, pulling according to ``pull_policy``."""

    async def ensure_volume(self, name: str, labels: dict[str, str]) -> None: ...
    async def remove_volume(self, name: str) -> None: ...
    async def volume_exists(self, name: str) -> bool: ...

    async def ensure_network(self, name: str, internal: bool, labels: dict[str, str], icc: bool = True) -> None: ...
    async def remove_network(self, name: str) -> None: ...
    async def connect_network(self, network: str, container: str, aliases: list[str] | None = None) -> None: ...
    async def disconnect_network(self, network: str, container: str) -> None: ...

    async def create_container(self, spec: ContainerSpec) -> str: ...
    async def start_container(self, container_id: str) -> None: ...
    async def stop_container(self, container_id: str, timeout: int = 10) -> None: ...
    async def remove_container(self, container_id: str) -> None: ...
    async def inspect_container(self, container_id: str) -> ContainerInfo: ...

    async def list_managed(self, label: str) -> ManagedObjects: ...


async def wait_published_port(runtime: Runtime, container_id: str, timeout: float = 10.0) -> int:
    """Return the host port bound to the sandbox port; Docker may report it shortly after start."""
    deadline = time.monotonic() + timeout
    while True:
        info = await runtime.inspect_container(container_id)
        if info.published_port:
            return info.published_port
        if not info.running or time.monotonic() >= deadline:
            raise RuntimeError_(f"Container {container_id[:12]} has no published port (status={info.status})")
        await asyncio.sleep(0.1)
