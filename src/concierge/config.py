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


class GatewayConfig(BaseModel):
    gateway: GatewayHttpConfig = Field(default_factory=GatewayHttpConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upstream_servers: list[UpstreamServerConfig] = Field(default_factory=list)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    log_level: str = "INFO"
    catalog_refresh_interval_s: float = 300.0


def load_config(path: str | Path) -> GatewayConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return GatewayConfig.model_validate(data)
