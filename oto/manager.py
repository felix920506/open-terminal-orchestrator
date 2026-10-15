"""Sandbox lifecycle: create, resume, stop, dehydrate, reset, destroy, reconcile."""

from __future__ import annotations

import asyncio
import logging
import secrets as pysecrets
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, func, select

from .config import Settings
from .crypto import SecretBox
from .db import AuditEvent, Database, Sandbox, SandboxState, utcnow
from .policies import EffectivePolicy
from .policy_store import PolicyStore
from .runtime.base import ContainerInfo, ContainerSpec, NotFound, Runtime, RuntimeError_, wait_published_port

log = logging.getLogger(__name__)

S = SandboxState


class SandboxError(Exception):
    status_code = 500


class OwnershipError(SandboxError):
    status_code = 403


class CapacityError(SandboxError):
    status_code = 503


class StartError(SandboxError):
    status_code = 503


class SandboxNotFound(SandboxError):
    status_code = 404


@dataclass(frozen=True)
class Endpoint:
    sandbox_id: str
    url: str
    api_key: str
    owner_user_id: str
    context_id: str
    session_id: str | None


class SandboxManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        runtime: Runtime,
        policies: PolicyStore,
        secrets: SecretBox,
        http: httpx.AsyncClient,
    ):
        self.settings = settings
        self.db = db
        self.runtime = runtime
        self.policies = policies
        self.secrets = secrets
        self.http = http
        self._locks: dict[str, asyncio.Lock] = {}
        self._endpoints: dict[str, Endpoint] = {}  # sandbox id → endpoint (running sandboxes)
        self._by_key: dict[tuple[str, str], Endpoint] = {}  # (policy id, context id) → endpoint
        self._activity: dict[str, datetime] = {}
        self._last_flush = time.monotonic()
        self.pins: Counter[str] = Counter()  # sandbox id → open WebSockets / in-flight requests
        self.label_managed = f"{settings.name_prefix}.managed"

    # ── naming / labels ───────────────────────────────────────────────────

    def _container_name(self, sid: str) -> str:
        return f"{self.settings.name_prefix}-sbx-{sid}"

    def _network_name(self, sid: str) -> str:
        return f"{self.settings.name_prefix}-net-{sid}"

    def _volume_name(self, sid: str, generation: int) -> str:
        return f"{self.settings.name_prefix}-vol-{sid}-g{generation}"

    def _labels(self, row: Sandbox, role: str = "sandbox") -> dict[str, str]:
        p = self.settings.name_prefix
        return {
            **self.settings.extra_labels,
            f"{p}.managed": "true",
            f"{p}.role": role,
            f"{p}.sandbox": row.id,
            f"{p}.context": row.context_id,
            f"{p}.policy": row.policy_id,
            f"{p}.owner": row.owner_user_id,
            f"{p}.generation": str(row.generation),
        }

    def _lock(self, policy_id: str, context_id: str) -> asyncio.Lock:
        return self._locks.setdefault(f"{policy_id}\x00{context_id}", asyncio.Lock())

    def _row_lock(self, row: Sandbox) -> asyncio.Lock:
        return self._lock(row.policy_id, row.context_id)

    # ── activity ──────────────────────────────────────────────────────────

    def touch(self, sandbox_id: str) -> None:
        self._activity[sandbox_id] = utcnow()

    def last_active(self, row: Sandbox) -> datetime:
        pending = self._activity.get(row.id)
        return max(row.last_active_at, pending) if pending else row.last_active_at

    async def flush_activity(self, force: bool = False) -> None:
        if not self._activity:
            return
        if not force and time.monotonic() - self._last_flush < self.settings.activity_flush_seconds:
            return
        pending, self._activity = self._activity, {}
        self._last_flush = time.monotonic()
        async with self.db.session() as s:
            rows = (await s.execute(select(Sandbox).where(Sandbox.id.in_(list(pending))))).scalars()
            for row in rows:
                if pending[row.id] > row.last_active_at:
                    row.last_active_at = pending[row.id]
            await s.commit()

    async def audit(self, row: Sandbox | None, actor: str, action: str, **detail) -> None:
        async with self.db.session() as s:
            s.add(
                AuditEvent(
                    sandbox_id=row.id if row else None,
                    context_id=row.context_id if row else detail.pop("context_id", None),
                    actor=actor,
                    action=action,
                    detail=detail,
                )
            )
            await s.commit()
        log.info("audit actor=%s action=%s sandbox=%s %s", actor, action, row.id if row else None, detail)

    # ── queries ───────────────────────────────────────────────────────────

    async def get(self, sandbox_id: str) -> Sandbox:
        async with self.db.session() as s:
            row = await s.get(Sandbox, sandbox_id)
        if row is None:
            raise SandboxNotFound(sandbox_id)
        return row

    async def find(self, policy_id: str, context_id: str) -> Sandbox | None:
        async with self.db.session() as s:
            return (
                await s.execute(select(Sandbox).where(Sandbox.policy_id == policy_id, Sandbox.context_id == context_id))
            ).scalar_one_or_none()

    async def list(
        self,
        *,
        policy_id: str | None = None,
        owner_user_id: str | None = None,
        context_id: str | None = None,
        state: str | None = None,
    ) -> list[Sandbox]:
        q = select(Sandbox)
        if policy_id:
            q = q.where(Sandbox.policy_id == policy_id)
        if owner_user_id:
            q = q.where(Sandbox.owner_user_id == owner_user_id)
        if context_id:
            q = q.where(Sandbox.context_id == context_id)
        if state:
            q = q.where(Sandbox.state == state)
        async with self.db.session() as s:
            return list((await s.execute(q.order_by(Sandbox.created_at))).scalars())

    async def counts(self) -> dict[tuple[str, str], int]:
        async with self.db.session() as s:
            rows = await s.execute(
                select(Sandbox.policy_id, Sandbox.state, func.count()).group_by(Sandbox.policy_id, Sandbox.state)
            )
            return {(p, st): n for p, st, n in rows}

    async def _save(self, row: Sandbox) -> None:
        async with self.db.session() as s:
            await s.merge(row)
            await s.commit()

    def describe(self, row: Sandbox, policy: EffectivePolicy | None = None) -> dict:
        last = self.last_active(row)
        out = {
            "id": row.id,
            "context_id": row.context_id,
            "policy_id": row.policy_id,
            "owner_user_id": row.owner_user_id,
            "state": row.state,
            "generation": row.generation,
            "persistent": bool(row.volume_name),
            "image": row.image_ref,
            "created_at": row.created_at.isoformat(),
            "last_active_at": last.isoformat(),
            "last_started_at": row.last_started_at.isoformat() if row.last_started_at else None,
            "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
            "error": row.error,
            "pinned": self.pins.get(row.id, 0),
        }
        if policy:
            lc = policy.lifecycle
            if row.state == S.RUNNING and policy.idle_timeout_minutes > 0:
                out["idle_stop_at"] = (last + timedelta(minutes=policy.idle_timeout_minutes)).isoformat()
            if lc.retention_days > 0:
                out["purge_after"] = (last + timedelta(days=lc.retention_days)).isoformat()
        return out

    # ── acquire (request path) ────────────────────────────────────────────

    async def acquire(self, policy_id: str, context_id: str, user_id: str, session_id: str | None) -> Endpoint:
        """Return a running endpoint for (policy, context), creating or resuming as needed."""
        policy = await self.policies.effective(policy_id)
        policy_id = policy.id  # may have fallen back to the default policy

        cached = self._fast_path(policy_id, context_id, user_id, session_id)
        if cached:
            self.touch(cached.sandbox_id)
            return cached

        async with self._lock(policy_id, context_id):
            row = await self.find(policy_id, context_id)
            if row is None:
                row = await self._create_row(policy, context_id, user_id, session_id)
                await self.audit(row, "user", "create", user_id=user_id)
            elif row.owner_user_id != user_id:
                raise OwnershipError("This terminal belongs to another user")
            if session_id and row.session_id != session_id:
                row.session_id = session_id
                await self._save(row)
            endpoint = await self._ensure_running(row, policy)
        self.touch(row.id)
        return endpoint

    def _fast_path(self, policy_id: str, context_id: str, user_id: str, session_id: str | None) -> Endpoint | None:
        ep = self._by_key.get((policy_id, context_id))
        if ep is None:
            return None
        if ep.owner_user_id != user_id:
            raise OwnershipError("This terminal belongs to another user")
        if session_id and ep.session_id != session_id:
            return None  # slow path records the new session id
        return ep

    def _remember(self, row: Sandbox, url: str) -> Endpoint:
        ep = Endpoint(
            sandbox_id=row.id,
            url=url,
            api_key=self.secrets.open(row.api_key_sealed),
            owner_user_id=row.owner_user_id,
            context_id=row.context_id,
            session_id=row.session_id,
        )
        self._endpoints[row.id] = ep
        self._by_key[(row.policy_id, row.context_id)] = ep
        return ep

    def _forget(self, sandbox_id: str) -> None:
        ep = self._endpoints.pop(sandbox_id, None)
        if ep is not None:
            self._by_key = {k: v for k, v in self._by_key.items() if v.sandbox_id != sandbox_id}

    async def invalidate(self, sandbox_id: str) -> None:
        """Called when the upstream is unreachable; the next acquire re-inspects the container."""
        self._forget(sandbox_id)
        async with self.db.session() as s:
            row = await s.get(Sandbox, sandbox_id)
            if row and row.state == S.RUNNING:
                row.state = S.STOPPED
                row.stopped_at = utcnow()
                await s.commit()

    async def _create_row(
        self, policy: EffectivePolicy, context_id: str, user_id: str, session_id: str | None
    ) -> Sandbox:
        sid = str(uuid.uuid4())
        row = Sandbox(
            id=sid,
            context_id=context_id,
            policy_id=policy.id,
            owner_user_id=user_id,
            session_id=session_id,
            generation=1,
            state=S.CREATING,
            volume_name=self._volume_name(sid, 1) if policy.persistent else None,
            network_name=self._network_name(sid),
            api_key_sealed=self.secrets.seal(pysecrets.token_urlsafe(32)),
            image_ref=policy.image_ref,
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        async with self.db.session() as s:
            s.add(row)
            await s.commit()
        return row

    async def _ensure_running(self, row: Sandbox, policy: EffectivePolicy) -> Endpoint:
        if row.state == S.RUNNING and row.id in self._endpoints:
            return self._endpoints[row.id]

        info: ContainerInfo | None = None
        if row.container_id:
            try:
                info = await self.runtime.inspect_container(row.container_id)
            except NotFound:
                row.container_id = None

        if info and info.running:
            url = await self._upstream_url(row, info)
            if await self._healthy(url, timeout=5):
                return await self._mark_running(row, url, started=False)

        await self._make_room(policy, exclude=row.id)

        try:
            if (
                info
                and not info.running
                and policy.lifecycle.upgrade == "on_next_start"
                and policy.image_ref
                and info.image_id != policy.image_ref
            ):
                log.info("Upgrading sandbox %s to %s", row.id, policy.image_ref)
                await self.runtime.remove_container(info.id)
                info, row.container_id = None, None

            if info is None:
                row.state = S.CREATING
                await self._save(row)
                row.container_id = await self._create_container(row, policy)
                row.image_ref = policy.image_ref
                await self._save(row)

            await self._attach_self(row)
            await self.runtime.start_container(row.container_id)
            info = await self.runtime.inspect_container(row.container_id)
            url = await self._upstream_url(row, info)
            if not await self._healthy(
                url, timeout=policy.lifecycle.start_timeout_seconds, container_id=row.container_id
            ):
                raise StartError(f"Sandbox did not become healthy within {policy.lifecycle.start_timeout_seconds}s")
        except (RuntimeError_, StartError, httpx.HTTPError) as e:
            log.exception("Failed to start sandbox %s", row.id)
            row.state = S.FAILED
            row.error = str(e)
            await self._save(row)
            raise StartError(f"Failed to start terminal: {e}") from e

        return await self._mark_running(row, url, started=True)

    async def _mark_running(self, row: Sandbox, url: str, started: bool) -> Endpoint:
        row.state = S.RUNNING
        row.error = None
        row.upstream_url = url
        row.stopped_at = None
        if started:
            row.last_started_at = utcnow()
        await self._save(row)
        return self._remember(row, url)

    async def _create_container(self, row: Sandbox, policy: EffectivePolicy) -> str:
        lc = policy.lifecycle
        published = self.settings.sandbox_access == "published"
        labels = self._labels(row)
        # Per-sandbox network: internal (no route out) unless published mode needs host port mapping.
        await self.runtime.ensure_network(row.network_name, internal=not published, labels=labels)
        extra_networks: list[str] = []
        env = dict(policy.env)
        env["OPEN_TERMINAL_API_KEY"] = self.secrets.open(row.api_key_sealed)
        cap_add = list(lc.security.cap_add)
        if lc.egress in ("open", "allowlist") and not published:
            await self.runtime.ensure_network(
                self.settings.egress_network,
                internal=False,
                labels={f"{self.settings.name_prefix}.managed": "true", f"{self.settings.name_prefix}.role": "egress"},
                icc=False,
            )
            extra_networks.append(self.settings.egress_network)
        if lc.egress == "allowlist" or (lc.egress == "none" and published):
            # Open Terminal's entrypoint enforces this with dnsmasq + iptables (needs NET_ADMIN).
            env["OPEN_TERMINAL_ALLOWED_DOMAINS"] = ",".join(lc.allowed_domains) if lc.egress == "allowlist" else ""
            cap_add.append("NET_ADMIN")

        if row.volume_name:
            await self.runtime.ensure_volume(row.volume_name, labels)

        spec = ContainerSpec(
            name=self._container_name(row.id),
            image=policy.image_ref or policy.image,
            env=env,
            labels=labels,
            network=row.network_name,
            network_aliases=["sandbox"],
            extra_networks=extra_networks,
            volume=row.volume_name,
            mount_path=lc.home_path,
            working_dir=lc.home_path if lc.working_dir is None else (lc.working_dir or None),
            publish_host=self.settings.publish_host if published else None,
            nano_cpus=policy.nano_cpus,
            memory_bytes=policy.memory_bytes,
            pids_limit=lc.security.pids_limit,
            cap_add=cap_add,
            cap_drop=[c for c in lc.security.cap_drop if c not in cap_add],
            security_opt=["no-new-privileges:true"] if lc.security.no_new_privileges else [],
            runtime=lc.security.runtime,
            read_only_rootfs=lc.security.read_only_rootfs,
        )
        return await self.runtime.create_container(spec)

    async def _attach_self(self, row: Sandbox) -> None:
        if self.settings.sandbox_access == "network" and row.network_name:
            await self.runtime.connect_network(row.network_name, self.settings.self_container)

    async def _detach_self(self, row: Sandbox) -> None:
        if self.settings.sandbox_access == "network" and row.network_name:
            await self.runtime.disconnect_network(row.network_name, self.settings.self_container)

    async def _upstream_url(self, row: Sandbox, info: ContainerInfo) -> str:
        if self.settings.sandbox_access == "published":
            port = info.published_port or await wait_published_port(self.runtime, info.id)
            host = self.settings.publish_host
            host = "127.0.0.1" if host in ("0.0.0.0", "") else host
            return f"http://{host}:{port}"
        ip = info.networks.get(row.network_name or "")
        if not ip:
            raise StartError("Sandbox has no address on its network")
        return f"http://{ip}:8000"

    async def _healthy(self, url: str, timeout: float, container_id: str | None = None) -> bool:
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            try:
                r = await self.http.get(f"{url}/health", timeout=2.0)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                return False
            if container_id:
                info = await self.runtime.inspect_container(container_id)
                if not info.running:
                    raise StartError(
                        f"Container exited during startup (status={info.status}, exit={info.exit_code}"
                        f"{', OOM killed' if info.oom_killed else ''})"
                    )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)

    # ── capacity ──────────────────────────────────────────────────────────

    async def _make_room(self, policy: EffectivePolicy, exclude: str) -> None:
        for _ in range(100):
            running = await self.list(state=S.RUNNING)
            running = [r for r in running if r.id != exclude]
            same_policy = [r for r in running if r.policy_id == policy.id]
            if len(running) < self.settings.max_running_global and len(same_policy) < policy.lifecycle.max_running:
                return
            pool = same_policy if len(same_policy) >= policy.lifecycle.max_running else running
            candidates = sorted((r for r in pool if not self.pins.get(r.id)), key=self.last_active)
            for victim in candidates:
                lock = self._row_lock(victim)
                if lock.locked():
                    continue  # busy (or held by a concurrent acquire) — never wait, avoids lock cycles
                async with lock:
                    await self._stop_locked(victim, reason="capacity")
                break
            else:
                raise CapacityError("All terminal slots are in use; try again shortly")
        raise CapacityError("Could not free a terminal slot")

    # ── transitions (reaper / admin / tools) ──────────────────────────────

    async def stop(self, row: Sandbox, reason: str, actor: str = "system") -> None:
        async with self._row_lock(row):
            row = await self.get(row.id)
            await self._stop_locked(row, reason, actor)

    async def _stop_locked(self, row: Sandbox, reason: str, actor: str = "system") -> None:
        self._forget(row.id)
        await self.flush_activity(force=True)
        row = await self.get(row.id)
        if row.container_id:
            row.state = S.STOPPING
            await self._save(row)
            try:
                await self.runtime.stop_container(row.container_id, timeout=10)
            except NotFound:
                row.container_id = None
        await self._detach_self(row)
        row.state = S.STOPPED if row.container_id else S.DEHYDRATED
        row.stopped_at = utcnow()
        await self._save(row)
        await self.audit(row, actor, "stop", reason=reason)

    async def dehydrate(self, row: Sandbox, reason: str = "container_gc", actor: str = "system") -> None:
        """Remove the container (and its private network) but keep the home volume."""
        async with self._row_lock(row):
            row = await self.get(row.id)
            await self._remove_container_locked(row)
            row.state = S.DEHYDRATED
            await self._save(row)
            await self.audit(row, actor, "dehydrate", reason=reason)

    async def _remove_container_locked(self, row: Sandbox) -> None:
        self._forget(row.id)
        if row.container_id:
            await self.runtime.remove_container(row.container_id)
            row.container_id = None
        else:
            # Deterministic name: clean up a container created before a crash.
            await self.runtime.remove_container(self._container_name(row.id))
        await self._detach_self(row)
        if row.network_name:
            await self.runtime.remove_network(row.network_name)

    async def destroy(self, row: Sandbox, reason: str, actor: str = "system") -> None:
        """Remove container, network and volume, and forget the sandbox."""
        async with self._row_lock(row):
            try:
                row = await self.get(row.id)
            except SandboxNotFound:
                return
            await self._remove_container_locked(row)
            if row.volume_name:
                await self.runtime.remove_volume(row.volume_name)
            async with self.db.session() as s:
                await s.execute(delete(Sandbox).where(Sandbox.id == row.id))
                await s.commit()
            self._activity.pop(row.id, None)
            await self.audit(row, actor, "destroy", reason=reason)

    async def reset(self, row: Sandbox, actor: str, reason: str = "", start: bool = False) -> Sandbox:
        """Wipe the sandbox: new container and a fresh, empty home volume."""
        async with self._row_lock(row):
            row = await self.get(row.id)
            old_volume = row.volume_name
            await self._remove_container_locked(row)
            if old_volume:
                await self.runtime.remove_volume(old_volume)
            policy = await self.policies.effective(row.policy_id)
            row.generation += 1
            row.volume_name = self._volume_name(row.id, row.generation) if policy.persistent else None
            row.api_key_sealed = self.secrets.seal(pysecrets.token_urlsafe(32))
            row.state = S.DEHYDRATED
            row.error = None
            await self._save(row)
            await self.audit(row, actor, "reset", reason=reason, generation=row.generation)
            if start:
                await self._make_room(policy, exclude=row.id)
                await self._ensure_running(row, policy)
        self.touch(row.id)
        return await self.get(row.id)

    async def recreate(self, row: Sandbox, actor: str) -> None:
        """Drop the container but keep the volume; next request starts from the current image."""
        await self.dehydrate(row, reason="refresh", actor=actor)

    # ── background process probe ──────────────────────────────────────────

    async def running_processes(self, row: Sandbox) -> int:
        ep = self._endpoints.get(row.id)
        if ep is None:
            return 0
        headers = {"Authorization": f"Bearer {ep.api_key}", "X-User-Id": row.owner_user_id}
        if row.session_id:
            headers["X-Session-Id"] = row.session_id
        try:
            r = await self.http.get(f"{ep.url}/execute", headers=headers, timeout=5.0)
            if r.status_code != 200:
                return 0
            return sum(1 for p in r.json() if p.get("status") == "running")
        except (httpx.HTTPError, ValueError):
            return 0

    # ── reconcile ─────────────────────────────────────────────────────────

    async def reconcile(self) -> None:
        """Make the database agree with Docker after a restart or crash."""
        p = self.settings.name_prefix
        managed = await self.runtime.list_managed(f"{p}.managed=true")
        by_id = {c.id: c for c in managed.containers}
        by_sandbox = {
            c.labels.get(f"{p}.sandbox"): c for c in managed.containers if c.labels.get(f"{p}.role") == "sandbox"
        }

        for c in managed.containers:
            if c.labels.get(f"{p}.role") == "probe":
                log.info("Removing leftover probe container %s", c.name)
                await self.runtime.remove_container(c.id)

        rows = await self.list()
        known = set()
        for row in rows:
            known.add(row.id)
            info = by_id.get(row.container_id or "") or by_sandbox.get(row.id)
            if info is None:
                row.container_id = None
                row.state = S.DEHYDRATED
                if row.volume_name and not await self.runtime.volume_exists(row.volume_name):
                    log.warning("Sandbox %s lost its volume %s; it will start empty", row.id, row.volume_name)
            elif info.running:
                row.container_id = info.id
                try:
                    await self._attach_self(row)
                    info = await self.runtime.inspect_container(info.id)
                    url = await self._upstream_url(row, info)
                    self._remember(row, url)
                    row.state = S.RUNNING
                    row.upstream_url = url
                except (RuntimeError_, StartError) as e:
                    log.warning("Could not re-attach to running sandbox %s: %s", row.id, e)
                    await self.runtime.stop_container(info.id)
                    row.state = S.STOPPED
            else:
                row.container_id = info.id
                row.state = S.STOPPED
                row.stopped_at = row.stopped_at or utcnow()
            await self._save(row)

        for sid, info in by_sandbox.items():
            if sid not in known and info.running:
                log.warning("Stopping orphaned sandbox container %s (no database record)", info.name)
                await self.runtime.stop_container(info.id)
        log.info("Reconciled %d sandboxes", len(rows))

    async def close(self) -> None:
        await self.flush_activity(force=True)
