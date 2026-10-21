"""Process-wide settings, read from ``ORCH_*`` environment variables."""

from __future__ import annotations

import fnmatch
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCH_", env_file=".env", extra="ignore")

    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    log_level: str = "info"

    # Key Open WebUI uses on the system terminal connection (bearer auth).
    api_key: str = ""
    # Optional separate key for /api/v1/terminals/sandboxes* (integrations, CLI).
    # When unset, api_key is accepted there too.
    admin_api_key: str = ""
    # Open WebUI's WEBUI_SECRET_KEY; enables auth_type=session (user JWT) verification.
    webui_secret_key: str = ""
    # Key for at-rest encryption of container API keys and policy env values.
    secret_key: str = ""

    # Source-address allowlists (comma-separated IPs, CIDRs or hostnames; empty = allow any).
    #  allowed_clients        – every route except /healthz; normally just the Open WebUI backend.
    #  admin_allowed_clients  – if set, replaces allowed_clients for sandbox admin routes
    #                           (/api/v1/terminals/* except refresh, /metrics). Include Open WebUI
    #                           here too if you use the integrations/owui functions.
    #  trusted_proxies        – peers whose X-Forwarded-For is honoured (reverse proxies only).
    allowed_clients: str = ""
    admin_allowed_clients: str = ""
    trusted_proxies: str = ""
    client_dns_refresh_seconds: float = 30.0

    database_url: str = "sqlite+aiosqlite:///./data/oto.db"
    docker_host: str | None = None  # None → DOCKER_HOST env / local socket

    image_allowlist: str = "ghcr.io/open-webui/open-terminal*"
    pull_policy: Literal["never", "if-not-present", "always-on-policy-save"] = "if-not-present"
    airgapped: bool = False

    # How the orchestrator reaches sandbox containers:
    #  network   – orchestrator runs in a container named `self_container` and joins each
    #              sandbox's private internal network (production).
    #  published – sandbox port 8000 is published on `publish_host` (development, orchestrator
    #              running on the host). Sandbox networks are then not internal.
    sandbox_access: Literal["network", "published"] = "network"
    self_container: str = "oto-orchestrator"
    publish_host: str = "127.0.0.1"
    egress_network: str = "oto-egress"

    max_running_global: int = 50
    reaper_interval_seconds: float = 30.0
    activity_flush_seconds: float = 15.0

    # If set, a policy with this id is created on startup when missing, and requests for
    # unknown policies fall back to it.
    default_policy_id: str = "default"
    default_image: str = ""

    name_prefix: str = "oto"

    probe_timeout_seconds: float = 120.0

    extra_labels: dict[str, str] = Field(default_factory=dict)

    @property
    def effective_pull_policy(self) -> str:
        return "never" if self.airgapped else self.pull_policy

    def image_allowed(self, image: str) -> bool:
        if self.default_image and image == self.default_image:  # set by the operator, so trusted
            return True
        patterns = [p.strip() for p in self.image_allowlist.split(",") if p.strip()]
        return any(fnmatch.fnmatchcase(image, p) for p in patterns)


@lru_cache
def get_settings() -> Settings:
    return Settings()
