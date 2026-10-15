from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from .conftest import ADMIN_H, IMAGE, chat_headers, put_policy


def write(client, chat, path, content):
    return client.post("/p/dev/files/write", json={"path": path, "content": content}, headers=chat_headers(chat))


def read(client, chat, path):
    return client.get("/p/dev/files/read", params={"path": path}, headers=chat_headers(chat))


def state(client, chat="A"):
    (b,) = client.get("/api/v1/terminals/sandboxes", params={"context_id": f"chat:{chat}"}, headers=ADMIN_H).json()
    return b["state"]


def gc(client):
    r = client.post("/api/v1/terminals/gc", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    return r.json()


def test_idle_stop_and_resume(client, runtime, clock):
    write(client, "A", "/f", "kept")
    assert gc(client)["stopped"] == 0
    clock.advance(minutes=11)
    assert gc(client)["stopped"] == 1
    assert state(client) == "stopped"
    (c,) = runtime.containers.values()
    assert not c.running
    assert (c.spec.network, "oto-orchestrator") not in runtime.connections

    assert read(client, "A", "/f").json()["content"] == "kept"
    assert state(client) == "running"
    assert (c.spec.network, "oto-orchestrator") in runtime.connections


def test_background_process_delays_idle_stop(client, runtime, clock):
    put_policy(
        client, "dev", {"image": IMAGE, "storage": "1Gi", "idle_timeout_minutes": 10}, {"max_background_minutes": 60}
    )
    write(client, "A", "/f", "x")
    (c,) = runtime.containers.values()
    c.processes = [{"id": "p1", "status": "running"}]
    clock.advance(minutes=30)
    assert gc(client)["stopped"] == 0
    # The process probe is scoped to the owner and chat, like Open Terminal expects.
    probe = [r for r in c.requests if r.url.path == "/execute" and r.method == "GET"][-1]
    assert probe.headers["x-user-id"] == "u1"
    assert probe.headers["x-session-id"] == "A"
    clock.advance(minutes=45)  # past idle + max_background
    assert gc(client)["stopped"] == 1


def test_open_connection_pins_sandbox(client, clock):
    write(client, "A", "/f", "x")
    svc = client.app.state.oto
    (row_id,) = [b["id"] for b in client.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json()]
    svc.manager.pins[row_id] += 1
    clock.advance(hours=5)
    assert gc(client)["stopped"] == 0
    del svc.manager.pins[row_id]
    assert gc(client)["stopped"] == 1


def test_dehydrate_then_retention_purge(client, runtime, clock):
    write(client, "A", "/f", "kept")
    clock.advance(minutes=11)
    gc(client)
    clock.advance(hours=73)
    assert gc(client)["dehydrated"] == 1
    assert state(client) == "dehydrated"
    assert runtime.containers == {}
    assert len(runtime.volumes) == 1

    # Resume from the volume alone.
    clock.offset = timedelta()
    assert read(client, "A", "/f").json()["content"] == "kept"
    assert state(client) == "running"

    clock.advance(days=31)
    stats = gc(client)  # running sandbox: idle stop first …
    assert stats["stopped"] == 1
    assert gc(client)["purged"] == 1  # … then purged on retention
    assert runtime.volumes == {}
    assert client.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json() == []


def test_ephemeral_sandbox_destroyed_on_gc(client, runtime, clock):
    put_policy(client, "eph", {"image": IMAGE, "idle_timeout_minutes": 5})
    client.post("/p/eph/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    clock.advance(minutes=6)
    gc(client)
    clock.advance(hours=73)
    assert gc(client)["purged"] == 1
    assert runtime.containers == {}


def test_reconcile_after_restart(make_client, runtime):
    c1 = make_client()
    put_policy(c1, "dev", {"image": IMAGE, "storage": "1Gi"})
    c1.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    c1.post("/p/dev/files/write", json={"path": "/f", "content": "2"}, headers=chat_headers("B"))
    c1.__exit__(None, None, None)

    # Simulate drift while the orchestrator was down.
    a, b = sorted(runtime.containers.values(), key=lambda c: c.spec.labels["oto.context"])
    b.running = False
    orphan_spec = replace(a.spec, name="oto-sbx-orphan", labels={**a.spec.labels, "oto.sandbox": "orphan"})
    orphan_id = asyncio.run(runtime.create_container(orphan_spec))
    runtime.containers[orphan_id].running = True

    c2 = make_client()
    boxes = {x["context_id"]: x["state"] for x in c2.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json()}
    assert boxes == {"chat:A": "running", "chat:B": "stopped"}
    assert runtime.containers[orphan_id].running is False
    assert c2.get("/p/dev/files/read", params={"path": "/f"}, headers=chat_headers("A")).json()["content"] == "1"
    assert c2.get("/p/dev/files/read", params={"path": "/f"}, headers=chat_headers("B")).json()["content"] == "2"


def test_image_upgrade_with_allowlist(make_client, runtime, clock):
    c = make_client(image_allowlist="ghcr.io/open-webui/open-terminal*,registry.local/*")
    put_policy(c, "dev", {"image": IMAGE, "storage": "1Gi", "idle_timeout_minutes": 10})
    c.post("/p/dev/files/write", json={"path": "/f", "content": "kept"}, headers=chat_headers("A"))
    clock.advance(minutes=11)
    gc(c)
    runtime.images["registry.local/ot:v2"] = "sha256:img2"
    put_policy(c, "dev", {"image": "registry.local/ot:v2", "storage": "1Gi", "idle_timeout_minutes": 10})
    r = c.get("/p/dev/files/read", params={"path": "/f"}, headers=chat_headers("A"))
    assert r.json()["content"] == "kept"
    (container,) = runtime.containers.values()
    assert container.spec.image == "sha256:img2"
