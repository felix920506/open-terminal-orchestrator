"""
title: Terminal sandbox cleanup
author: open-terminal-orchestrator
description: Destroys a conversation's terminal sandbox when the chat is deleted.
version: 0.1.0
requirements: httpx
"""

# Install in Open WebUI: Admin Panel → Functions → "+" → paste this file → enable it.
# Function type: event. Set the valves to the orchestrator URL and ORCH_ADMIN_API_KEY.
# Without this function, sandboxes of deleted chats are still purged by the orchestrator's
# retention reaper, just later.

import logging

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Event:
    class Valves(BaseModel):
        orchestrator_url: str = Field(default="http://oto-orchestrator:8080", description="Orchestrator base URL")
        admin_api_key: str = Field(default="", description="ORCH_ADMIN_API_KEY (or ORCH_API_KEY if no admin key)")
        timeout_seconds: float = Field(default=30.0)

    def __init__(self):
        self.valves = self.Valves()

    async def event(self, event: dict) -> None:
        name = event.get("event")
        subject = event.get("subject") or {}
        if name == "chat.deleted":
            params = {"context_id": f"chat:{subject.get('id')}"}
        elif name == "chat.deleted_all" and subject.get("type") == "user":
            params = {"user_id": str(subject.get("id"))}
        else:
            return
        if not subject.get("id") or not self.valves.admin_api_key:
            return
        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_seconds) as client:
                r = await client.delete(
                    f"{self.valves.orchestrator_url.rstrip('/')}/api/v1/terminals/contexts",
                    params=params,
                    headers={"Authorization": f"Bearer {self.valves.admin_api_key}"},
                )
                r.raise_for_status()
                log.info("Terminal cleanup for %s: %s", params, r.json())
        except httpx.HTTPError as e:
            log.warning("Terminal cleanup for %s failed: %s", params, e)
