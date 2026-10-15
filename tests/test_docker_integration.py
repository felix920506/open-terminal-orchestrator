"""End-to-end against a real Docker daemon (published-port mode, orchestrator on the host).

Run with: ``uv run pytest -m docker``. Skipped when Docker or the image is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from oto.config import Settings
from oto.main import create_app

from .conftest import ADMIN, ADMIN_H, CONN_H, KEY, chat_headers

IMAGE = os.environ.get("OTO_TEST_IMAGE", "ghcr.io/open-webui/open-terminal:slim")

pytestmark = pytest.mark.docker


def _docker_ready() -> bool:
    try:
        import aiodocker

        async def check():
            d = aiodocker.Docker()
            try:
                await d.images.inspect(IMAGE)
                return True
            finally:
                await d.close()

        return asyncio.run(check())
    except Exception:
        return False


@pytest.fixture(scope="module")
def prefix():
    if not _docker_ready():
        pytest.skip(f"Docker or image {IMAGE} not available")
    return f"ototest{uuid.uuid4().hex[:6]}"


@pytest.fixture
def client(tmp_path, prefix):
    settings = Settings(
        _env_file=None,
        api_key=KEY,
        admin_api_key=ADMIN,
        secret_key="x",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/oto.db",
        sandbox_access="published",
        name_prefix=prefix,
        reaper_interval_seconds=3600,
        activity_flush_seconds=0,
        pull_policy="never",
    )
    with TestClient(create_app(settings)) as c:
        r = c.put(
            "/api/v1/policies/dev", json={"image": IMAGE, "storage": "1Gi", "memory_limit": "512Mi"}, headers=CONN_H
        )
        assert r.status_code == 200, r.text
        yield c
        for box in c.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json():
            c.delete(f"/api/v1/terminals/sandboxes/{box['id']}", headers=ADMIN_H)


def run(c, chat, command):
    r = c.post("/p/dev/execute", params={"wait": 20}, json={"command": command}, headers=chat_headers(chat))
    assert r.status_code == 200, r.text
    return "".join(o["data"] for o in r.json()["output"]).strip()


def test_end_to_end(client):
    spec = client.get("/p/dev/openapi.json").json()
    assert "/execute" in spec["paths"] and "/_sandbox/reset" in spec["paths"]

    assert run(client, "A", "echo hi > ~/a.txt && cat ~/a.txt") == "hi"
    assert "No such file" in run(client, "B", "cat ~/a.txt 2>&1 || true")

    # Stop, then resume on demand with data intact.
    (box,) = client.get("/api/v1/terminals/sandboxes", params={"context_id": "chat:A"}, headers=ADMIN_H).json()
    client.post(f"/api/v1/terminals/sandboxes/{box['id']}/stop", headers=ADMIN_H)
    assert run(client, "A", "cat ~/a.txt") == "hi"

    # Dehydrate (container removed), resume from the volume.
    client.post("/api/v1/terminals/refresh", json={"only_idle": False, "policy_id": "dev"}, headers=CONN_H)
    assert run(client, "A", "cat ~/a.txt") == "hi"

    # Interactive terminal over the WebSocket proxy.
    r = client.post("/p/dev/api/terminals", json={}, headers=chat_headers("A"))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    ws_headers = {k: v for k, v in chat_headers("A").items() if k != "Authorization"}
    with client.websocket_connect(f"/p/dev/api/terminals/{sid}", headers=ws_headers) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": KEY}))
        ws.send_bytes(b"echo ws-$((6*7))\n")
        out = b""
        for _ in range(200):
            msg = ws.receive()
            out += msg.get("bytes") or (msg.get("text") or "").encode()
            if b"ws-42" in out:
                break
        assert b"ws-42" in out

    # Reset wipes the home volume.
    r = client.post("/p/dev/_sandbox/reset", json={"confirm": True}, headers=chat_headers("A"))
    assert r.status_code == 200, r.text
    assert "No such file" in run(client, "A", "cat ~/a.txt 2>&1 || true")
