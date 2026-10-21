"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .api import admin, gateway
from .auth import describe_request, require_connection_key
from .client_filter import ClientAllowlist, ClientFilterMiddleware
from .config import Settings, get_settings
from .manager import CapacityError, SandboxError
from .policy_store import PolicyError
from .runtime.base import Runtime
from .services import Services, services

log = logging.getLogger("oto")

# .env names used by the Compose examples, which map them onto the orchestrator's settings.
# Setting them on the container directly does nothing, so warn about it.
COMPOSE_ENV_NAMES = {
    "STAGE_UPLOADS": "ORCH_STAGE_UPLOADS",
    "SANDBOX_IMAGE": "ORCH_DEFAULT_IMAGE",
    "MAX_RUNNING_SANDBOXES": "ORCH_MAX_RUNNING_GLOBAL",
}


def create_app(
    settings: Settings | None = None,
    runtime: Runtime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. ``runtime`` and ``transport`` are injection points for tests."""
    settings = settings or get_settings()
    clients = ClientAllowlist.from_setting(settings.allowed_clients, "clients")
    admin_clients = ClientAllowlist.from_setting(settings.admin_allowed_clients, "admin clients")
    trusted_proxies = ClientAllowlist.from_setting(settings.trusted_proxies, "trusted proxies")
    allowlists = [a for a in (clients, admin_clients, trusted_proxies) if a]

    async def refresh_allowlists() -> None:
        while True:
            await asyncio.sleep(settings.client_dns_refresh_seconds)
            for allowlist in allowlists:
                await allowlist.refresh()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for allowlist in allowlists:
            await allowlist.refresh()
            log.info("Allowlist (%s): %s", allowlist.name, ", ".join(allowlist.describe()))
        if not clients:
            log.warning("ORCH_ALLOWED_CLIENTS is not set; any host that can reach this port may call it")
        for name, setting in COMPOSE_ENV_NAMES.items():
            if name in os.environ and setting not in os.environ:
                log.warning(
                    "%s is set in the orchestrator's environment but has no effect; the orchestrator reads %s. "
                    "(%s is the name for the .env file next to the Compose examples.)",
                    name,
                    setting,
                    name,
                )
        dns_task = asyncio.create_task(refresh_allowlists()) if any(a.hostnames for a in allowlists) else None
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
            "Open Terminal Orchestrator %s ready (access=%s, airgapped=%s, held uploads=%s)",
            __version__,
            settings.sandbox_access,
            settings.airgapped,
            f"on, in {settings.staging_dir}" if settings.stage_uploads else "off",
        )
        try:
            yield
        finally:
            if dns_task:
                dns_task.cancel()
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

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if exc.status_code == 409:
            log.warning("409 Conflict: %s", describe_request(request, exc.detail))
        return await http_exception_handler(request, exc)

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

    app.state.allowlists = {"clients": clients, "admin_clients": admin_clients}
    app.add_middleware(
        ClientFilterMiddleware, clients=clients, admin_clients=admin_clients, trusted_proxies=trusted_proxies
    )
    app.include_router(admin.router)
    app.include_router(gateway.router)
    app.include_router(gateway.root_router)  # catch-all; must be last
    return app
