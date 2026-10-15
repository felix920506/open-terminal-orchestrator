from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from oto.config import Settings
from oto.db import utcnow
from oto.main import create_app

from .fakes import FakeRuntime

IMAGE = "ghcr.io/open-webui/open-terminal:slim"
KEY = "conn-key"
ADMIN = "admin-key"


def chat_headers(chat: str, user: str = "u1", key: str = KEY) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "X-User-Id": user,
        "X-Session-Id": chat,
        "X-Terminal-Context-Id": f"chat:{chat}",
    }


ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
CONN_H = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        api_key=KEY,
        admin_api_key=ADMIN,
        secret_key="test-secret",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/oto.db",
        sandbox_access="network",
        reaper_interval_seconds=3600,
        activity_flush_seconds=0,
        default_image="",
    )


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def make_client(settings, runtime):
    clients = []

    def _make(**overrides) -> TestClient:
        s = settings.model_copy(update=overrides)
        client = TestClient(create_app(s, runtime=runtime, transport=runtime.transport()))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    c = make_client()
    put_policy(c, "dev", {"image": IMAGE, "storage": "1Gi", "idle_timeout_minutes": 10})
    return c


def put_policy(c: TestClient, pid: str, data: dict, lifecycle: dict | None = None) -> None:
    r = c.put(f"/api/v1/policies/{pid}", json=data, headers=CONN_H)
    assert r.status_code == 200, r.text
    if lifecycle is not None:
        r = c.put(f"/api/v1/policies/{pid}/lifecycle", json=lifecycle, headers=CONN_H)
        assert r.status_code == 200, r.text


class Clock:
    """Shifts the reaper's notion of "now"."""

    def __init__(self, monkeypatch):
        self.offset = timedelta()
        monkeypatch.setattr("oto.reaper.utcnow", lambda: utcnow() + self.offset)

    def advance(self, **kw) -> None:
        self.offset += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)
