from __future__ import annotations

import json
import time

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from oto.api import gateway

from .conftest import ADMIN_H, CONN_H, IMAGE, KEY, chat_headers, put_policy


def write(client, chat, path, content, user="u1"):
    return client.post("/p/dev/files/write", json={"path": path, "content": content}, headers=chat_headers(chat, user))


def read(client, chat, path, user="u1"):
    return client.get("/p/dev/files/read", params={"path": path}, headers=chat_headers(chat, user))


def sandboxes(client, **params):
    r = client.get("/api/v1/terminals/sandboxes", params=params, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    return r.json()


def test_static_spec_served_without_sandbox(client, runtime):
    spec = client.get("/p/dev/openapi.json").json()
    assert "/_sandbox/info" in spec["paths"]
    assert "/_sandbox/reset" in spec["paths"]
    assert spec["paths"]["/_sandbox/reset"]["post"]["operationId"] == "reset_sandbox"
    assert "servers" not in spec

    assert client.get("/p/dev/api/config").json()["features"]["system"] is True
    prompt = client.get("/p/dev/system").json()["prompt"]
    assert prompt.startswith("You have a Linux box.")
    assert "reset_sandbox" in prompt

    # The probe container and its network were cleaned up; no sandbox was created.
    assert runtime.containers == {}
    assert not any(n.startswith("oto-probe-") for n in runtime.networks)
    assert sandboxes(client) == []


def test_context_required(client):
    h = chat_headers("a")
    del h["X-Terminal-Context-Id"]
    r = client.get("/p/dev/files/read", params={"path": "/f"}, headers=h)
    assert r.status_code == 409
    assert "'Per chat'" in r.json()["detail"]
    r = client.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=h)
    assert r.status_code == 409


def test_shared_chat_context_is_reported(client, caplog, monkeypatch):
    monkeypatch.setattr(gateway, "_warned_shared", False)
    # Connection left on "Shared": Open WebUI sends the chat id but no context header.
    h = chat_headers("a")
    del h["X-Terminal-Context-Id"]
    assert client.get("/p/dev/ports", headers=h).status_code == 409
    assert client.get("/p/dev/files/cwd", headers=h).status_code == 409
    assert "Per chat" in caplog.text
    assert sandboxes(client) == []


def test_unsaved_chat_ui_reads_are_empty(client, runtime):
    # Open WebUI's skills menu, file browser and port poller run before a chat is saved.
    h = {"Authorization": f"Bearer {KEY}", "X-User-Id": "u1"}
    assert client.get("/p/dev/skills", headers=h).json() == []
    assert client.get("/p/dev/ports", headers=h).json() == {"ports": []}
    assert client.get("/p/dev/files/cwd", headers=h).json() == {"cwd": None}
    r = client.get("/p/dev/files/list", params={"directory": "/home/user"}, headers=h)
    assert r.status_code == 200 and r.json() == {"entries": [], "writable": False}
    assert runtime.containers == {}
    assert sandboxes(client) == []


def test_per_user_contextless(client):
    put_policy(client, "dev", {"image": IMAGE, "storage": "1Gi"}, {"contextless": "per_user"})
    h = chat_headers("a")
    del h["X-Terminal-Context-Id"]
    assert client.post("/p/dev/files/write", json={"path": "/x", "content": "1"}, headers=h).status_code == 200
    assert sandboxes(client)[0]["context_id"] == "user:u1"


def test_invalid_context_rejected(client):
    h = chat_headers("a")
    h["X-Terminal-Context-Id"] = "chat:../../etc"
    assert client.get("/p/dev/files/cwd", headers=h).status_code == 400


def test_conversations_are_isolated_and_headers_rewritten(client, runtime):
    assert write(client, "A", "/home/user/a.txt", "hello").status_code == 200
    assert read(client, "A", "/home/user/a.txt").json()["content"] == "hello"
    assert read(client, "B", "/home/user/a.txt").status_code == 404

    boxes = sandboxes(client)
    assert {b["context_id"] for b in boxes} == {"chat:A", "chat:B"}
    assert len(runtime.containers) == 2
    a, b = sorted(runtime.containers.values(), key=lambda c: c.spec.labels["oto.context"])
    assert a.spec.network != b.spec.network
    assert a.spec.volume != b.spec.volume
    assert a.spec.env["OPEN_TERMINAL_API_KEY"] != b.spec.env["OPEN_TERMINAL_API_KEY"]
    assert runtime.networks[a.spec.network]["internal"] is True
    assert a.spec.working_dir == "/home/user"

    r = client.post("/p/dev/echo?x=1&x=2", content=b"payload", headers=chat_headers("A"))
    assert r.status_code == 200
    assert r.headers["x-upstream"] == "yes"
    seen = r.json()
    assert seen["headers"]["authorization"] == f"Bearer {a.spec.env['OPEN_TERMINAL_API_KEY']}"
    assert seen["headers"]["x-user-id"] == "u1"
    assert seen["headers"]["x-session-id"] == "A"
    assert "x-terminal-context-id" not in seen["headers"]
    assert seen["query"] == "x=1&x=2"
    assert seen["body"] == "payload"


