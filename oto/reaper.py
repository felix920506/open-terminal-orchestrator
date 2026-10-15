"""Background loop: idle stop, crash detection, container GC and retention purge."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from .db import SandboxState, utcnow
from .manager import SandboxManager
from .policy_store import PolicyError
from .runtime.base import NotFound, RuntimeError_

log = logging.getLogger(__name__)

S = SandboxState


class Reaper:
    def __init__(self, manager: SandboxManager, interval: float):
        self.manager = manager
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="oto-reaper")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Reaper tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval)

    async def tick(self) -> dict[str, int]:
        m = self.manager
        await m.flush_activity(force=True)
        now = utcnow()
        stats = {"stopped": 0, "dehydrated": 0, "purged": 0, "crashed": 0}
        policies: dict = {}

        for row in await m.list():
            if row.policy_id not in policies:
                try:
                    policies[row.policy_id] = await m.policies.effective(row.policy_id, resolve_image=False)
                except PolicyError:
                    policies[row.policy_id] = None
            policy = policies[row.policy_id]
            if policy is None:
                continue
            lc = policy.lifecycle
            last = m.last_active(row)
            pinned = m.pins.get(row.id, 0) > 0

            try:
                if row.state == S.RUNNING:
                    # Crash / external stop detection.
                    if row.container_id:
                        try:
                            info = await m.runtime.inspect_container(row.container_id)
                            alive = info.running
                        except NotFound:
                            alive = False
                        if not alive:
                            log.warning("Sandbox %s container is no longer running", row.id)
                            await m.invalidate(row.id)
                            stats["crashed"] += 1
                            continue
                    if pinned or policy.idle_timeout_minutes <= 0:
                        continue
                    idle_deadline = last + timedelta(minutes=policy.idle_timeout_minutes)
                    if now < idle_deadline:
                        continue
                    hard_deadline = idle_deadline + timedelta(minutes=lc.max_background_minutes)
                    if now < hard_deadline and await m.running_processes(row) > 0:
                        continue
                    await m.stop(row, reason="idle")
                    stats["stopped"] += 1

                elif row.state in (S.STOPPED, S.DEHYDRATED, S.FAILED):
                    if lc.retention_days > 0 and now - last > timedelta(days=lc.retention_days):
                        await m.destroy(row, reason="retention")
                        stats["purged"] += 1
                    elif (
                        row.state == S.STOPPED
                        and row.stopped_at
                        and now - row.stopped_at > timedelta(hours=lc.container_gc_hours)
                    ):
                        if row.volume_name:
                            await m.dehydrate(row)
                            stats["dehydrated"] += 1
                        else:
                            # Ephemeral sandbox: removing the container loses everything anyway.
                            await m.destroy(row, reason="container_gc_ephemeral")
                            stats["purged"] += 1
            except (RuntimeError_, PolicyError) as e:
                log.warning("Reaper could not process sandbox %s: %s", row.id, e)

        if any(stats.values()):
            log.info("Reaper: %s", stats)
        return stats
