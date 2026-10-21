"""The "Terminal uploads" Open WebUI filter, with Open WebUI's chat store faked."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

FILTER = Path(__file__).parent.parent / "integrations" / "owui" / "filter_terminal_uploads.py"


@pytest.fixture
def filter_mod(monkeypatch):
    chats: dict[tuple[str, str], dict] = {}

    class Chats:
        @staticmethod
        async def get_chat_by_id_and_user_id(chat_id, user_id):
            data = chats.get((chat_id, user_id))
            return types.SimpleNamespace(chat=data) if data is not None else None

    for name in ("open_webui", "open_webui.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", types.SimpleNamespace(Chats=Chats))
    spec = importlib.util.spec_from_file_location("filter_terminal_uploads", FILTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    posts = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json, headers):
            posts.append({"url": url, "json": json, "headers": headers})
            return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"delivered": json["paths"]})

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    return mod, chats, posts


def run_inlet(mod, chat_id, user="u1", key="k"):
    f = mod.Filter()
    f.valves.admin_api_key = key
    body = {"messages": []}
    out = asyncio.run(f.inlet(body, __user__={"id": user}, __metadata__={"chat_id": chat_id}))
    assert out is body
    return f


def test_claims_filesystem_attachments_of_the_chat(filter_mod):
    mod, chats, posts = filter_mod
    chats[("c1", "u1")] = {
        "history": {
            "messages": {
                "m1": {
                    "role": "user",
                    "files": [
                        {"type": "filesystem", "id": "/home/user/a.txt", "path": "/home/user/a.txt"},
                        {"type": "file", "id": "rag-file", "url": "/api/v1/files/x"},
                    ],
                },
                "m2": {"role": "assistant", "files": [{"type": "filesystem", "path": "/home/user/out.png"}]},
                "m3": {"role": "user", "files": [{"type": "filesystem", "url": "/home/user/b (1).csv"}]},
            }
        }
    }
    run_inlet(mod, "c1")
    (post,) = posts
    assert post["url"] == "http://oto-orchestrator:8080/api/v1/terminals/uploads/claim"
    assert post["json"] == {
        "context_id": "chat:c1",
        "user_id": "u1",
        "paths": ["/home/user/a.txt", "/home/user/b (1).csv"],
    }
    assert post["headers"] == {"Authorization": "Bearer k"}


@pytest.mark.parametrize(
    "chat_id,user,key",
    [
        ("c1", "u2", "k"),  # not this user's chat
        ("local:abc", "u1", "k"),  # temporary chat
        ("c1", "u1", ""),  # no key configured
        ("c2", "u1", "k"),  # no filesystem attachments
    ],
)
def test_no_claim(filter_mod, chat_id, user, key):
    mod, chats, posts = filter_mod
    chats[("c1", "u1")] = {
        "history": {"messages": {"m": {"role": "user", "files": [{"type": "filesystem", "path": "/p"}]}}}
    }
    chats[("c2", "u1")] = {"history": {"messages": {"m": {"role": "user", "content": "hi"}}}}
    run_inlet(mod, chat_id, user, key)
    assert posts == []
