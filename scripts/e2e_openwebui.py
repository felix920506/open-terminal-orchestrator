"""End-to-end test: a real Open WebUI talking to a real orchestrator, driven over HTTP.

Prerequisites (see scripts/e2e_up.sh):
  * the orchestrator stack from deploy/ running and reachable from Open WebUI at ORCH_INTERNAL_URL
  * an Open WebUI container with an OpenAI-compatible connection (e.g. OpenRouter)

Run:  uv run python scripts/e2e_openwebui.py --model openai/gpt-4.1-nano
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx
from websockets.asyncio.client import connect as ws_connect

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


class OWUI:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token
        self.http = httpx.Client(base_url=self.base, timeout=120)

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    @property
    def version(self) -> tuple[int, ...]:
        if not hasattr(self, "_version"):
            raw = self.http.get("/api/version").json().get("version", "0")
            self._version = tuple(int(x) for x in re.findall(r"\d+", raw)[:3])
        return self._version

    def terminal(self, method: str, terminal_id: str, chat_id: str, path: str, **kw) -> httpx.Response:
        """Call a terminal through Open WebUI's proxy in a chat's context, as the browser does.

        0.12+ puts the chat in the URL; 0.11 sends it as X-Session-Id."""
        if self.version >= (0, 12):
            return self.req(method, f"/api/v1/terminals/{terminal_id}/chats/{chat_id}/{path}", **kw)
        headers = {**kw.pop("headers", {}), "X-Session-Id": chat_id}
        return self.req(method, f"/api/v1/terminals/{terminal_id}/{path}", headers=headers, **kw)

    def signup_or_login(self, name: str, email: str, password: str) -> None:
        r = self.http.post("/api/v1/auths/signin", json={"email": email, "password": password})
        if r.status_code != 200:
            r = self.http.post("/api/v1/auths/signup", json={"name": name, "email": email, "password": password})
        r.raise_for_status()
        self.token = r.json()["token"]
        self.user = r.json()


class Chat:
    """A saved chat whose turns run through Open WebUI's server-side tool loop."""

    def __init__(self, ui: OWUI, model: str, terminal_id: str, title: str):
        self.ui, self.model, self.terminal_id = ui, model, terminal_id
        self.messages: dict[str, dict] = {}
        self.current: str | None = None
        r = ui.req("POST", "/api/v1/chats/new", json={"chat": {"title": title, "models": [model], "history": {}}})
        r.raise_for_status()
        self.id = r.json()["id"]

    def _thread(self) -> list[dict]:
        out, mid = [], self.current
        while mid:
            m = self.messages[mid]
            out.append({"role": m["role"], "content": m["content"]})
            mid = m["parentId"]
        return list(reversed(out))

    def send(self, prompt: str, timeout: float = 180) -> dict:
        uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
        now = int(time.time())
        # Start from the stored history so earlier turns keep their recorded tool calls.
        stored = self.ui.req("GET", f"/api/v1/chats/{self.id}").json()["chat"].get("history") or {}
        self.messages = stored.get("messages") or self.messages
        if self.current:
            self.messages[self.current]["childrenIds"].append(uid)
        self.messages[uid] = {
            "id": uid,
            "parentId": self.current,
            "childrenIds": [aid],
            "role": "user",
            "content": prompt,
            "timestamp": now,
            "models": [self.model],
        }
        self.messages[aid] = {
            "id": aid,
            "parentId": uid,
            "childrenIds": [],
            "role": "assistant",
            "content": "",
            "model": self.model,
            "timestamp": now,
        }
        self.current = aid
        chat = {
            "title": "e2e",
            "models": [self.model],
            "history": {"currentId": aid, "messages": self.messages},
            "messages": [self.messages[k] for k in self.messages],
        }
        self.ui.req("POST", f"/api/v1/chats/{self.id}", json={"chat": chat}).raise_for_status()
        body = {
            "model": self.model,
            "messages": self._thread()[:-1],  # without the empty assistant placeholder
            "stream": True,
            "chat_id": self.id,
            "id": aid,
            "session_id": f"e2e-{uuid.uuid4()}",
            "terminal_id": self.terminal_id,
            "features": {},
            "background_tasks": {"title_generation": False, "tags_generation": False, "follow_up_generation": False},
        }
        r = self.ui.req("POST", "/api/chat/completions", json=body)
        r.raise_for_status()
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.ui.req("GET", f"/api/tasks/chat/{self.id}").json()
            if not t.get("task_ids"):
                break
            time.sleep(1)
        saved = self.ui.req("GET", f"/api/v1/chats/{self.id}").json()
        msg = saved["chat"]["history"]["messages"][aid]
        self.messages[aid]["content"] = msg.get("content", "")
        return msg


