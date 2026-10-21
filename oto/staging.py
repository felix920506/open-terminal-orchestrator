"""Holding area for uploads made before a chat exists.

Open WebUI uploads a file attached on a new, unsaved chat without any chat id, so there is
no sandbox to put it in yet. The file is kept on disk under its user and reported back with
the path it will have in the sandbox. When the chat's first message is sent, the "Terminal
uploads" filter function reads that path from the saved message and asks the orchestrator to
deliver it (see ``claim``). Unclaimed files expire.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import BinaryIO

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


class StagingError(Exception):
    status_code = 400


class StagingFull(StagingError):
    status_code = 413


@dataclass
class StagedUpload:
    id: str
    user_id: str
    policy_id: str
    path: str  # absolute path the file will have in the sandbox
    size: int
    created: float


class UploadStaging:
    def __init__(self, root: str, ttl_minutes: int, max_file_bytes: int, max_user_bytes: int):
        self.root = root
        self.ttl = ttl_minutes * 60
        self.max_file_bytes = max_file_bytes
        self.max_user_bytes = max_user_bytes
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._delivery_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def delivery_lock(self, user_id: str) -> asyncio.Lock:
        """Held while a user's uploads are delivered, so concurrent claims don't deliver twice."""
        return self._delivery_locks[user_id]

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(self.root, hashlib.sha256(user_id.encode()).hexdigest()[:32])

    def _items(self, user_id: str) -> list[StagedUpload]:
        d = self._user_dir(user_id)
        if not os.path.isdir(d):
            return []
        out = []
        for name in os.listdir(d):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(d, name)) as f:
                        out.append(StagedUpload(**json.load(f)))
                except (OSError, ValueError, TypeError):
                    log.warning("Ignoring unreadable staged upload metadata %s", name)
        return sorted(out, key=lambda i: i.created)

    def _data_path(self, item: StagedUpload) -> str:
        return os.path.join(self._user_dir(item.user_id), f"{item.id}.bin")

    def _remove(self, item: StagedUpload) -> None:
        base = os.path.join(self._user_dir(item.user_id), item.id)
        for suffix in (".json", ".bin"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(base + suffix)

    @staticmethod
    def _unique(path: str, taken: set[str]) -> str:
        if path not in taken:
            return path
        stem, ext = posixpath.splitext(path)
        n = 1
        while f"{stem} ({n}){ext}" in taken:
            n += 1
        return f"{stem} ({n}){ext}"

    async def put(self, user_id: str, policy_id: str, directory: str, filename: str, src: BinaryIO) -> StagedUpload:
        name = posixpath.basename(filename.replace("\\", "/")) or "upload"
        async with self._locks[user_id]:
            existing = [i for i in self._items(user_id) if time.time() - i.created < self.ttl]
            path = self._unique(posixpath.join(directory, name), {i.path for i in existing if i.policy_id == policy_id})
            item = StagedUpload(str(uuid.uuid4()), user_id, policy_id, path, 0, time.time())
            budget = min(self.max_file_bytes, self.max_user_bytes - sum(i.size for i in existing))
            os.makedirs(self._user_dir(user_id), exist_ok=True)
            data = self._data_path(item)
            try:
                with open(data, "wb") as out:
                    while chunk := await asyncio.to_thread(src.read, CHUNK):
                        item.size += len(chunk)
                        if item.size > budget:
                            raise StagingFull(
                                "Too much uploaded before the chat started. Send a message first, then upload it."
                            )
                        await asyncio.to_thread(out.write, chunk)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(data)
                raise
            with open(os.path.join(self._user_dir(user_id), f"{item.id}.json"), "w") as f:
                json.dump(asdict(item), f)
        log.info("Holding upload %s for user %s until its chat is known", path, user_id)
        return item

    async def take(self, user_id: str, paths: list[str]) -> list[StagedUpload]:
        """Unexpired uploads of this user at these paths (oldest first); they stay until ``done``."""
        wanted = set(paths)
        async with self._locks[user_id]:
            return [i for i in self._items(user_id) if i.path in wanted and time.time() - i.created < self.ttl]

    def open(self, item: StagedUpload) -> BinaryIO:
        return open(self._data_path(item), "rb")

    async def done(self, item: StagedUpload) -> None:
        async with self._locks[item.user_id]:
            self._remove(item)

    def stats(self) -> dict[str, int]:
        files = size = 0
        if os.path.isdir(self.root):
            for d in os.listdir(self.root):
                for name in os.listdir(os.path.join(self.root, d)):
                    if name.endswith(".bin"):
                        files += 1
                        size += os.path.getsize(os.path.join(self.root, d, name))
        return {"files": files, "bytes": size}

    def purge_expired(self) -> int:
        """Delete held uploads past their TTL (and orphaned data files)."""
        if not os.path.isdir(self.root):
            return 0
        removed = 0
        now = time.time()
        for d in os.listdir(self.root):
            path = os.path.join(self.root, d)
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if now - os.path.getmtime(full) > self.ttl:
                    os.remove(full)
                    removed += name.endswith(".bin")
            if not os.listdir(path):
                shutil.rmtree(path, ignore_errors=True)
        if removed:
            log.info("Deleted %d held upload(s) that were never attached to a chat", removed)
        return removed
