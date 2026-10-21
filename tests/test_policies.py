from __future__ import annotations

import pytest

from oto.db import Policy, StateEntry
from oto.policies import parse_cpu, parse_memory

from .conftest import CONN_H, IMAGE, chat_headers, put_policy


@pytest.mark.parametrize(
    "value,expected",
    [("1Gi", 1024**3), ("512Mi", 512 * 1024**2), ("1g", 1024**3), ("1GB", 1000**3), ("100", 100), (None, None)],
)
def test_parse_memory(value, expected):
    assert parse_memory(value) == expected


@pytest.mark.parametrize("value,expected", [("1", 10**9), ("0.5", 5 * 10**8), ("500m", 5 * 10**8), ("", None)])
def test_parse_cpu(value, expected):
    assert parse_cpu(value) == expected


def test_policy_roundtrip_matches_open_webui_contract(make_client):
    c = make_client()
    assert c.get("/api/v1/policies", headers=CONN_H).status_code == 200  # orchestrator detection
    assert c.get("/api/v1/policies/dev", headers=CONN_H).status_code == 404
    assert c.get("/api/v1/policies/dev/lifecycle", headers=CONN_H).json() == {"id": "dev", "data": {}}

    data = {
        "image": IMAGE,
        "cpu_limit": "1",
        "memory_limit": "1Gi",
        "storage": "5Gi",
        "idle_timeout_minutes": 15,
        "env": {"FOO": "bar"},
    }
    put_policy(c, "dev", data, {"retention_days": 7})
    got = c.get("/api/v1/policies/dev", headers=CONN_H).json()
    assert got["data"] == data
    assert c.get("/api/v1/policies/dev/lifecycle", headers=CONN_H).json()["data"] == {"retention_days": 7}


def test_env_is_encrypted_at_rest(make_client):
    c = make_client()
    put_policy(c, "dev", {"image": IMAGE, "env": {"TOKEN": "s3cret"}})
    db = c.app.state.oto.db

    async def raw():
        async with db.session() as s:
            row = await s.get(Policy, "dev")
            return row.env_sealed, row.data

    sealed, data = c.portal.call(raw)
    assert "s3cret" not in str(sealed) and "s3cret" not in str(data)
    assert c.get("/api/v1/policies/dev", headers=CONN_H).json()["data"]["env"] == {"TOKEN": "s3cret"}


@pytest.mark.parametrize(
    "data",
    [
        {"image": "docker.io/evil/image:latest"},
        {"image": IMAGE, "memory_limit": "lots"},
        {"image": IMAGE, "env": {"OPEN_TERMINAL_API_KEY": "x"}},
        {"image": IMAGE, "env": {"BAD NAME": "x"}},
    ],
)
def test_invalid_policies_rejected(make_client, data):
    c = make_client()
    assert c.put("/api/v1/policies/dev", json=data, headers=CONN_H).status_code == 400


def test_invalid_lifecycle_rejected(make_client):
    c = make_client()
    put_policy(c, "dev", {"image": IMAGE})
    r = c.put("/api/v1/policies/dev/lifecycle", json={"egress": "sideways"}, headers=CONN_H)
    assert r.status_code == 400


def test_airgapped_forbids_egress(make_client, runtime):
    c = make_client(airgapped=True)
    put_policy(c, "dev", {"image": IMAGE, "storage": "1Gi"})
    r = c.put("/api/v1/policies/dev/lifecycle", json={"egress": "open"}, headers=CONN_H)
    assert r.status_code == 400
    # Default egress is forced to none: no shared egress network is attached.
    c.post("/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("A"))
    (container,) = runtime.containers.values()
    assert container.spec.extra_networks == []
    assert "oto-egress" not in runtime.networks
    assert runtime.networks[container.spec.network]["internal"] is True


def test_missing_image_airgapped(make_client, runtime):
    c = make_client(airgapped=True, image_allowlist="*")
    put_policy(c, "dev", {"image": "registry.local/not-loaded:1"})
    r = c.get("/p/dev/files/cwd", headers=chat_headers("A"))
    assert r.status_code == 503
    status = c.get("/api/v1/status", headers=CONN_H).json()
    assert status["policies"][0]["status"] == "unavailable"


SCI = "ghcr.io/felix920506/open-terminal-scientific:latest"


def _default_image(c) -> str:
    return c.get("/api/v1/policies/default", headers=CONN_H).json()["data"]["image"]


def test_default_image_follows_env_across_restarts(make_client):
    assert _default_image(make_client(default_image=IMAGE)) == IMAGE
    c = make_client(default_image=SCI)  # restart with a changed ORCH_DEFAULT_IMAGE
    assert _default_image(c) == SCI
    status = c.get("/api/v1/status", headers=CONN_H).json()
    assert status["policies"][0]["id"] == "default" and status["policies"][0]["image"] == SCI


def test_default_image_keeps_other_policy_fields(make_client):
    c = make_client(default_image=IMAGE)
    put_policy(c, "default", {"image": IMAGE, "memory_limit": "2Gi", "env": {"A": "1"}})
    got = make_client(default_image=SCI).get("/api/v1/policies/default", headers=CONN_H).json()["data"]
    assert got == {"image": SCI, "memory_limit": "2Gi", "env": {"A": "1"}}


def test_default_image_set_through_api_wins_over_env(make_client):
    c = make_client(default_image=IMAGE, image_allowlist="*")
    put_policy(c, "default", {"image": "registry.local/custom:1"})
    assert _default_image(make_client(default_image=IMAGE, image_allowlist="*")) == "registry.local/custom:1"
    assert _default_image(make_client(default_image=SCI, image_allowlist="*")) == "registry.local/custom:1"
    # Setting it back to the env-managed image hands control back to the env var.
    c = make_client(default_image=IMAGE, image_allowlist="*")
    put_policy(c, "default", {"image": IMAGE})
    assert _default_image(make_client(default_image=SCI, image_allowlist="*")) == SCI


def test_default_image_upgrade_from_untracked_db(make_client):
    c = make_client(default_image=IMAGE)
    db = c.app.state.oto.db

    async def forget():  # databases from 0.1.0 have no record of the env image
        async with db.session() as s:
            await s.delete(await s.get(StateEntry, "default_image"))
            await s.commit()

    c.portal.call(forget)
    assert _default_image(make_client(default_image=SCI)) == SCI


def test_default_image_bypasses_allowlist(make_client):
    c = make_client(default_image="registry.local/open-terminal-scientific:1")
    assert _default_image(c) == "registry.local/open-terminal-scientific:1"
    r = c.put("/api/v1/policies/other", json={"image": "registry.local/other:1"}, headers=CONN_H)
    assert r.status_code == 400
