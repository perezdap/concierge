"""
Configuration loader.

Single YAML file. Pydantic models validate it and surface clear errors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .core.types import RiskLevel


class GatewayHttpConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    bind_public: bool = False
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost", "http://127.0.0.1"])
    path: str = "/mcp"


class AuthConfig(BaseModel):
    type: Literal["none", "bearer", "localhost"] = "localhost"
    bearer_tokens: list[str] = Field(default_factory=list)


class UpstreamServerConfig(BaseModel):
    id: str
    transport: Literal["stdio", "streamable_http", "sse_legacy", "custom"]

    # stdio
    command: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None

    # http / sse
    url: str | None = None
    sse_url: str | None = None
    post_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # custom
    custom_kind: str | None = None
    custom_params: dict[str, Any] = Field(default_factory=dict)

    # metadata defaults applied to every primitive catalogued from this server
    default_risk: RiskLevel = RiskLevel.MEDIUM
    default_tags: list[str] = Field(default_factory=list)
    default_categories: list[str] = Field(default_factory=list)
    requires_auth: bool = False
    requires_approval_for: list[str] = Field(default_factory=list)

    request_timeout_s: float = 30.0

    # Upstream session isolation:
    #   "shared"      — one upstream session for all router sessions (default;
    #                   best for stdio / single-tenant; matches MVP behavior).
    #   "per_session" — lazily create an isolated upstream session per router
    #                   session, pooled + LRU-evicted (best for multi-tenant HTTP).
    isolation: Literal["shared", "per_session"] = "shared"

    # Connect retry/backoff (used for on-demand per_session session creation).
    connect_max_retries: int = 3
    connect_backoff_base_s: float = 0.5
    connect_backoff_max_s: float = 10.0


class ProfileSelectorConfig(BaseModel):
    server: str | None = None
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primitive_type: Literal["tool", "resource", "prompt"] | None = None
    names: list[str] = Field(default_factory=list)


class ProfileConfig(BaseModel):
    name: str
    description: str = ""
    selectors: list[ProfileSelectorConfig] = Field(default_factory=list)


class PolicyConfig(BaseModel):
    rate_limit_capacity: float = 30.0
    rate_limit_refill_per_sec: float = 0.5
    block_dangerous_without_approval: bool = True


class SessionPoolConfig(BaseModel):
    """Tuning for router-session lifecycle and the per-session upstream pool."""
    # Idle router sessions older than this are garbage-collected.
    idle_ttl_s: int = 60 * 60
    # How often the background GC sweep runs.
    gc_interval_s: float = 60.0
    # Global cap on pooled per_session upstream sessions (LRU-evicted past this).
    max_upstream_sessions: int = 256


class PayloadConfig(BaseModel):
    """Controls outbound payload trimming toward downstream LLM clients."""
    # Slim published-tool input schemas in tools/list (model-facing surface).
    slim_tools_list: bool = True
    max_schema_description_chars: int = 160
    drop_schema_examples: bool = True
    # Cap heavy tool-result text (bytes). 0 = disabled (never truncate silently).
    max_result_bytes: int = 0


class GatewayConfig(BaseModel):
    gateway: GatewayHttpConfig = Field(default_factory=GatewayHttpConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upstream_servers: list[UpstreamServerConfig] = Field(default_factory=list)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    session_pool: SessionPoolConfig = Field(default_factory=SessionPoolConfig)
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    log_level: str = "INFO"
    catalog_refresh_interval_s: float = 300.0


def load_config(path: str | Path) -> GatewayConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return GatewayConfig.model_validate(data)
