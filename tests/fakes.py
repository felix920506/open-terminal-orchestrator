"""In-memory runtime and a fake Open Terminal upstream for tests without Docker."""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import dataclass, field

import httpx

from oto.runtime.base import ContainerInfo, ContainerSpec, ImageUnavailable, ManagedObjects, NotFound


@dataclass
class FakeContainer:
    id: str
    spec: ContainerSpec
    ip: str
    running: bool = False
    files: dict[str, str] = field(default_factory=dict)
    processes: list[dict] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)


class FakeRuntime:
    def __init__(self, images: dict[str, str] | None = None):
        self.images = images if images is not None else {"ghcr.io/open-webui/open-terminal:slim": "sha256:img1"}
        self.containers: dict[str, FakeContainer] = {}
        self.volumes: dict[str, dict] = {}  # name → {"labels":…, "files":{}}
        self.networks: dict[str, dict] = {}
        self.connections: set[tuple[str, str]] = set()
        self._ips = itertools.count(2)
        self.calls: list[str] = []

    async def close(self) -> None:
        pass

    async def resolve_image(self, image: str, pull_policy: str) -> str:
        if image not in self.images:
            raise ImageUnavailable(f"{image} not present")
        return self.images[image]

    async def ensure_volume(self, name, labels):
        self.volumes.setdefault(name, {"labels": labels, "files": {}})

    async def remove_volume(self, name):
        self.volumes.pop(name, None)

    async def volume_exists(self, name):
        return name in self.volumes

    async def ensure_network(self, name, internal, labels, icc=True):
        self.networks.setdefault(name, {"internal": internal, "labels": labels, "icc": icc})

    async def remove_network(self, name):
        self.networks.pop(name, None)

    async def connect_network(self, network, container, aliases=None):
        self.connections.add((network, container))

    async def disconnect_network(self, network, container):
        self.connections.discard((network, container))

    async def create_container(self, spec: ContainerSpec) -> str:
        for c in self.containers.values():
            if c.spec.name == spec.name:
                return c.id
        cid = uuid.uuid4().hex
        self.containers[cid] = FakeContainer(id=cid, spec=spec, ip=f"10.9.0.{next(self._ips)}")
        self.calls.append(f"create {spec.name}")
        return cid

    def _get(self, cid_or_name: str) -> FakeContainer:
        for c in self.containers.values():
            if c.id == cid_or_name or c.spec.name == cid_or_name:
                return c
        raise NotFound(cid_or_name)

    async def start_container(self, cid):
        self._get(cid).running = True
        self.calls.append(f"start {cid}")

    async def stop_container(self, cid, timeout=10):
        try:
            self._get(cid).running = False
        except NotFound:
            return
        self.calls.append(f"stop {cid}")

    async def remove_container(self, cid):
        try:
            c = self._get(cid)
        except NotFound:
            return
        del self.containers[c.id]
        self.calls.append(f"remove {c.spec.name}")

    async def inspect_container(self, cid) -> ContainerInfo:
        c = self._get(cid)
        return ContainerInfo(
            id=c.id,
            name=c.spec.name,
            running=c.running,
            status="running" if c.running else "exited",
            exit_code=0,
            oom_killed=False,
            image_id=c.spec.image,
            labels=c.spec.labels,
            networks={c.spec.network: c.ip, **{n: c.ip for n in c.spec.extra_networks}},
            published_port=None,
        )

    async def list_managed(self, label) -> ManagedObjects:
        return ManagedObjects(
            containers=[await self.inspect_container(c.id) for c in self.containers.values()],
            volumes={k: v["labels"] for k, v in self.volumes.items()},
            networks={k: v["labels"] for k, v in self.networks.items()},
        )

    # ── fake Open Terminal ────────────────────────────────────────────────

    def by_ip(self, ip: str) -> FakeContainer | None:
        for c in self.containers.values():
            if c.ip == ip and c.running:
                return c
        return None

    def volume_files(self, c: FakeContainer) -> dict[str, str]:
        if c.spec.volume:
            return self.volumes[c.spec.volume]["files"]
        return c.files

    def transport(self) -> httpx.AsyncBaseTransport:
        return _FakeTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        c = self.by_ip(request.url.host)
        if c is None:
            raise httpx.ConnectError("connection refused", request=request)
        c.requests.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "servers": [{"url": "/"}],
                    "paths": {"/execute": {"post": {"operationId": "run_command"}}},
                },
            )
        if path == "/api/config":
            return httpx.Response(200, json={"features": {"terminal": True, "system": True}})
        expected = f"Bearer {c.spec.env['OPEN_TERMINAL_API_KEY']}"
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"detail": "Invalid API key"})
        if path == "/system":
            return httpx.Response(200, json={"prompt": "You have a Linux box."})
        if path == "/files/write":
            body = json.loads(request.content)
            self.volume_files(c)[body["path"]] = body["content"]
            return httpx.Response(200, json={"ok": True})
        if path == "/files/read":
            p = request.url.params["path"]
            files = self.volume_files(c)
            if p not in files:
                return httpx.Response(404, json={"detail": "File not found"})
            return httpx.Response(200, json={"content": files[p]})
        if path == "/execute" and request.method == "GET":
            return httpx.Response(200, json=c.processes)
        if path == "/echo":
            return httpx.Response(
                200,
                json={
                    "headers": dict(request.headers),
                    "query": str(request.url.query, "ascii"),
                    "body": request.content.decode(),
                },
                headers={"x-upstream": "yes", "content-type": "application/json"},
            )
        return httpx.Response(404, json={"detail": "not found"})


class _FakeTransport(httpx.AsyncBaseTransport):
    """Like httpx.MockTransport, but leaves the response stream unread so raw streaming works."""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        resp = self._handler(request)
        return httpx.Response(resp.status_code, headers=resp.headers, stream=_Stream(resp.content))


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        yield self._data