def test_owner_binding(client):
    assert write(client, "A", "/f", "1", user="alice").status_code == 200
    r = read(client, "A", "/f", user="mallory")
    assert r.status_code == 403
    r = client.get("/p/dev/_sandbox/info", headers=chat_headers("A", "mallory"))
    assert r.status_code == 403


def test_auth(client, make_client):
    h = chat_headers("A", key="wrong")
    assert client.get("/p/dev/files/cwd", headers=h).status_code == 401
    assert client.get("/api/v1/policies").status_code == 401


def test_session_jwt_auth(make_client):
    c = make_client(webui_secret_key="owui-secret")
    put_policy(c, "dev", {"image": IMAGE, "storage": "1Gi"})
    token = jwt.encode({"id": "u7"}, "owui-secret", algorithm="HS256")
    h = chat_headers("A", user="u7", key=token)
    assert c.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=h).status_code == 200
    # Token for u7 cannot claim to be someone else.
    h = chat_headers("A", user="u8", key=token)
    assert c.get("/p/dev/files/read", params={"path": "/f"}, headers=h).status_code == 401


def test_admin_routes_need_admin_key(client):
    assert client.get("/api/v1/terminals/sandboxes", headers=CONN_H).status_code == 401
    assert client.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).status_code == 200
    # Endpoints Open WebUI's admin UI calls accept the connection key.
    assert client.get("/api/v1/status", headers=CONN_H).status_code == 200
    assert client.post("/api/v1/terminals/refresh", json={}, headers=CONN_H).status_code == 200


def test_root_route_uses_default_policy(make_client):
    c = make_client(default_image=IMAGE)
    h = chat_headers("A")
    assert c.post("/files/write", json={"path": "/f", "content": "1"}, headers=h).status_code == 200
    assert sandboxes(c)[0]["policy_id"] == "default"
    # Reserved prefixes never reach a sandbox.
    assert c.get("/api/v1/whatever", headers=h).status_code in (401, 404)


def test_unknown_policy(client):
    assert client.get("/p/nope/files/cwd", headers=chat_headers("A")).status_code == 404


def test_upstream_crash_is_recovered(client, runtime):
    write(client, "A", "/f", "kept")
    (container,) = runtime.containers.values()
    container.running = False  # died behind our back
    r = read(client, "A", "/f")
    assert r.status_code == 200
    assert r.json()["content"] == "kept"
    assert container.running


def test_reset_tool(client, runtime):
    write(client, "A", "/home/user/f", "data")
    r = client.post("/p/dev/_sandbox/reset", json={}, headers=chat_headers("A"))
    assert r.status_code == 400
    old_volume = next(iter(runtime.containers.values())).spec.volume

    r = client.post("/p/dev/_sandbox/reset", json={"confirm": True, "reason": "user asked"}, headers=chat_headers("A"))
    assert r.status_code == 200, r.text
    assert r.json()["generation"] == 2
    assert r.json()["state"] == "running"
    assert old_volume not in runtime.volumes
    assert read(client, "A", "/home/user/f").status_code == 404

    info = client.get("/p/dev/_sandbox/info", headers=chat_headers("A")).json()
    assert info["generation"] == 2

    audit = client.get("/api/v1/terminals/audit", headers=ADMIN_H).json()
    assert any(e["action"] == "reset" and e["actor"] == "ai" for e in audit)


def test_reset_can_be_disabled(client):
    put_policy(client, "dev", {"image": IMAGE, "storage": "1Gi"}, {"allow_ai_reset": False})
    spec = client.get("/p/dev/openapi.json").json()
    assert "/_sandbox/reset" not in spec["paths"]
    r = client.post("/p/dev/_sandbox/reset", json={"confirm": True}, headers=chat_headers("A"))
    assert r.status_code == 403


def test_info_before_creation(client):
    r = client.get("/p/dev/_sandbox/info", headers=chat_headers("A"))
    assert r.json()["state"] == "not_created"


