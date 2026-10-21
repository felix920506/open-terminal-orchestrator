"""Uploads made before a chat exists: held, then delivered when the uploads filter claims them."""

from __future__ import annotations

import pytest

from .conftest import ADMIN_H, CONN_H, IMAGE, KEY, chat_headers, put_policy


def no_chat(user: str = "u1") -> dict[str, str]:
    """What Open WebUI sends from a new, unsaved chat: a user but no chat."""
    return {"Authorization": f"Bearer {KEY}", "X-User-Id": user}


def upload(c, name="a.txt", content=b"hello", directory="/home/user", user="u1"):
    return c.post(
        "/p/dev/files/upload", params={"directory": directory}, files={"file": (name, content)}, headers=no_chat(user)
    )


def claim(c, chat, paths, user="u1"):
    r = c.post(
        "/api/v1/terminals/uploads/claim",
        json={"context_id": f"chat:{chat}", "user_id": user, "paths": paths},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def staging_client(make_client, tmp_path):
    c = make_client(stage_uploads=True, staging_dir=str(tmp_path / "staging"))
    put_policy(c, "dev", {"image": IMAGE, "storage": "1Gi"})
    return c


def test_disabled_by_default(client):
    assert upload(client).status_code == 409
    assert client.get("/p/dev/files/cwd", headers=no_chat()).json() == {"cwd": None}
    assert claim(client, "c1", ["/home/user/a.txt"]) == {"delivered": [], "failed": [], "enabled": False}


def test_new_chat_cwd_is_the_sandbox_home(staging_client, runtime):
    assert staging_client.get("/p/dev/files/cwd", headers=no_chat()).json()["cwd"] == "/home/user"
    assert not any(c.running for c in runtime.containers.values())  # only the probe ran


def test_held_upload_is_delivered_to_the_claiming_chat(staging_client, runtime):
    c = staging_client
    r = upload(c)
    assert r.status_code == 200 and r.json() == {"path": "/home/user/a.txt", "size": 5}
    # Same name again before the chat starts: kept apart.
    assert upload(c, content=b"second").json()["path"] == "/home/user/a (1).txt"
    assert c.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json() == []
    assert c.get("/api/v1/status", headers=CONN_H).json()["held_uploads"]["files"] == 2

    # Another user can't claim them, and unknown paths are ignored.
    assert claim(c, "other", ["/home/user/a.txt"], user="u2")["delivered"] == []
    result = claim(c, "c1", ["/home/user/a.txt", "/home/user/a (1).txt", "/home/user/nope"])
    assert sorted(result["delivered"]) == ["/home/user/a (1).txt", "/home/user/a.txt"]

    (box,) = c.get("/api/v1/terminals/sandboxes", headers=ADMIN_H).json()
    assert box["context_id"] == "chat:c1" and box["owner_user_id"] == "u1"
    read = c.get("/p/dev/files/read", params={"path": "/home/user/a.txt"}, headers=chat_headers("c1"))
    assert read.json()["content"] == "hello"
    (sandbox,) = [x for x in runtime.containers.values() if x.running]
    sent = [r for r in sandbox.requests if r.url.path == "/files/upload"]
    assert {r.headers["x-session-id"] for r in sent} == {"c1"}

    # Delivered once only.
    assert claim(c, "c1", ["/home/user/a.txt"])["delivered"] == []
    assert c.get("/api/v1/status", headers=CONN_H).json()["held_uploads"]["files"] == 0


def test_upload_outside_home_refused(staging_client):
    r = upload(staging_client, directory="/etc")
    assert r.status_code == 400 and "/home/user" in r.json()["detail"]
    assert upload(staging_client, directory="/home/user/../../etc").status_code == 400
    assert upload(staging_client, directory="/home/user/sub").json()["path"] == "/home/user/sub/a.txt"


def test_size_limits(make_client, tmp_path):
    c = make_client(stage_uploads=True, staging_dir=str(tmp_path / "s"), staging_max_file_mb=1, staging_max_user_mb=1)
    put_policy(c, "dev", {"image": IMAGE})
    assert upload(c, content=b"x" * (2 * 1024 * 1024)).status_code == 413
    assert upload(c, name="b.txt", content=b"x" * (600 * 1024)).status_code == 200
    assert upload(c, name="c.txt", content=b"x" * (600 * 1024)).status_code == 413  # per-user total
    assert c.get("/api/v1/status", headers=CONN_H).json()["held_uploads"]["files"] == 1


def test_unclaimed_uploads_expire(make_client, tmp_path):
    c = make_client(stage_uploads=True, staging_dir=str(tmp_path / "s"), staging_ttl_minutes=0)
    put_policy(c, "dev", {"image": IMAGE})
    assert upload(c).status_code == 200
    assert claim(c, "c1", ["/home/user/a.txt"])["delivered"] == []
    assert c.post("/api/v1/terminals/gc", headers=ADMIN_H).json()["uploads_expired"] == 1


def test_claim_into_another_users_chat_refused(staging_client):
    c = staging_client
    assert c.post(
        "/p/dev/files/write", json={"path": "/f", "content": "1"}, headers=chat_headers("c1", "u2")
    ).is_success
    upload(c)
    r = c.post(
        "/api/v1/terminals/uploads/claim",
        json={"context_id": "chat:c1", "user_id": "u1", "paths": ["/home/user/a.txt"]},
        headers=ADMIN_H,
    )
    assert r.status_code == 403


def test_claim_needs_admin_key_and_chat_context(staging_client):
    c = staging_client
    body = {"context_id": "chat:c1", "user_id": "u1", "paths": []}
    assert c.post("/api/v1/terminals/uploads/claim", json=body, headers=CONN_H).status_code in (401, 403)
    body["context_id"] = "user:u1"
    assert c.post("/api/v1/terminals/uploads/claim", json=body, headers=ADMIN_H).status_code == 400


def test_new_chat_cwd_follows_the_policy_working_dir(staging_client):
    c = staging_client
    put_policy(c, "dev", {"image": IMAGE}, {"working_dir": "/work"})
    assert c.get("/p/dev/files/cwd", headers=no_chat()).json()["cwd"] == "/work"
    assert upload(c).status_code == 400
    assert upload(c, directory="/work").json()["path"] == "/work/a.txt"
    put_policy(c, "dev", {"image": IMAGE}, {"working_dir": ""})  # image decides: probed value
    assert c.get("/p/dev/files/cwd", headers=no_chat()).json()["cwd"] == "/home/user"
