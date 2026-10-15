"""FastAPI application."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .api import admin, gateway
from .auth import require_connection_key
from .config import Settings, get_settings
from .manager import CapacityError, SandboxError
from .policy_store import PolicyError
from .runtime.base import Runtime
from .services import Services, services

log = logging.getLogger("oto")


def create_app(
    settings: Settings | None = None,
    runtime: Runtime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. ``runtime`` and ``transport`` are injection points for tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.api_key:
            settings.api_key = secrets.token_urlsafe(32)
            log.warning("ORCH_API_KEY is not set; generated a temporary key for this run: %s", settings.api_key)
        if runtime is None:
            from .runtime.docker import DockerRuntime

            rt = DockerRuntime(settings.docker_host)
        else:
            rt = runtime
        svc = Services.build(settings, rt, transport)
        app.state.oto = svc
        await svc.db.create_all()
        await svc.policies.ensure_default()
        await svc.manager.reconcile()
        for policy in await svc.policies.list():
            admin._spawn_warmup(svc, policy.id)
        svc.reaper.start()
        log.info(
            "Open Terminal Orchestrator %s ready (access=%s, airgapped=%s)",
            __version__,
            settings.sandbox_access,
            settings.airgapped,
        )
        try:
            yield
        finally:
            await svc.close()

    app = FastAPI(
        title="Open Terminal Orchestrator",
        version=__version__,
        lifespan=lifespan,
        # The gateway serves the sandbox's openapi.json; keep FastAPI's own docs out of the way.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(SandboxError)
    async def _sandbox_error(request: Request, exc: SandboxError):
        headers = {"Retry-After": "15"} if isinstance(exc, CapacityError) else None
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(PolicyError)
    async def _policy_error(request: Request, exc: PolicyError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request, svc: Services = Depends(services)):
        require_connection_key(svc.settings, request)
        lines = [
            "# HELP oto_sandboxes Sandboxes by policy and state.",
            "# TYPE oto_sandboxes gauge",
        ]
        for (policy, state), n in sorted((await svc.manager.counts()).items()):
            lines.append(f'oto_sandboxes{{policy="{policy}",state="{state}"}} {n}')
        lines += [
            "# HELP oto_pinned_sandboxes Sandboxes with open WebSockets or in-flight requests.",
            "# TYPE oto_pinned_sandboxes gauge",
            f"oto_pinned_sandboxes {len(svc.manager.pins)}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    app.include_router(admin.router)
    app.include_router(gateway.router)
    app.include_router(gateway.root_router)  # catch-all; must be last
    return app
