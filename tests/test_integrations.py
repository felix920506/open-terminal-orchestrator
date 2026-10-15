"""The Open WebUI functions in integrations/owui call the admin API correctly."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from .conftest import ADMIN, ADMIN_H, chat_headers

ROOT = Path(__file__).resolve().parent.parent / "integrations" / "owui"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def via_app(client, monkeypatch):
    """Route the functions' httpx calls into the in-process app."""

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def _call(self, method, url, **kw):
            path = httpx.URL(url).raw_path.decode()
            r = client.request(
                method,
                path.split("?")[0],
                params=kw.get("params") or httpx.URL(url).params,
                json=kw.get("json"),
                headers=kw.get("headers"),
            )
            return httpx.Response(r.status_code, content=r.content, request=httpx.Request(method, url))

        async def delete(self, url, **kw):
            return await self._call("DELETE", url, **kw)

        async def post(self, url, **kw):
            return await self._call("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return client


async def test_event_function_destroys_chat_sandbox(via_app):
    via_app.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    fn = load("event_chat_cleanup").Event()
    fn.valves.orchestrator_url = "http://oto"
    fn.valves.admin_api_key = ADMIN
    await fn.event({"event": "chat.updated", "subject": {"id": "A"}})
    assert len(via_app.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json()) == 1
    await fn.event({"event": "chat.deleted", "subject": {"id": "A", "type": "chat"}})
    assert via_app.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json() == []


async def test_action_function_resets_only_own_chat(via_app):
    via_app.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    fn = load("action_reset_terminal").Action()
    fn.valves.orchestrator_url = "http://oto"
    fn.valves.admin_api_key = ADMIN
    events = []

    async def emit(e):
        events.append(e)

    async def confirm(_):
        return True

    await fn.action({"chat_id": "A"}, __user__={"id": "intruder"}, __event_emitter__=emit, __event_call__=confirm)
    assert "failed" in events[-1]["data"]["description"]
    await fn.action({"chat_id": "A"}, __user__={"id": "u1"}, __event_emitter__=emit, __event_call__=confirm)
    assert "reset" in events[-1]["data"]["description"].lower()
    (box,) = via_app.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json()
    assert box["generation"] == 2