def test_ephemeral_policy_has_no_volume(client, runtime):
    put_policy(client, "eph", {"image": IMAGE})
    r = client.post("/p/eph/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    assert r.status_code == 200
    (c,) = runtime.containers.values()
    assert c.spec.volume is None
    assert runtime.volumes == {}


def test_capacity_evicts_least_recently_used(client, runtime):
    put_policy(client, "dev", {"image": IMAGE, "storage": "1Gi"}, {"max_running": 1})
    write(client, "A", "/f", "a")
    write(client, "B", "/f", "b")
    states = {b["context_id"]: b["state"] for b in sandboxes(client)}
    assert states == {"chat:A": "stopped", "chat:B": "running"}
    # A resumes on demand (evicting B) with its data intact.
    assert read(client, "A", "/f").json()["content"] == "a"
    states = {b["context_id"]: b["state"] for b in sandboxes(client)}
    assert states == {"chat:A": "running", "chat:B": "stopped"}


def test_context_delete_and_user_reset(client, runtime):
    write(client, "A", "/f", "a")
    r = client.post(
        "/api/v1/terminals/contexts/reset", json={"context_id": "chat:A", "user_id": "someone-else"}, headers=ADMIN_H
    )
    assert r.status_code == 403
    r = client.post("/api/v1/terminals/contexts/reset", json={"context_id": "chat:A", "user_id": "u1"}, headers=ADMIN_H)
    assert r.json()["reset"] == 1

    r = client.delete("/api/v1/terminals/contexts", params={"context_id": "chat:A"}, headers=ADMIN_H)
    assert r.json() == {"destroyed": 1}
    assert sandboxes(client) == []
    assert runtime.containers == {}
    assert runtime.volumes == {}
    assert not any(n.startswith("oto-net-") for n in runtime.networks)


def test_refresh_keeps_volume_unless_reset(client, runtime):
    write(client, "A", "/f", "a")
    r = client.post("/api/v1/terminals/refresh", json={"only_idle": False}, headers=CONN_H)
    assert r.json() == {"refreshed": 1}
    assert sandboxes(client)[0]["state"] == "dehydrated"
    assert read(client, "A", "/f").json()["content"] == "a"

    r = client.post("/api/v1/terminals/refresh", json={"only_idle": False, "reset": True}, headers=CONN_H)
    assert r.json() == {"refreshed": 1}
    assert read(client, "A", "/f").status_code == 404


def test_metrics(client):
    write(client, "A", "/f", "a")
    assert client.get("/metrics").status_code == 401
    text = client.get("/metrics", headers=CONN_H).text
    assert 'oto_sandboxes{policy="dev",state="running"} 1' in text


def test_key_not_leaked_in_policy_or_status(client):
    write(client, "A", "/f", "a")
    for path in ("/api/v1/status", "/api/v1/policies"):
        assert KEY not in client.get(path, headers=CONN_H).text


def test_destroy_all_chats_of_user(client):
    write(client, "A", "/f", "a", user="u1")
    write(client, "B", "/f", "b", user="u1")
    write(client, "C", "/f", "c", user="u2")
    assert client.delete("/api/v1/terminals/contexts", headers=ADMIN_H).status_code == 400
    r = client.delete("/api/v1/terminals/contexts", params={"user_id": "u1"}, headers=ADMIN_H)
    assert r.json() == {"destroyed": 2}
    assert [b["context_id"] for b in sandboxes(client)] == ["chat:C"]


def test_user_id_required(client):
    h = chat_headers("A")
    del h["X-User-Id"]
    assert client.get("/p/dev/files/cwd", headers=h).status_code == 400
    assert client.post("/p/dev/_sandbox/reset", json={"confirm": True}, headers=h).status_code == 400
    h["X-User-Id"] = "system"
    assert client.get("/p/dev/_sandbox/info", headers=h).status_code == 400
    assert sandboxes(client) == []


def test_concurrent_cold_requests_create_one_sandbox(client, runtime):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda i: write(client, "A", f"/f{i}", str(i)).status_code, range(10)))
    assert codes == [200] * 10
    assert len(runtime.containers) == 1
    assert len([c for c in runtime.calls if c.startswith("create oto-sbx-")]) == 1


def test_ws_user_from_query_for_older_open_webui(client):
    # Open WebUI < 0.11.4 sends the user only as ?user_id= on the terminal WebSocket.
    h = {"X-Terminal-Context-Id": "chat:a"}
    with client.websocket_connect("/p/dev/api/terminals/s1?user_id=u7", headers=h) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": KEY}))
        for _ in range(100):  # the fake runtime has no WebSocket upstream; stop once it's routed
            if sandboxes(client):
                break
            time.sleep(0.02)
        ws.close()
    (box,) = sandboxes(client)
    assert box["context_id"] == "chat:a" and box["owner_user_id"] == "u7"


def test_ws_without_user_is_refused(client):
    with client.websocket_connect("/p/dev/api/terminals/s1", headers={"X-Terminal-Context-Id": "chat:a"}) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": KEY}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4001
    assert sandboxes(client) == []