def tool_calls(msg: dict) -> list[dict]:
    """Function calls recorded in the assistant message's output items."""
    calls = []
    for item in msg.get("output") or []:
        if item.get("type") == "function_call":
            calls.append(item)
    return calls


def tool_names(msg: dict) -> list[str]:
    return [c.get("name", "") for c in tool_calls(msg)]


def outputs_text(msg: dict) -> str:
    parts = [msg.get("content") or ""]
    for item in msg.get("output") or []:
        if item.get("type") == "function_call_output":
            parts.append(json.dumps(item.get("output")))
    return "\n".join(parts)


def orch(args, method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(
        method, f"{args.orch_url}{path}", headers={"Authorization": f"Bearer {args.orch_admin_key}"}, timeout=60, **kw
    )


def sandbox_for(args, chat_id: str) -> dict | None:
    rows = orch(args, "GET", "/api/v1/terminals/sandboxes", params={"context_id": f"chat:{chat_id}"}).json()
    return rows[0] if rows else None


async def ws_terminal(args, ui: OWUI, chat_id: str) -> str:
    # Open WebUI's UI creates the session through the chat-scoped proxy, then attaches over WS.
    r = ui.terminal("POST", args.terminal_id, chat_id, "api/terminals", json={})
    r.raise_for_status()
    sid = r.json()["id"]
    url = args.owui_url.replace("http", "ws", 1) + f"/api/v1/terminals/{args.terminal_id}/api/terminals/{sid}"
    async with ws_connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": ui.token, "chat_id": chat_id}))
        await ws.send(b"echo ws-$((6*7)) && cat ~/hello.txt\n")
        out = b""
        try:
            while b"from chat A" not in out or b"ws-42" not in out:
                m = await asyncio.wait_for(ws.recv(), 10)
                out += m if isinstance(m, bytes) else m.encode()
        except (TimeoutError, Exception):
            pass
        return out.decode(errors="replace")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--owui-url", default="http://127.0.0.1:13000")
    p.add_argument("--orch-url", default="http://127.0.0.1:18090", help="orchestrator, as reached from this script")
    p.add_argument("--orch-internal-url", default="http://oto-orchestrator:8080", help="as reached from Open WebUI")
    p.add_argument("--orch-key", default="e2e-conn-key")
    p.add_argument("--orch-admin-key", default="e2e-admin-key")
    p.add_argument("--terminal-id", default="oto")
    p.add_argument("--policy-id", default="default")
    p.add_argument("--model", default="openai/gpt-4.1-nano")
    args = p.parse_args()

    admin = OWUI(args.owui_url)
    admin.signup_or_login("Admin", "admin@e2e.local", "e2e-password-1")
    check("admin signed in", admin.user.get("role") == "admin", admin.user.get("role", ""))

    # A second, non-admin user.
    r = admin.req(
        "POST",
        "/api/v1/auths/add",
        json={"name": "Bob", "email": "bob@e2e.local", "password": "e2e-password-2", "role": "user"},
    )
    bob = OWUI(args.owui_url)
    bob.signup_or_login("Bob", "bob@e2e.local", "e2e-password-2")

    # ── connection setup, as the admin UI does it ─────────────────────────
    r = admin.req(
        "POST",
        "/api/v1/configs/terminal_servers/verify",
        json={"url": args.orch_internal_url, "key": args.orch_key, "auth_type": "bearer"},
    )
    check(
        "Open WebUI detects the orchestrator", r.status_code == 200 and r.json().get("type") == "orchestrator", r.text
    )

    r = admin.req(
        "POST",
        "/api/v1/configs/terminal_servers/policy",
        json={
            "url": args.orch_internal_url,
            "key": args.orch_key,
            "auth_type": "bearer",
            "policy_id": args.policy_id,
            "policy_data": {
                "image": "ghcr.io/open-webui/open-terminal:slim",
                "storage": "1Gi",
                "memory_limit": "1Gi",
                "cpu_limit": "1",
                "idle_timeout_minutes": 30,
            },
        },
    )
    check("policy saved through Open WebUI", r.status_code == 200, r.text[:200])
    r = admin.req(
        "POST",
        "/api/v1/configs/terminal_servers/lifecycle",
        json={
            "url": args.orch_internal_url,
            "key": args.orch_key,
            "auth_type": "bearer",
            "policy_id": args.policy_id,
            "lifecycle_data": {"retention_days": 7},
        },
    )
    check("lifecycle saved through Open WebUI", r.status_code == 200, r.text[:200])
    r = admin.req(
        "POST",
        "/api/v1/configs/terminal_servers/policy",
        json={"url": args.orch_internal_url, "key": args.orch_key, "auth_type": "bearer", "policy_id": args.policy_id},
    )
    check("policy read back through Open WebUI", r.json().get("data", {}).get("storage") == "1Gi", r.text[:200])

    connection = {
        "id": args.terminal_id,
        "name": "Orchestrated terminal",
        "enabled": True,
        "url": args.orch_internal_url,
        "path": "/openapi.json",
        "key": args.orch_key,
        "auth_type": "bearer",
        "server_type": "orchestrator",
        "policy_id": args.policy_id,
        "config": {
            "contexts": {"chat": {"context_id": "chat_id"}},
            "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}],
        },
    }
    r = admin.req("POST", "/api/v1/configs/terminal_servers", json={"TERMINAL_SERVER_CONNECTIONS": [connection]})
    check("terminal connection saved", r.status_code == 200, r.text[:200])
    servers = bob.req("GET", "/api/v1/terminals/").json()
    check("non-admin user sees the terminal", any(s["id"] == args.terminal_id for s in servers), json.dumps(servers))

    # ── chat A: the model uses the terminal ───────────────────────────────
    chat_a = Chat(admin, args.model, args.terminal_id, "e2e chat A")
    msg = chat_a.send(
        "Use the run_command tool with wait=10 to run exactly this shell command and tell me its output: "
        "echo 'from chat A' > ~/hello.txt && cat ~/hello.txt && hostname"
    )
    names = tool_names(msg)
    check("chat A: model called a terminal tool", "run_command" in names, f"tools={names}")
    check("chat A: command output reached the model", "from chat A" in outputs_text(msg), msg.get("content", "")[:200])
    box_a = sandbox_for(args, chat_a.id)
    check(
        "chat A: sandbox created for this chat",
        bool(box_a) and box_a["state"] == "running",
        json.dumps(box_a)[:300] if box_a else "none",
    )
    if box_a:
        check("chat A: sandbox owned by the chatting user", box_a["owner_user_id"] == admin.user["id"])

    r = admin.terminal("GET", args.terminal_id, chat_a.id, "files/cwd")
    check("chat A: file browser opens in the persisted home", r.json().get("home") == "/home/user", r.text[:200])

    # ── chat B: separate sandbox ──────────────────────────────────────────
    chat_b = Chat(admin, args.model, args.terminal_id, "e2e chat B")
    msg = chat_b.send(
        "Use the run_command tool with wait=10 to run exactly: "
        "cat ~/hello.txt 2>&1; ls -la ~ | head -20 ; and report the output."
    )
    text = outputs_text(msg)
    check("chat B: model called a terminal tool", "run_command" in tool_names(msg), f"tools={tool_names(msg)}")
    check("chat B: cannot see chat A's file", "No such file" in text and "from chat A\\n" not in text, text[:300])
    box_b = sandbox_for(args, chat_b.id)
    check("chat B: has its own sandbox", bool(box_b) and box_a and box_b["id"] != box_a["id"])

    # ── chat A again: data still there; file sidebar via Open WebUI proxy ─
    msg = chat_a.send("Use run_command with wait=10 to run: cat ~/hello.txt")
    check("chat A (turn 2): file still there", "from chat A" in outputs_text(msg), outputs_text(msg)[:200])

    r = admin.terminal(
        "GET",
        args.terminal_id,
        chat_a.id,
        "files/read",
        params={"path": "/home/user/hello.txt"},
    )
    check(
        "file sidebar (chat-scoped proxy) reads chat A's file",
        r.status_code == 200 and "from chat A" in r.text,
        r.text[:200],
    )
    r = admin.terminal(
        "GET",
        args.terminal_id,
        chat_b.id,
        "files/read",
        params={"path": "/home/user/hello.txt"},
    )
    check(
        "file sidebar for chat B does not see chat A's file",
        r.status_code != 200 or "from chat A" not in r.text,
        r.text[:200],
    )

    # ── another user cannot reach chat A's sandbox by guessing its id ─────
    r = bob.terminal(
        "GET",
        args.terminal_id,
        chat_a.id,
        "files/read",
        params={"path": "/home/user/hello.txt"},
    )
    check(
        "other user is refused chat A's sandbox",
        r.status_code == 403 and "from chat A" not in r.text,
        f"{r.status_code} {r.text[:200]}",
    )

    # ── interactive terminal over Open WebUI's WebSocket proxy ────────────
    out = asyncio.run(ws_terminal(args, admin, chat_a.id))
    check("interactive terminal (WS) attaches to chat A's sandbox", "ws-42" in out and "from chat A" in out, out[-200:])

    # ── idle stop + resume (forced via admin API) ─────────────────────────
    if box_a:
        orch(args, "POST", f"/api/v1/terminals/sandboxes/{box_a['id']}/stop")
        check("chat A: stopped", sandbox_for(args, chat_a.id)["state"] == "stopped")
        msg = chat_a.send("Use run_command with wait=10 to run: cat ~/hello.txt")
        resumed = sandbox_for(args, chat_a.id)
        check(
            "chat A: model's tool call resumed the stopped sandbox",
            "run_command" in tool_names(msg)
            and resumed["state"] == "running"
            and resumed["last_started_at"] != box_a["last_started_at"],
            f"tools={tool_names(msg)} state={resumed['state']}",
        )
        check(
            "chat A: command output after resume reached the model",
            "from chat A" in outputs_text(msg),
            "(model-dependent) " + outputs_text(msg)[:160],
        )
        r = admin.terminal(
            "GET",
            args.terminal_id,
            chat_a.id,
            "files/read",
            params={"path": "/home/user/hello.txt"},
        )
        check("chat A: data intact after stop/resume", "from chat A" in r.text, r.text[:120])

    # ── admin "Refresh Terminals" (keep data): container recreated from the volume ──
    r = admin.req(
        "POST",
        "/api/v1/configs/terminal_servers/refresh",
        json={
            "url": args.orch_internal_url,
            "key": args.orch_key,
            "auth_type": "bearer",
            "policy_id": args.policy_id,
            "only_idle": False,
            "reset": False,
        },
    )
    check("admin refresh through Open WebUI", r.status_code == 200 and r.json().get("refreshed", 0) >= 1, r.text[:200])
    check("chat A: dehydrated after refresh", sandbox_for(args, chat_a.id)["state"] == "dehydrated")
    r = admin.terminal(
        "GET",
        args.terminal_id,
        chat_a.id,
        "files/read",
        params={"path": "/home/user/hello.txt"},
    )
    check("chat A: recreated container still has the data", "from chat A" in r.text, r.text[:120])

    # ── the model resets the sandbox ──────────────────────────────────────
    msg = chat_a.send(
        "I want a clean slate. Please wipe and reset this conversation's terminal sandbox now using the "
        "reset_sandbox tool with confirm=true. Do not ask me for confirmation, I confirm."
    )
    check("model called reset_sandbox", "reset_sandbox" in tool_names(msg), f"tools={tool_names(msg)}")
    box = sandbox_for(args, chat_a.id)
    check("chat A: generation incremented by reset", bool(box) and box["generation"] >= 2, json.dumps(box)[:200])
    r = admin.terminal(
        "GET",
        args.terminal_id,
        chat_a.id,
        "files/read",
        params={"path": "/home/user/hello.txt"},
    )
    check("chat A: file gone after reset", "from chat A" not in r.text, f"{r.status_code} {r.text[:120]}")

    # ── chat deletion cleans up via the event function ────────────────────
    code = (Path(__file__).resolve().parent.parent / "integrations/owui/event_chat_cleanup.py").read_text()
    admin.req("DELETE", "/api/v1/functions/id/oto_cleanup/delete")
    r = admin.req(
        "POST",
        "/api/v1/functions/create",
        json={
            "id": "oto_cleanup",
            "name": "Terminal sandbox cleanup",
            "content": code,
            "meta": {"description": "e2e"},
        },
    )
    ok = check("event function installed", r.status_code == 200, r.text[:200])
    if ok:
        admin.req(
            "POST",
            "/api/v1/functions/id/oto_cleanup/valves/update",
            json={"orchestrator_url": args.orch_internal_url, "admin_api_key": args.orch_admin_key},
        )
        state = admin.req("GET", "/api/v1/functions/id/oto_cleanup").json()
        if not state.get("is_active"):
            admin.req("POST", "/api/v1/functions/id/oto_cleanup/toggle")
        admin.req("DELETE", f"/api/v1/chats/{chat_b.id}")
        gone = False
        for _ in range(20):
            if sandbox_for(args, chat_b.id) is None:
                gone = True
                break
            time.sleep(0.5)
        check("deleting chat B destroys its sandbox", gone)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
