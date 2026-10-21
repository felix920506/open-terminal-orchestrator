"""Request authentication."""

from __future__ import annotations

import hmac
import logging

import jwt
from fastapi import HTTPException, Request

from .config import Settings

log = logging.getLogger(__name__)


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def identify_gateway_caller(settings: Settings, token: str, header_user_id: str | None) -> str | None:
    """Authenticate a gateway call and return the effective user id.

    * Connection key (Open WebUI ``auth_type: bearer``): Open WebUI is trusted, so
      ``X-User-Id`` is taken as-is.
    * Open WebUI user JWT (``auth_type: session``, needs ORCH_WEBUI_SECRET_KEY): the user id
      comes from the verified token and must match ``X-User-Id`` when that is present.
    """
    if _eq(token, settings.api_key):
        return header_user_id or ""
    if settings.webui_secret_key and token:
        try:
            claims = jwt.decode(token, settings.webui_secret_key, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        uid = str(claims.get("id") or "")
        if not uid or (header_user_id and header_user_id != uid):
            return None
        return uid
    return None


def require_gateway(settings: Settings, request: Request) -> str:
    uid = identify_gateway_caller(settings, bearer_token(request), request.headers.get("x-user-id"))
    if uid is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return uid


def require_connection_key(settings: Settings, request: Request) -> None:
    """Endpoints Open WebUI's admin UI calls with the connection key (policies, refresh, status)."""
    token = bearer_token(request)
    if _eq(token, settings.api_key) or _eq(token, settings.admin_api_key):
        return
    raise HTTPException(status_code=401, detail="Invalid credentials")


def require_admin_key(settings: Settings, request: Request) -> None:
    """Sandbox management endpoints. Uses the admin key when one is configured."""
    token = bearer_token(request)
    expected = settings.admin_api_key or settings.api_key
    if _eq(token, expected):
        return
    raise HTTPException(status_code=401, detail="Invalid credentials")


SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key"}


def describe_request(request: Request, detail: object = None) -> str:
    """One-line request description for troubleshooting logs; credentials are never included."""
    settings = request.app.state.oto.settings if hasattr(request.app.state, "oto") else None
    headers = {}
    for name, value in request.headers.items():
        if name.lower() == "authorization":
            token = bearer_token(request)
            kind = "missing or not bearer"
            if token and settings:
                if _eq(token, settings.api_key):
                    kind = "connection key"
                elif _eq(token, settings.admin_api_key):
                    kind = "admin key"
                else:
                    kind = "unrecognised"
            headers[name] = f"<redacted: {kind}>"
        elif name.lower() in SECRET_HEADERS:
            headers[name] = "<redacted>"
        else:
            headers[name] = value
    client = request.client.host if request.client else "?"
    query = f"?{request.url.query}" if request.url.query else ""
    parts = [f"{request.method} {request.url.path}{query} from {client}", f"detail={detail!r}", f"headers={headers}"]
    if settings:
        parts.append(f"held_uploads={'on' if settings.stage_uploads else 'off (ORCH_STAGE_UPLOADS)'}")
    return "; ".join(parts)
