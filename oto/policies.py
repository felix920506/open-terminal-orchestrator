"""Policy and lifecycle schemas.

``PolicyData`` mirrors the fields Open WebUI's admin UI reads and writes
(``AddTerminalServerModal.svelte``); ``Lifecycle`` is the free-form JSON the same UI edits
under "Lifecycle", interpreted by the orchestrator.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicyData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image: str | None = None
    cpu_limit: str | None = None  # "1", "0.5", "500m"
    memory_limit: str | None = None  # "1Gi", "512Mi", "1g"
    storage: str | None = None  # None → ephemeral (no volume); "5Gi" → persistent volume
    idle_timeout_minutes: int | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env", mode="before")
    @classmethod
    def _env_strings(cls, v):
        return {str(k): str(val) for k, val in (v or {}).items()}


class Security(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Defaults are compatible with the stock Open Terminal images: the full image relies on
    # sudo (incompatible with no-new-privileges) and the slim entrypoint needs root caps to
    # chown /home/user and drop privileges via gosu.
    no_new_privileges: bool = False
    cap_drop: list[str] = Field(default_factory=lambda: ["NET_RAW", "MKNOD", "AUDIT_WRITE", "SYS_CHROOT"])
    cap_add: list[str] = Field(default_factory=list)
    pids_limit: int = 1024
    runtime: str | None = None  # e.g. "runsc" for gVisor
    read_only_rootfs: bool = False


class Lifecycle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    container_gc_hours: float = 72
    retention_days: float = 30  # 0 → keep forever
    max_background_minutes: float = 120
    start_timeout_seconds: float = 60
    max_running: int = 20
    allow_ai_reset: bool = True
    contextless: Literal["reject", "per_user"] = "reject"
    upgrade: Literal["on_next_start", "manual"] = "on_next_start"
    egress: Literal["none", "allowlist", "open"] = "open"
    allowed_domains: list[str] = Field(default_factory=list)
    home_path: str = "/home/user"
    # Container working directory. Open Terminal reports it as cwd and home, so it decides where
    # relative paths land and where the file browser opens. None → home_path (on the volume);
    # "" → the image's default (/app for the slim image, which is not persisted).
    working_dir: str | None = None
    security: Security = Field(default_factory=Security)


DEFAULT_IDLE_TIMEOUT_MINUTES = 30


class EffectivePolicy(BaseModel):
    """Fully resolved policy used by the sandbox manager."""

    id: str
    image: str
    image_ref: str | None
    nano_cpus: int | None
    memory_bytes: int | None
    persistent: bool
    storage: str | None
    idle_timeout_minutes: int
    env: dict[str, str]
    lifecycle: Lifecycle


_MEM_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "ti": 1024**4,
}


def parse_memory(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", str(value))
    if not m:
        raise ValueError(f"Invalid memory quantity: {value!r}")
    unit = m.group(2).lower()
    if unit not in _MEM_UNITS:
        raise ValueError(f"Unknown memory unit in {value!r}")
    # Docker treats bare "1g" as GiB; follow that for single-letter suffixes.
    if unit in ("k", "m", "g", "t"):
        unit = unit + "i"
    return int(float(m.group(1)) * _MEM_UNITS[unit])


def parse_cpu(value: str | None) -> int | None:
    """Return nano-CPUs from "1", "0.5" or Kubernetes-style "500m"."""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    cpus = float(s[:-1]) / 1000 if s.endswith("m") else float(s)
    if cpus <= 0:
        raise ValueError(f"Invalid CPU limit: {value!r}")
    return int(cpus * 1e9)


def validate_policy_data(data: PolicyData) -> None:
    parse_cpu(data.cpu_limit)
    parse_memory(data.memory_limit)
    if data.storage:
        parse_memory(data.storage)
    if data.idle_timeout_minutes is not None and data.idle_timeout_minutes < 0:
        raise ValueError("idle_timeout_minutes must be >= 0")
    for key in data.env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid env var name: {key!r}")
        if key.startswith("OPEN_TERMINAL_API_KEY"):
            raise ValueError("OPEN_TERMINAL_API_KEY is managed by the orchestrator")
