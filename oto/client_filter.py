"""Source-address allowlist for callers (normally only the Open WebUI backend).

Entries are IPs, CIDRs or hostnames. Hostnames (e.g. the Open WebUI container name) are
resolved at startup, refreshed periodically, and re-resolved on a miss so a restarted
Open WebUI container with a new IP is picked up quickly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$")
MISS_REFRESH_INTERVAL = 5.0


def parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


class ClientAllowlist:
    def __init__(self, entries: Iterable[str], name: str = "clients"):
        self.name = name
        self.networks: list[IPNetwork] = []
        self.hostnames: list[str] = []
        for raw in entries:
            entry = raw.strip()
            if not entry:
                continue
            try:
                self.networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            except ValueError:
                pass
            if not _HOSTNAME_RE.match(entry):
                raise ValueError(f"Invalid allowlist entry {entry!r}: expected an IP, CIDR or hostname")
            self.hostnames.append(entry.lower())
        self._resolved: dict[str, set[IPAddress]] = {h: set() for h in self.hostnames}
        self._last_resolve = 0.0
        self._resolve_lock = asyncio.Lock()

    @classmethod
    def from_setting(cls, value: str, name: str) -> ClientAllowlist | None:
        entries = [e for e in value.split(",") if e.strip()]
        return cls(entries, name) if entries else None

    def __bool__(self) -> bool:
        return bool(self.networks or self.hostnames)

    def describe(self) -> list[str]:
        out = [str(n) for n in self.networks]
        for h in self.hostnames:
            ips = ", ".join(sorted(str(i) for i in self._resolved.get(h, ()))) or "unresolved"
            out.append(f"{h} ({ips})")
        return out

    def matches(self, ip: IPAddress) -> bool:
        if any(ip in net for net in self.networks):
            return True
        return any(ip in ips for ips in self._resolved.values())

    async def refresh(self) -> None:
        if not self.hostnames:
            return
        async with self._resolve_lock:
            loop = asyncio.get_running_loop()
            for host in self.hostnames:
                try:
                    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                except OSError as e:
                    # Keep the last known addresses: a transient DNS failure must not lock
                    # Open WebUI out, and an unknown host never matches anything.
                    log.warning("Allowlist (%s): cannot resolve %r: %s", self.name, host, e)
                    continue
                ips = {ip for info in infos if (ip := parse_ip(info[4][0])) is not None}
                if ips != self._resolved.get(host):
                    log.info("Allowlist (%s): %s → %s", self.name, host, ", ".join(sorted(map(str, ips))))
                self._resolved[host] = ips
            self._last_resolve = time.monotonic()

    async def allows(self, ip: IPAddress | None) -> bool:
        if ip is None:
            return False
        if self.matches(ip):
            return True
        if self.hostnames and time.monotonic() - self._last_resolve >= MISS_REFRESH_INTERVAL:
            await self.refresh()
            return self.matches(ip)
        return False


def is_admin_path(path: str) -> bool:
    """Sandbox-management routes (CLI, Open WebUI functions), as opposed to routes Open WebUI's
    backend calls for the connection itself (gateway, policies, status, refresh)."""
    if path == "/metrics":
        return True
    return path.startswith("/api/v1/terminals/") and path.rstrip("/") != "/api/v1/terminals/refresh"


EXEMPT_PATHS = {"/healthz"}


class ClientFilterMiddleware:
    """Rejects HTTP and WebSocket requests whose source address is not allowed."""

    def __init__(
        self,
        app: ASGIApp,
        clients: ClientAllowlist | None,
        admin_clients: ClientAllowlist | None,
        trusted_proxies: ClientAllowlist | None,
    ):
        self.app = app
        self.clients = clients
        self.admin_clients = admin_clients
        self.trusted_proxies = trusted_proxies
        self._logged: dict[str, float] = {}

    def client_ip(self, scope: Scope) -> IPAddress | None:
        peer = parse_ip((scope.get("client") or (None,))[0])
        if peer is None or not self.trusted_proxies or not self.trusted_proxies.matches(peer):
            return peer
        forwarded = []
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-for":
                forwarded.extend(v.strip() for v in value.decode("latin-1").split(","))
        # Walk right to left: the first hop that is not a trusted proxy is the client.
        for hop in reversed(forwarded):
            ip = parse_ip(hop)
            if ip is None:
                return None  # malformed chain: fail closed
            if not self.trusted_proxies.matches(ip):
                return ip
        return parse_ip(forwarded[0]) if forwarded else peer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        allowlist = (self.admin_clients or self.clients) if is_admin_path(scope["path"]) else self.clients
        if allowlist is None:
            await self.app(scope, receive, send)
            return
        ip = self.client_ip(scope)
        if await allowlist.allows(ip):
            await self.app(scope, receive, send)
            return

        self._log_denied(str(ip) if ip else repr((scope.get("client") or ("?",))[0]), scope["path"], allowlist.name)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4003, "reason": "Client address not allowed"})
            return
        body = b'{"detail":"Client address not allowed"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _log_denied(self, ip: str, path: str, name: str) -> None:
        now = time.monotonic()
        if now - self._logged.get(ip, 0.0) >= 60:
            self._logged[ip] = now
            if len(self._logged) > 10_000:
                self._logged.clear()
            log.warning("Denied request from %s to %s (not in %s allowlist)", ip, path, name)
