"""Static Open Terminal responses served without starting a sandbox.

Open WebUI fetches ``openapi.json``, ``/api/config`` and ``/system`` per connection with no
chat context and caches them. We capture them once per image from a throwaway probe
container and add the orchestrator's own tools.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
import uuid

import httpx
from sqlalchemy import select

from .config import Settings
from .db import Database, Policy, SpecCache
from .policies import EffectivePolicy
from .policy_store import PolicyNotReady, PolicyStore
from .runtime.base import ContainerSpec, NotFound, Runtime, RuntimeError_, wait_published_port

log = logging.getLogger(__name__)

TOOL_PREFIX = "/_sandbox"

INFO_OPERATION = {
    "get": {
        "operationId": "sandbox_info",
        "summary": "Get terminal sandbox info",
        "description": (
            "Return information about this conversation's terminal sandbox: its state, generation "
            "(incremented on every reset), image, persistence, when it will be stopped for "
            "inactivity and when its files will be purged."
        ),
        "responses": {"200": {"description": "Sandbox information"}},
    }
}

RESET_OPERATION = {
    "post": {
        "operationId": "reset_sandbox",
        "summary": "Reset the terminal sandbox",
        "description": (
            "Permanently delete this conversation's terminal environment, including ALL files in the "
            "home directory and installed packages, and start a fresh one from the base image. "
            "Only use this when the user explicitly asks to wipe or reset the terminal, or the "
            "environment is broken beyond repair. Requires confirm=true."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "confirm": {
                                "type": "boolean",
                                "description": "Must be true to confirm that all data will be deleted.",
                            },
                            "reason": {"type": "string", "description": "Short reason for the reset."},
                        },
                        "required": ["confirm"],
                    }
                }
            },
        },
        "responses": {"200": {"description": "The sandbox was reset"}},
    }
}


def sandbox_prompt_note(policy: EffectivePolicy) -> str:
    lc = policy.lifecycle
    parts = [
        "This terminal is a private sandbox dedicated to this conversation.",
    ]
    if policy.persistent:
        parts.append(f"Files under {lc.home_path} persist across pauses")
        if lc.retention_days > 0:
            parts[-1] += f" and are deleted after {lc.retention_days:g} days without activity."
        else:
            parts[-1] += "."
    else:
        parts.append("Storage is ephemeral: files may be lost when the sandbox is recycled.")
    if policy.idle_timeout_minutes > 0:
        parts.append(
            f"The sandbox is paused after {policy.idle_timeout_minutes} minutes of inactivity and resumes "
            "automatically; running processes do not survive a pause."
        )
    if lc.egress == "none":
        parts.append("The sandbox has no network access.")
    if lc.allow_ai_reset:
        parts.append("If the user asks to wipe or start the environment fresh, use the reset_sandbox tool.")
    return " ".join(parts)


class SpecService:
    def __init__(
        self, settings: Settings, db: Database, runtime: Runtime, policies: PolicyStore, http: httpx.AsyncClient
    ):
        self.settings = settings
        self.db = db
        self.runtime = runtime
        self.policies = policies
        self.http = http
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, image_ref: str) -> SpecCache | None:
        async with self.db.session() as s:
            return await s.get(SpecCache, image_ref)

    async def ensure(self, policy: EffectivePolicy) -> SpecCache:
        if not policy.image_ref:
            raise PolicyNotReady(f"Policy {policy.id!r} image is not resolved")
        cached = await self.get(policy.image_ref)
        if cached:
            return cached
        lock = self._locks.setdefault(policy.image_ref, asyncio.Lock())
        async with lock:
            cached = await self.get(policy.image_ref)
            if cached:
                return cached
            try:
                entry = await asyncio.wait_for(self._probe(policy), timeout=self.settings.probe_timeout_seconds)
            except Exception as e:
                await self._set_policy_status(policy.id, "error", f"Probe failed: {e}")
                raise PolicyNotReady(f"Could not read the terminal image's API: {e}") from e
            async with self.db.session() as s:
                await s.merge(entry)
                await s.commit()
            await self._set_policy_status(policy.id, "ready", None)
            return entry

    async def _set_policy_status(self, policy_id: str, status: str, detail: str | None) -> None:
        async with self.db.session() as s:
            row = await s.get(Policy, policy_id)
            if row:
                row.status = status
                row.status_detail = detail
                await s.commit()

    async def _probe(self, policy: EffectivePolicy) -> SpecCache:
        p = self.settings.name_prefix
        probe_id = uuid.uuid4().hex[:12]
        key = secrets.token_urlsafe(24)
        published = self.settings.sandbox_access == "published"
        network = f"{p}-probe-{probe_id}"
        labels = {f"{p}.managed": "true", f"{p}.role": "probe"}
        container_id = None
        log.info("Probing image %s for policy %s", policy.image_ref, policy.id)
        try:
            await self.runtime.ensure_network(network, internal=not published, labels=labels)
            container_id = await self.runtime.create_container(
                ContainerSpec(
                    name=f"{p}-probe-{probe_id}",
                    image=policy.image_ref,
                    env={**policy.env, "OPEN_TERMINAL_API_KEY": key},
                    labels=labels,
                    network=network,
                    publish_host=self.settings.publish_host if published else None,
                    memory_bytes=policy.memory_bytes,
                    nano_cpus=policy.nano_cpus,
                )
            )
            if not published:
                await self.runtime.connect_network(network, self.settings.self_container)
            await self.runtime.start_container(container_id)
            info = await self.runtime.inspect_container(container_id)
            if published:
                host = "127.0.0.1" if self.settings.publish_host in ("0.0.0.0", "") else self.settings.publish_host
                url = f"http://{host}:{info.published_port or await wait_published_port(self.runtime, container_id)}"
            else:
                url = f"http://{info.networks[network]}:8000"

            for _ in range(600):
                try:
                    if (await self.http.get(f"{url}/health", timeout=2)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError_("probe container never became healthy")

            auth = {"Authorization": f"Bearer {key}", "X-User-Id": "system"}
            openapi = (await self.http.get(f"{url}/openapi.json", headers=auth, timeout=10)).raise_for_status().json()
            api_config = (await self.http.get(f"{url}/api/config", headers=auth, timeout=10)).raise_for_status().json()
            system_prompt = None
            if (api_config.get("features") or {}).get("system"):
                r = await self.http.get(f"{url}/system", headers=auth, timeout=10)
                if r.status_code == 200:
                    system_prompt = (r.json() or {}).get("prompt")
            return SpecCache(
                image_ref=policy.image_ref, openapi=openapi, api_config=api_config, system_prompt=system_prompt
            )
        finally:
            if container_id:
                try:
                    await self.runtime.remove_container(container_id)
                except (NotFound, RuntimeError_):
                    log.warning("Failed to remove probe container %s", container_id)
            if not published:
                await self.runtime.disconnect_network(network, self.settings.self_container)
            try:
                await self.runtime.remove_network(network)
            except RuntimeError_:
                log.warning("Failed to remove probe network %s", network)

    # ── responses ─────────────────────────────────────────────────────────

    def openapi(self, entry: SpecCache, policy: EffectivePolicy) -> dict:
        spec = copy.deepcopy(entry.openapi)
        paths = spec.setdefault("paths", {})
        paths[f"{TOOL_PREFIX}/info"] = INFO_OPERATION
        if policy.lifecycle.allow_ai_reset:
            paths[f"{TOOL_PREFIX}/reset"] = RESET_OPERATION
        spec.pop("servers", None)
        return spec

    def api_config(self, entry: SpecCache) -> dict:
        cfg = copy.deepcopy(entry.api_config)
        cfg.setdefault("features", {})["system"] = True
        return cfg

    def system(self, entry: SpecCache, policy: EffectivePolicy) -> dict:
        note = sandbox_prompt_note(policy)
        prompt = f"{entry.system_prompt.rstrip()}\n\n{note}" if entry.system_prompt else note
        return {"prompt": prompt}

    async def invalidate_unused(self) -> None:
        """Drop cache entries for images no policy references any more."""
        async with self.db.session() as s:
            used = {r for (r,) in await s.execute(select(Policy.image_ref)) if r}
            for entry in (await s.execute(select(SpecCache))).scalars():
                if entry.image_ref not in used:
                    await s.delete(entry)
            await s.commit()
