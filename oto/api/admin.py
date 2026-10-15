"""Admin API.

Every route lives under a prefix Open WebUI's user-facing terminal proxy refuses to
forward (``api/v1/policies``, ``api/v1/status``, ``api/v1/terminals``), so end users can
never reach it through Open WebUI even though it shares the connection key.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from .. import __version__
from ..auth import require_admin_key, require_connection_key
from ..db import AuditEvent, SandboxState, utcnow
from ..policy_store import PolicyError
from ..services import Services, services

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def connection_auth(request: Request, svc: Services = Depends(services)) -> Services:
    require_connection_key(svc.settings, request)
    return svc


def admin_auth(request: Request, svc: Services = Depends(services)) -> Services:
    require_admin_key(svc.settings, request)
    return svc


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Body must be JSON") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return data


# ── policies (Open WebUI admin UI contract) ────────────────────────────────


@router.get("/policies")
async def list_policies(svc: Services = Depends(connection_auth)):
    return [svc.policies.to_response(p) for p in await svc.policies.list()]


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: str, svc: Services = Depends(connection_auth)):
    row = await svc.policies.get(policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return svc.policies.to_response(row)


@router.put("/policies/{policy_id}")
async def put_policy(policy_id: str, request: Request, svc: Services = Depends(connection_auth)):
    row = await svc.policies.put(policy_id, await _body(request))
    # Resolve the image and capture the spec in the background: pulls can outlast the
    # Open WebUI admin request timeout.
    _spawn_warmup(svc, policy_id)
    return svc.policies.to_response(row)


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: str, svc: Services = Depends(admin_auth)):
    if await svc.manager.list(policy_id=policy_id):
        raise HTTPException(status_code=409, detail="Policy still has sandboxes")
    await svc.policies.delete(policy_id)
    return {"status": "deleted"}


@router.get("/policies/{policy_id}/lifecycle")
async def get_lifecycle(policy_id: str, svc: Services = Depends(connection_auth)):
    row = await svc.policies.get(policy_id)
    return {"id": policy_id, "data": (row.lifecycle if row else {}) or {}}


@router.put("/policies/{policy_id}/lifecycle")
async def put_lifecycle(policy_id: str, request: Request, svc: Services = Depends(connection_auth)):
    row = await svc.policies.put_lifecycle(policy_id, await _body(request))
    return {"id": policy_id, "data": row.lifecycle or {}}


def _spawn_warmup(svc: Services, policy_id: str) -> None:
    async def warm():
        try:
            policy = await svc.policies.effective(policy_id)
            await svc.specs.ensure(policy)
            await svc.specs.invalidate_unused()
        except PolicyError as e:
            log.warning("Policy %s warm-up failed: %s", policy_id, e)

    svc.spawn(warm())


# ── status ─────────────────────────────────────────────────────────────────


@router.get("/status")
async def status(svc: Services = Depends(connection_auth)):
    counts = await svc.manager.counts()
    by_state: dict[str, int] = {}
    for (_, state), n in counts.items():
        by_state[state] = by_state.get(state, 0) + n
    return {
        "version": __version__,
        "airgapped": svc.settings.airgapped,
        "sandbox_access": svc.settings.sandbox_access,
        "sandboxes": by_state,
        "max_running_global": svc.settings.max_running_global,
        "policies": [
            {"id": p.id, "status": p.status, "detail": p.status_detail, "image_ref": p.image_ref}
            for p in await svc.policies.list()
        ],
    }


# ── refresh (Open WebUI "Refresh Terminals") ───────────────────────────────


class RefreshForm(BaseModel):
    user_id: str | None = None
    policy_id: str | None = None
    only_idle: bool = True
    reset: bool = False


@router.post("/terminals/refresh")
async def refresh(form: RefreshForm, svc: Services = Depends(connection_auth)):
    m = svc.manager
    rows = await m.list(policy_id=form.policy_id, owner_user_id=form.user_id)
    refreshed = 0
    now = utcnow()
    for row in rows:
        if form.only_idle:
            if m.pins.get(row.id):
                continue
            if row.state == SandboxState.RUNNING and now - m.last_active(row) < timedelta(minutes=5):
                continue
        if form.reset:
            await m.reset(row, actor="admin", reason="refresh")
        else:
            await m.recreate(row, actor="admin")
        refreshed += 1
    return {"refreshed": refreshed}


# ── sandboxes (ops / integrations) ─────────────────────────────────────────


async def _describe(svc: Services, row) -> dict:
    try:
        policy = await svc.policies.effective(row.policy_id, resolve_image=False)
    except PolicyError:
        policy = None
    return svc.manager.describe(row, policy)


@router.get("/terminals/sandboxes")
async def list_sandboxes(
    policy_id: str | None = None,
    user_id: str | None = None,
    context_id: str | None = None,
    state: str | None = None,
    svc: Services = Depends(admin_auth),
):
    rows = await svc.manager.list(policy_id=policy_id, owner_user_id=user_id, context_id=context_id, state=state)
    return [await _describe(svc, r) for r in rows]


@router.get("/terminals/sandboxes/{sandbox_id}")
async def get_sandbox(sandbox_id: str, svc: Services = Depends(admin_auth)):
    return await _describe(svc, await svc.manager.get(sandbox_id))


@router.post("/terminals/sandboxes/{sandbox_id}/stop")
async def stop_sandbox(sandbox_id: str, svc: Services = Depends(admin_auth)):
    row = await svc.manager.get(sandbox_id)
    await svc.manager.stop(row, reason="admin", actor="admin")
    return await _describe(svc, await svc.manager.get(sandbox_id))


@router.post("/terminals/sandboxes/{sandbox_id}/reset")
async def reset_sandbox(sandbox_id: str, svc: Services = Depends(admin_auth)):
    row = await svc.manager.reset(await svc.manager.get(sandbox_id), actor="admin", reason="admin")
    return await _describe(svc, row)


@router.delete("/terminals/sandboxes/{sandbox_id}")
async def destroy_sandbox(sandbox_id: str, svc: Services = Depends(admin_auth)):
    await svc.manager.destroy(await svc.manager.get(sandbox_id), reason="admin", actor="admin")
    return {"status": "destroyed"}


class ContextResetForm(BaseModel):
    context_id: str
    user_id: str | None = None  # when given, only sandboxes owned by this user are affected
    policy_id: str | None = None
    reason: str = ""


@router.post("/terminals/contexts/reset")
async def reset_context(form: ContextResetForm, svc: Services = Depends(admin_auth)):
    """Used by the Open WebUI "Reset terminal" action button."""
    rows = await svc.manager.list(context_id=form.context_id, policy_id=form.policy_id)
    if form.user_id and any(r.owner_user_id != form.user_id for r in rows):
        raise HTTPException(status_code=403, detail="Terminal belongs to another user")
    out = []
    for row in rows:
        out.append(await _describe(svc, await svc.manager.reset(row, actor="user", reason=form.reason or "user")))
    return {"reset": len(out), "sandboxes": out}


@router.delete("/terminals/contexts")
async def destroy_context(
    context_id: str | None = None,
    user_id: str | None = None,
    svc: Services = Depends(admin_auth),
):
    """Used by the Open WebUI chat-deleted event function.

    ``context_id`` destroys that context's sandboxes; ``user_id`` alone destroys all of the
    user's chat sandboxes (Open WebUI "delete all chats").
    """
    if not context_id and not user_id:
        raise HTTPException(status_code=400, detail="context_id or user_id is required")
    rows = await svc.manager.list(context_id=context_id, owner_user_id=user_id)
    if not context_id:
        rows = [r for r in rows if r.context_id.startswith("chat:")]
    for row in rows:
        await svc.manager.destroy(row, reason="context_deleted", actor="system")
    return {"destroyed": len(rows)}


@router.post("/terminals/gc")
async def run_gc(svc: Services = Depends(admin_auth)):
    return await svc.reaper.tick()


@router.get("/terminals/audit")
async def audit(limit: int = 100, sandbox_id: str | None = None, svc: Services = Depends(admin_auth)):
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(max(limit, 1), 1000))
    if sandbox_id:
        q = q.where(AuditEvent.sandbox_id == sandbox_id)
    async with svc.db.session() as s:
        rows = (await s.execute(q)).scalars()
        return [
            {
                "ts": e.ts.isoformat(),
                "sandbox_id": e.sandbox_id,
                "context_id": e.context_id,
                "actor": e.actor,
                "action": e.action,
                "detail": e.detail,
            }
            for e in rows
        ]
