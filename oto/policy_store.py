"""Policy persistence and resolution."""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError
from sqlalchemy import select

from .config import Settings
from .crypto import SecretBox
from .db import Database, Policy, StateEntry
from .policies import (
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    EffectivePolicy,
    Lifecycle,
    PolicyData,
    parse_cpu,
    parse_memory,
    validate_policy_data,
)
from .runtime.base import ImageUnavailable, Runtime

log = logging.getLogger(__name__)


class PolicyError(Exception):
    status_code = 400


class PolicyNotFound(PolicyError):
    status_code = 404


class PolicyNotReady(PolicyError):
    status_code = 503


class PolicyStore:
    def __init__(self, settings: Settings, db: Database, runtime: Runtime, secrets: SecretBox):
        self.settings = settings
        self.db = db
        self.runtime = runtime
        self.secrets = secrets
        self._resolve_locks: dict[str, asyncio.Lock] = {}

    # ── serialization ─────────────────────────────────────────────────────

    def public_data(self, row: Policy) -> dict:
        """Policy data as Open WebUI expects it (env values decrypted, so a load/save round-trips)."""
        data = dict(row.data or {})
        if row.env_sealed:
            data["env"] = {k: self.secrets.open(v) for k, v in row.env_sealed.items()}
        return data

    def to_response(self, row: Policy) -> dict:
        return {
            "id": row.id,
            "data": self.public_data(row),
            "status": row.status,
            "status_detail": row.status_detail,
            "image_ref": row.image_ref,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def list(self) -> list[Policy]:
        async with self.db.session() as s:
            return list((await s.execute(select(Policy).order_by(Policy.id))).scalars())

    async def get(self, policy_id: str) -> Policy | None:
        async with self.db.session() as s:
            return await s.get(Policy, policy_id)

    async def put(self, policy_id: str, raw: dict) -> Policy:
        try:
            data = PolicyData.model_validate(raw or {})
            validate_policy_data(data)
        except (ValidationError, ValueError) as e:
            raise PolicyError(str(e)) from e
        if data.image and not self.settings.image_allowed(data.image):
            raise PolicyError(f"Image {data.image!r} is not in ORCH_IMAGE_ALLOWLIST")

        stored = data.model_dump(exclude_none=True, exclude={"env"})
        env_sealed = {k: self.secrets.seal(v) for k, v in data.env.items()}

        async with self.db.session() as s:
            row = await s.get(Policy, policy_id)
            if row is None:
                row = Policy(id=policy_id, data={}, lifecycle={}, env_sealed={})
                s.add(row)
            image_changed = (row.data or {}).get("image") != data.image
            row.data = stored
            row.env_sealed = env_sealed
            if image_changed:
                row.image_ref = None
                row.status = "pending"
                row.status_detail = None
            await s.commit()
        return row

    async def put_lifecycle(self, policy_id: str, raw: dict) -> Policy:
        try:
            lifecycle = Lifecycle.model_validate(raw or {})
        except ValidationError as e:
            raise PolicyError(str(e)) from e
        if self.settings.airgapped and "egress" in (raw or {}) and lifecycle.egress != "none":
            raise PolicyError("Egress other than 'none' is not allowed in airgapped mode")
        async with self.db.session() as s:
            row = await s.get(Policy, policy_id)
            if row is None:
                raise PolicyNotFound(f"Policy {policy_id!r} not found")
            row.lifecycle = raw or {}
            await s.commit()
        return row

    async def delete(self, policy_id: str) -> None:
        async with self.db.session() as s:
            row = await s.get(Policy, policy_id)
            if row is None:
                raise PolicyNotFound(f"Policy {policy_id!r} not found")
            await s.delete(row)
            await s.commit()

    async def ensure_default(self) -> None:
        """Create the default policy, and keep its image in step with ORCH_DEFAULT_IMAGE.

        The image follows the env var across restarts until someone sets a different image
        through the policy API (e.g. in Open WebUI); from then on the API value wins.
        """
        policy_id, image = self.settings.default_policy_id, self.settings.default_image
        if not image or not policy_id:
            return
        row = await self.get(policy_id)
        if row is None:
            log.info("Creating default policy %r with image %s", policy_id, image)
            await self.put(policy_id, {"image": image, "storage": "5Gi"})
            await self._set_state("default_image", {"policy_id": policy_id, "image": image})
            return

        current = (row.data or {}).get("image")
        seeded = await self._get_state("default_image")
        # Before this was tracked (<= 0.1.0) the stored image can only have come from the env var
        # or an admin edit; assume the env var, since changing it is why this code runs.
        last_env_image = seeded.get("image") if seeded.get("policy_id") == policy_id else current
        if current == image:
            pass
        elif current not in (last_env_image, None):
            if image == last_env_image:
                return
            log.warning(
                "Policy %r uses image %s, set through the policy API; ignoring ORCH_DEFAULT_IMAGE=%s. "
                "Change the image in Open WebUI, or set it back to %s to let ORCH_DEFAULT_IMAGE manage it again.",
                policy_id,
                current,
                image,
                last_env_image,
            )
            return
        else:
            log.info("Default policy %r: image %s -> %s (ORCH_DEFAULT_IMAGE)", policy_id, current, image)
            await self.put(policy_id, {**self.public_data(row), "image": image})
        await self._set_state("default_image", {"policy_id": policy_id, "image": image})

    async def _get_state(self, key: str) -> dict:
        async with self.db.session() as s:
            row = await s.get(StateEntry, key)
            return dict(row.value) if row else {}

    async def _set_state(self, key: str, value: dict) -> None:
        async with self.db.session() as s:
            row = await s.get(StateEntry, key)
            if row is None:
                s.add(StateEntry(key=key, value=value))
            else:
                row.value = value
            await s.commit()

    # ── resolution ────────────────────────────────────────────────────────

    async def lookup(self, policy_id: str) -> Policy:
        """Find a policy, falling back to the default policy for unknown ids."""
        row = await self.get(policy_id)
        if row is None and self.settings.default_image and policy_id != self.settings.default_policy_id:
            row = await self.get(self.settings.default_policy_id)
        if row is None:
            raise PolicyNotFound(f"Policy {policy_id!r} not found")
        return row

    async def effective(self, policy_id: str, resolve_image: bool = True) -> EffectivePolicy:
        row = await self.lookup(policy_id)
        if resolve_image and not row.image_ref:
            row = await self.resolve_image(row.id)
        data = PolicyData.model_validate(self.public_data(row))
        lifecycle = Lifecycle.model_validate(row.lifecycle or {})
        if self.settings.airgapped:
            lifecycle.egress = "none"
        image = data.image or self.settings.default_image
        if not image:
            raise PolicyNotReady(f"Policy {row.id!r} has no image")
        return EffectivePolicy(
            id=row.id,
            image=image,
            image_ref=row.image_ref,
            nano_cpus=parse_cpu(data.cpu_limit),
            memory_bytes=parse_memory(data.memory_limit),
            persistent=bool(data.storage),
            storage=data.storage,
            idle_timeout_minutes=(
                data.idle_timeout_minutes if data.idle_timeout_minutes is not None else DEFAULT_IDLE_TIMEOUT_MINUTES
            ),
            env=data.env,
            lifecycle=lifecycle,
        )

    async def resolve_image(self, policy_id: str) -> Policy:
        """Resolve (and if allowed, pull) the policy image; records status on the policy."""
        lock = self._resolve_locks.setdefault(policy_id, asyncio.Lock())
        async with lock:
            row = await self.get(policy_id)
            if row is None:
                raise PolicyNotFound(f"Policy {policy_id!r} not found")
            if row.image_ref:
                return row
            image = (row.data or {}).get("image") or self.settings.default_image
            if not image:
                raise PolicyNotReady(f"Policy {policy_id!r} has no image")
            try:
                image_ref = await self.runtime.resolve_image(image, self.settings.effective_pull_policy)
                status, detail = "ready", None
            except ImageUnavailable as e:
                image_ref, status, detail = None, "unavailable", str(e)
            async with self.db.session() as s:
                row = await s.get(Policy, policy_id)
                row.image_ref = image_ref
                row.status = status
                row.status_detail = detail
                await s.commit()
            if image_ref is None:
                raise PolicyNotReady(detail or "image unavailable")
            return row
