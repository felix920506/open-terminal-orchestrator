"""
title: Reset terminal
author: open-terminal-orchestrator
description: Adds a message button that wipes this conversation's terminal sandbox and starts it fresh.
version: 0.1.0
requirements: httpx
"""

# Install in Open WebUI: Admin Panel → Functions → "+" → paste this file → enable it, then
# enable it globally or on the models that use the terminal. Set the valves.
#
# The orchestrator only resets the sandbox if it is owned by the clicking user.

import httpx
from pydantic import BaseModel, Field


class Action:
    class Valves(BaseModel):
        orchestrator_url: str = Field(default="http://oto-orchestrator:8080", description="Orchestrator base URL")
        admin_api_key: str = Field(default="", description="ORCH_ADMIN_API_KEY (or ORCH_API_KEY if no admin key)")
        policy_id: str = Field(default="", description="Only reset this policy's sandbox (empty: all for this chat)")
        timeout_seconds: float = Field(default=120.0)

    def __init__(self):
        self.valves = self.Valves()

    async def action(self, body: dict, __user__: dict | None = None, __event_emitter__=None, __event_call__=None):
        chat_id = body.get("chat_id")
        user_id = (__user__ or {}).get("id")
        if not chat_id or not user_id:
            return

        async def status(description: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": description, "done": done}})

        if __event_call__:
            confirmed = await __event_call__(
                {
                    "type": "confirmation",
                    "data": {
                        "title": "Reset terminal?",
                        "message": "This permanently deletes all files and installed packages in this chat's terminal.",
                    },
                }
            )
            if not confirmed:
                return

        await status("Resetting terminal…")
        payload = {"context_id": f"chat:{chat_id}", "user_id": user_id, "reason": "user action"}
        if self.valves.policy_id:
            payload["policy_id"] = self.valves.policy_id
        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_seconds) as client:
                r = await client.post(
                    f"{self.valves.orchestrator_url.rstrip('/')}/api/v1/terminals/contexts/reset",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.valves.admin_api_key}"},
                )
                r.raise_for_status()
                n = r.json().get("reset", 0)
        except httpx.HTTPError as e:
            await status(f"Terminal reset failed: {e}", done=True)
            return
        message = "Terminal reset — it starts fresh on next use." if n else "This chat has no terminal yet."
        await status(message, done=True)
