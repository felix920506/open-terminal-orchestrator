from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from oto.client_filter import ClientAllowlist, is_admin_path, parse_ip

from .conftest import ADMIN_H, CONN_H, IMAGE, chat_headers, put_policy

OWUI = ("10.1.2.3", 40000)
STRANGER = ("10.2.0.9", 40000)


def write(c, chat="A"):
    return c.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers(chat))


@pytest.fixture
def owui_and_stranger(make_client):
    """Two clients of the same app instance with different source addresses."""
    owui = make_client(client_addr=OWUI, allowed_clients="10.1.0.0/16")
    put_policy(owui, "dev", {"image": IMAGE, "storage": "1Gi"})
    stranger = type(owui)(owui.app, client=STRANGER)
    stranger.__enter__()
    yield owui, stranger
    stranger.__exit__(None, None, None)


def test_no_allowlist_allows_everyone(make_client):
    c = make_client(client_addr=STRANGER)
    assert c.get("/api/v1/policies", headers=CONN_H).status_code == 200


def test_allowlisted_client_passes_everyone_else_is_refused(owui_and_stranger):
    owui, stranger = owui_and_stranger
    assert write(owui).status_code == 200
    assert write(stranger).status_code == 403
    assert stranger.get("/p/dev/openapi.json").status_code == 403  # even the public spec
    assert stranger.get("/api/v1/policies", headers=CONN_H).status_code == 403
    assert stranger.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).status_code == 403
    assert stranger.get("/metrics", headers=CONN_H).status_code == 403
    assert stranger.get("/healthz").status_code == 200  # container health checks


def test_websocket_refused_before_accept(owui_and_stranger):
    _, stranger = owui_and_stranger
    with pytest.raises(WebSocketDisconnect) as e, stranger.websocket_connect("/p/dev/api/terminals/x"):
        pass
    assert e.value.code == 4003


def test_admin_allowlist_is_separate(make_client):
    owui = make_client(client_addr=OWUI, allowed_clients="10.1.2.3", admin_allowed_clients="127.0.0.1")
    put_policy(owui, "dev", {"image": IMAGE})
    ops = type(owui)(owui.app, client=("127.0.0.1", 1))
    ops.__enter__()
    try:
        # Open WebUI keeps the routes it needs for the connection …
        assert write(owui).status_code == 200
        assert owui.post("/api/v1/terminals/refresh", json={}, headers=CONN_H).status_code == 200
        assert owui.get("/api/v1/status", headers=CONN_H).status_code == 200
        # … but not sandbox administration, which only the ops host may use.
        assert owui.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).status_code == 403
        assert ops.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).status_code == 200
        assert write(ops).status_code == 403
    finally:
        ops.__exit__(None, None, None)


def test_hostname_entries_are_resolved(make_client):
    c = make_client(client_addr=("127.0.0.1", 1), allowed_clients="localhost")
    assert c.get("/api/v1/policies", headers=CONN_H).status_code == 200
    status = c.get("/api/v1/status", headers=CONN_H).json()
    assert status["client_allowlist"]["clients"][0].startswith("localhost (")
    assert status["client_allowlist"]["admin_clients"] is None


def test_non_ip_peer_is_refused(make_client):
    c = make_client(allowed_clients="10.0.0.0/8")  # TestClient's default peer is "testclient"
    assert c.get("/api/v1/policies", headers=CONN_H).status_code == 403


def test_trusted_proxy_forwarded_for(make_client):
    proxy = make_client(client_addr=("10.0.0.1", 1), allowed_clients="10.9.9.9", trusted_proxies="10.0.0.1")
    assert proxy.get("/api/v1/policies", headers=CONN_H).status_code == 403
    h = {**CONN_H, "X-Forwarded-For": "10.9.9.9"}
    assert proxy.get("/api/v1/policies", headers=h).status_code == 200
    # A client cannot smuggle an allowed address in front of its own through the proxy.
    h = {**CONN_H, "X-Forwarded-For": "10.9.9.9, 192.168.7.7"}
    assert proxy.get("/api/v1/policies", headers=h).status_code == 403


def test_untrusted_peer_cannot_spoof_forwarded_for(make_client):
    c = make_client(client_addr=STRANGER, allowed_clients="10.9.9.9")
    assert c.get("/api/v1/policies", headers={**CONN_H, "X-Forwarded-For": "10.9.9.9"}).status_code == 403


def test_invalid_entry_fails_startup():
    with pytest.raises(ValueError):
        ClientAllowlist(["not a host!"])


async def test_ipv6_and_mapped_addresses():
    a = ClientAllowlist(["10.0.0.0/8", "fd00::/8"])
    assert await a.allows(parse_ip("::ffff:10.1.1.1"))
    assert await a.allows(parse_ip("fd12::1"))
    assert not await a.allows(parse_ip("2001:db8::1"))
    assert not await a.allows(None)


def test_admin_path_classification():
    assert is_admin_path("/api/v1/terminals/sandboxes")
    assert is_admin_path("/metrics")
    assert not is_admin_path("/api/v1/terminals/refresh")
    assert not is_admin_path("/api/v1/policies/x")
    assert not is_admin_path("/p/x/api/v1/terminals/sandboxes")
