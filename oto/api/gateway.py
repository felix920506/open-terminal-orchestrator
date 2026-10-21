"""Open WebUI-facing gateway: ``/p/{policy_id}/…`` (and the root for the default policy).

Requests carrying ``X-Terminal-Context-Id`` are routed to that context's sandbox; the
static spec routes are answered from the spec cache without starting anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
from urllib.parse import quote, unquote

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from websockets.asyncio.client import connect as ws_connect

from ..auth import identify_gateway_caller, require_gateway
from ..manager import Endpoint, OwnershipError, SandboxError
from ..policies import EffectivePolicy
from ..policy_store import PolicyError
from ..services import Services, services, ws_services
from ..spec_cache import TOOL_PREFIX

log = logging.getLogger(__name__)

router = APIRouter()

CONTEXT_HEADER = "x-terminal-context-id"
CONTEXT_RE = re.compile(r"^(chat|automation):[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
STRIP_REQUEST = HOP_BY_HOP | {"host", "authorization", CONTEXT_HEADER, "cookie", "x-user-id", "x-session-id"}
STRIP_RESPONSE = HOP_BY_HOP | {"server", "date"}

STATIC_PATHS = {"openapi.json", "api/config", "system"}
# Reads the Open WebUI UI makes before a chat is saved (and so before it has a context): the
# skills menu, file browser and port list. Answered as "nothing there yet" rather than 409, so
# a new chat doesn't produce a stream of errors (the port list polls every few seconds).
CONTEXTLESS_EMPTY = {
    "skills": [],
    "ports": {"ports": []},
    "files/cwd": {"cwd": None},
    "files/list": {"entries": [], "writable": False},
}
MAX_REPLAYABLE_BODY = 8 * 1024 * 1024
RESERVED_ROOT_PREFIXES = ("api/v1/", "healthz", "metrics")


def _clean_path(path: str) -> str:
    decoded = unquote(path)
    if any(c in decoded for c in "\\\x00\r\n\t"):
        raise HTTPException(status_code=400, detail="Invalid path")
    trailing = decoded.endswith("/")
    norm = posixpath.normpath("/" + decoded).lstrip("/")
    if norm == ".":
        norm = ""
    if trailing and norm and not norm.endswith("/"):
        norm += "/"
    return norm


def _context_id(request_headers, policy: EffectivePolicy, user_id: str) -> str:
    ctx = (request_headers.get(CONTEXT_HEADER) or "").strip()
    if ctx:
        if not CONTEXT_RE.match(ctx):
            raise HTTPException(status_code=400, detail="Invalid X-Terminal-Context-Id")
        return ctx
    if policy.lifecycle.contextless == "per_user" and user_id and user_id != "system":
        return f"user:{user_id}"
    if request_headers.get("x-session-id"):
        # A chat is known but Open WebUI sent no context: the connection is on "Shared".
        _warn_shared_context()
    raise HTTPException(status_code=409, detail=CONTEXT_REQUIRED)


CONTEXT_REQUIRED = (
    "This terminal needs a per-chat context. In Open WebUI, edit the connection under Admin Settings → "
    "Integrations → Open Terminal, expand 'Orchestrator' and set Terminal Contexts → Chat to 'Per chat'. "
    "Temporary chats cannot use it."
)
_warned_shared = False


def _warn_shared_context() -> None:
    global _warned_shared
    if not _warned_shared:
        _warned_shared = True
        log.warning("Open WebUI is calling without a chat context, so every chat request fails. %s", CONTEXT_REQUIRED)


async def _static(svc: Services, policy_id: str, path: str) -> Response:
    policy = await svc.policies.effective(policy_id)
    entry = await svc.specs.ensure(policy)
    if path == "openapi.json":
        return JSONResponse(svc.specs.openapi(entry, policy))
    if path == "api/config":
        return JSONResponse(svc.specs.api_config(entry))
    return JSONResponse(svc.specs.system(entry, policy))


# ── orchestrator tools ─────────────────────────────────────────────────────


async def _tool(svc: Services, request: Request, policy: EffectivePolicy, user_id: str, name: str) -> Response:
    ctx = _context_id(request.headers, policy, user_id)
    row = await svc.manager.find(policy.id, ctx)
    if row is not None and row.owner_user_id != user_id:
        raise OwnershipError("This terminal belongs to another user")

    if name == "info" and request.method == "GET":
        if row is None:
            return JSONResponse({"state": "not_created", "context_id": ctx, "policy_id": policy.id})
        return JSONResponse(svc.manager.describe(row, policy))

    if name == "reset" and request.method == "POST":
        if not policy.lifecycle.allow_ai_reset:
            raise HTTPException(status_code=403, detail="Reset is disabled for this terminal")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="Set confirm=true to reset; this deletes all files")
        reason = str(body.get("reason") or "")[:500]
        if row is None:
            await svc.manager.acquire(policy.id, ctx, user_id, request.headers.get("x-session-id"))
            row = await svc.manager.find(policy.id, ctx)
            return JSONResponse(
                {"status": "fresh", "message": "The terminal was already fresh.", **svc.manager.describe(row, policy)}
            )
        row = await svc.manager.reset(row, actor="ai", reason=reason, start=True)
        return JSONResponse(
            {
                "status": "reset",
                "message": "The terminal was wiped and restarted from the base image. All previous files are gone.",
                **svc.manager.describe(row, policy),
            }
        )

    raise HTTPException(status_code=404, detail="Not found")


# ── HTTP proxy ─────────────────────────────────────────────────────────────


async def _proxy(svc: Services, request: Request, policy_id: str, path: str) -> Response:
    if request.method == "GET" and path in STATIC_PATHS:
        return await _static(svc, policy_id, path)

    user_id = require_gateway(svc.settings, request)
    policy = await svc.policies.effective(policy_id)

    if not user_id or user_id == "system":
        raise HTTPException(status_code=400, detail="X-User-Id is required")

    if path.startswith(TOOL_PREFIX.lstrip("/") + "/"):
        return await _tool(svc, request, policy, user_id, path.split("/", 1)[1])

    if (
        request.method == "GET"
        and path in CONTEXTLESS_EMPTY
        and not request.headers.get(CONTEXT_HEADER)
        and not request.headers.get("x-session-id")  # with a chat id, the connection is misconfigured
        and policy.lifecycle.contextless != "per_user"
    ):
        return JSONResponse(CONTEXTLESS_EMPTY[path])

    ctx = _context_id(request.headers, policy, user_id)
    session_id = request.headers.get("x-session-id") or None

    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}
    headers["x-user-id"] = user_id
    if session_id:
        headers["x-session-id"] = session_id

    content_length = int(request.headers.get("content-length") or 0)
    replayable = request.method in ("GET", "HEAD", "DELETE", "OPTIONS") or 0 < content_length <= MAX_REPLAYABLE_BODY
    body: bytes | None = await request.body() if replayable else None
    if replayable:
        headers.pop("content-length", None)  # httpx sets it from the buffered body

    attempts = 2 if replayable else 1
    for attempt in range(attempts):
        ep = await svc.manager.acquire(policy.id, ctx, user_id, session_id)
        req = svc.proxy_http.build_request(
            request.method,
            f"{ep.url}/{quote(path, safe='/:@!$&()*+,;=-._~')}",
            params=request.query_params.multi_items(),
            headers={**headers, "authorization": f"Bearer {ep.api_key}"},
            content=body if replayable else request.stream(),
        )
        svc.manager.pins[ep.sandbox_id] += 1
        try:
            upstream = await svc.proxy_http.send(req, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            _unpin(svc, ep)
            log.warning("Upstream %s unreachable (%s); attempt %d", ep.sandbox_id, e, attempt + 1)
            await svc.manager.invalidate(ep.sandbox_id)
            if attempt + 1 < attempts:
                continue
            raise HTTPException(status_code=502, detail="Terminal is unreachable") from e
        except BaseException:
            _unpin(svc, ep)
            raise
        return _stream_response(svc, ep, upstream)
    raise HTTPException(status_code=502, detail="Terminal is unreachable")


def _unpin(svc: Services, ep: Endpoint) -> None:
    pins = svc.manager.pins
    pins[ep.sandbox_id] -= 1
    if pins[ep.sandbox_id] <= 0:
        del pins[ep.sandbox_id]
    svc.manager.touch(ep.sandbox_id)


def _stream_response(svc: Services, ep: Endpoint, upstream: httpx.Response) -> StreamingResponse:
    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            _unpin(svc, ep)

    headers = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in STRIP_RESPONSE]
    resp = StreamingResponse(body(), status_code=upstream.status_code)
    resp.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    return resp


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/p/{policy_id}/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def policy_proxy(policy_id: str, path: str, request: Request, svc: Services = Depends(services)):
    return await _proxy(svc, request, policy_id, _clean_path(path))


# ── WebSocket proxy ────────────────────────────────────────────────────────


async def _ws_proxy(svc: Services, ws: WebSocket, policy_id: str, session_id: str) -> None:
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        payload = json.loads(first)
        if not isinstance(payload, dict) or payload.get("type") != "auth":
            raise ValueError
    except (TimeoutError, ValueError, KeyError):
        await ws.close(code=4001, reason="Expected auth message")
        return
    except Exception:
        return

    # Open WebUI before 0.11.4 passes the user only as a query parameter on the WebSocket.
    header_user_id = ws.headers.get("x-user-id") or ws.query_params.get("user_id")
    user_id = identify_gateway_caller(svc.settings, str(payload.get("token") or ""), header_user_id)
    if not user_id or user_id == "system":
        await ws.close(code=4001, reason="Invalid credentials")
        return
    chat_session = ws.headers.get("x-session-id") or str(payload.get("chat_id") or "") or None

    try:
        policy = await svc.policies.effective(policy_id)
        ctx = _context_id(ws.headers, policy, user_id)
        if not chat_session and ctx.startswith("chat:"):
            # Open WebUI before 0.11.4 sends no chat id on the WebSocket; Open Terminal needs it
            # to find the session, which was created over HTTP with X-Session-Id.
            chat_session = ctx.removeprefix("chat:")
        ep = await svc.manager.acquire(policy.id, ctx, user_id, chat_session)
    except HTTPException as e:
        await ws.close(code=4003, reason=str(e.detail)[:120])
        return
    except (SandboxError, PolicyError) as e:
        await ws.close(code=4003, reason=str(e)[:120])
        return

    upstream_url = ep.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    upstream_url += f"/api/terminals/{quote(session_id, safe='')}"
    upstream_headers = {"X-User-Id": user_id}
    if chat_session:
        upstream_headers["X-Session-Id"] = chat_session

    svc.manager.pins[ep.sandbox_id] += 1
    try:
        async with ws_connect(upstream_url, additional_headers=upstream_headers, max_size=None, open_timeout=15) as up:
            await up.send(json.dumps({"type": "auth", "token": ep.api_key}))

            async def client_to_up():
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])
                    svc.manager.touch(ep.sandbox_id)

            async def up_to_client():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            tasks = [asyncio.create_task(client_to_up()), asyncio.create_task(up_to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            close_code, close_reason = up.close_code, up.close_reason
    except (OSError, websockets.WebSocketException) as e:
        log.warning("WebSocket proxy error for %s: %s", ep.sandbox_id, e)
        close_code, close_reason = 1011, "Terminal connection failed"
    finally:
        _unpin(svc, ep)
    try:
        code = close_code if close_code and close_code not in (1005, 1006) else 1000
        await ws.close(code=code, reason=(close_reason or "")[:120])
    except Exception:
        pass


@router.websocket("/p/{policy_id}/api/terminals/{session_id}")
async def policy_ws(ws: WebSocket, policy_id: str, session_id: str):
    await _ws_proxy(ws_services(ws), ws, policy_id, session_id)


@router.websocket("/api/terminals/{session_id}")
async def root_ws(ws: WebSocket, session_id: str):
    svc = ws_services(ws)
    await _ws_proxy(svc, ws, svc.settings.default_policy_id, session_id)


# Registered last by main.py so admin and health routes take precedence.
root_router = APIRouter()


@root_router.api_route("/{path:path}", methods=PROXY_METHODS, include_in_schema=False)
async def root_proxy(path: str, request: Request, svc: Services = Depends(services)):
    clean = _clean_path(path)
    if not clean or clean.startswith(RESERVED_ROOT_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")
    return await _proxy(svc, request, svc.settings.default_policy_id, clean)
