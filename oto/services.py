"""Wiring of long-lived services."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field

import httpx
from fastapi import Request, WebSocket

from .config import Settings
from .crypto import SecretBox
from .db import Database
from .manager import SandboxManager
from .policy_store import PolicyStore
from .reaper import Reaper
from .runtime.base import Runtime
from .spec_cache import SpecService


@dataclass
class Services:
    settings: Settings
    db: Database
    runtime: Runtime
    secrets: SecretBox
    http: httpx.AsyncClient  # control-plane calls to sandboxes (health, probes)
    proxy_http: httpx.AsyncClient  # proxied user traffic (long timeouts)
    policies: PolicyStore
    specs: SpecService
    manager: SandboxManager
    reaper: Reaper
    _background: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Run a fire-and-forget task, keeping a reference so it is not garbage collected."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @classmethod
    def build(cls, settings: Settings, runtime: Runtime, transport: httpx.AsyncBaseTransport | None = None) -> Services:
        db = Database(settings.database_url)
        secrets = SecretBox(settings.secret_key)
        http = httpx.AsyncClient(timeout=10.0, transport=transport)
        proxy_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=330.0, write=330.0, pool=30.0),
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=100),
            transport=transport,
        )
        policies = PolicyStore(settings, db, runtime, secrets)
        specs = SpecService(settings, db, runtime, policies, http)
        manager = SandboxManager(settings, db, runtime, policies, secrets, http)
        reaper = Reaper(manager, settings.reaper_interval_seconds)
        return cls(settings, db, runtime, secrets, http, proxy_http, policies, specs, manager, reaper)

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.reaper.stop()
        await self.manager.close()
        await self.http.aclose()
        await self.proxy_http.aclose()
        await self.runtime.close()
        await self.db.close()


def services(request: Request) -> Services:
    return request.app.state.oto


def ws_services(ws: WebSocket) -> Services:
    return ws.app.state.oto
