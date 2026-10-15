"""Database models and session handling."""

from __future__ import annotations

import enum
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, TypeDecorator, UniqueConstraint, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """DateTime that always round-trips as timezone-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class SandboxState(enum.StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"  # container exists, not running
    DEHYDRATED = "dehydrated"  # container removed, volume kept
    FAILED = "failed"


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Open WebUI-visible fields: image, cpu_limit, memory_limit, storage, idle_timeout_minutes, env
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    # Free-form lifecycle knobs (see oto.policies.Lifecycle)
    lifecycle: Mapped[dict] = mapped_column(JSON, default=dict)
    # Encrypted env values, keyed like data["env"]
    env_sealed: Mapped[dict] = mapped_column(JSON, default=dict)
    image_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)  # resolved image id
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|ready|unavailable|error
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Sandbox(Base):
    __tablename__ = "sandboxes"
    __table_args__ = (UniqueConstraint("context_id", "policy_id", name="uq_sandbox_context_policy"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    context_id: Mapped[str] = mapped_column(String(512), index=True)
    policy_id: Mapped[str] = mapped_column(String(128), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(256), index=True)
    session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default=SandboxState.CREATING.value, index=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    volume_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    network_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    api_key_sealed: Mapped[str] = mapped_column(Text)
    image_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    upstream_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SpecCache(Base):
    __tablename__ = "spec_cache"

    image_ref: Mapped[str] = mapped_column(String(512), primary_key=True)
    openapi: Mapped[dict] = mapped_column(JSON)
    api_config: Mapped[dict] = mapped_column(JSON)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    context_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor: Mapped[str] = mapped_column(String(32))  # user|ai|admin|system
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite") and ":///" in url:
            path = url.split(":///", 1)[1]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()

        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
