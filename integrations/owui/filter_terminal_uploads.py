"""
title: Terminal uploads
author: open-terminal-orchestrator
description: Delivers files attached before a chat's first message into that chat's terminal sandbox.
version: 0.1.0
requirements: httpx
"""

# Needed when terminal connections use "Chat Uploads: Filesystem". Open WebUI uploads a file
# attached on a new chat before the chat exists, so the orchestrator holds it
# (ORCH_STAGE_UPLOADS=true). When the message is sent, this filter reads the attached files'
# paths from the saved chat and asks the orchestrator to put them into the chat's sandbox,
# before the model runs.
#
# Install in Open WebUI: Admin Panel → Functions → "+" → paste this file → save, enable it and
# make it Global (⋯ menu → Global), so it runs for every model. Set the valves.

import logging

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Filter:
    class Valves(BaseModel):
        orchestrator_url: str = Field(default="http://oto-orchestrator:8080", description="Orchestrator base URL")
        admin_api_key: str = Field(default="", description="ORCH_ADMIN_API_KEY (or ORCH_API_KEY if no admin key)")
        timeout_seconds: float = Field(default=120.0, description="Includes starting the chat's sandbox")

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None) -> dict:
        chat_id = (__metadata__ or {}).get("chat_id") or body.get("chat_id")
        user_id = (__user__ or {}).get("id")
        if not chat_id or not user_id or not self.valves.admin_api_key or chat_id.startswith("local:"):
            return body
        paths = await self._attached_paths(chat_id, user_id)
        if not paths:
            return body
        try:
            async with httpx.AsyncClient(timeout=self.valves.timeout_seconds) as client:
                r = await client.post(
                    f"{self.valves.orchestrator_url.rstrip('/')}/api/v1/terminals/uploads/claim",
                    json={"context_id": f"chat:{chat_id}", "user_id": user_id, "paths": paths},
                    headers={"Authorization": f"Bearer {self.valves.admin_api_key}"},
                )
                r.raise_for_status()
                result = r.json()
                if result.get("delivered") or result.get("failed"):
                    log.info("Terminal uploads for chat %s: %s", chat_id, result)
        except httpx.HTTPError as e:
            log.warning("Terminal uploads for chat %s failed: %s", chat_id, e)
        return body

    @staticmethod
    async def _attached_paths(chat_id: str, user_id: str) -> list[str]:
        """Paths of files the user attached to this chat's messages in filesystem upload mode."""
        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
        if not chat:
            return []
        messages = ((chat.chat or {}).get("history") or {}).get("messages") or {}
        paths = set()
        for message in messages.values():
            if message.get("role") != "user":
                continue
            for item in message.get("files") or []:
                if item.get("type") == "filesystem":
                    path = item.get("path") or item.get("url") or item.get("id")
                    if isinstance(path, str) and path.startswith("/"):
                        paths.add(path)
        return sorted(paths)
