"""
Core domain types.

These are intentionally framework-agnostic. Transport layers map to/from these.
Pydantic is used for validation but each type has minimal logic — the policy
engine, registry, etc. operate on these as plain data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Primitive identity
# ---------------------------------------------------------------------------

class PrimitiveType(str, Enum):
    TOOL = "tool"
    RESOURCE = "resource"
    PROMPT = "prompt"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DANGEROUS = "dangerous"


class TransportType(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"
    SSE_LEGACY = "sse_legacy"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Catalog entry — the gateway's normalized view of an upstream primitive.
# ---------------------------------------------------------------------------

class ArgumentSummary(BaseModel):
    """Compact representation of one input field, for discovery output."""
    name: str
    type: str = "string"
    required: bool = False
    description: str | None = None


class CatalogEntry(BaseModel):
    """One upstream primitive, after sanitization and normalization."""

    # identity
    canonical_name: str                       # e.g. "github__search_repos"
    upstream_name: str                        # original name from the upstream server
    server_id: str                            # which upstream server it came from
    transport: TransportType
    primitive_type: PrimitiveType

    # display
    display_label: str                        # short, sanitized human label
    title: str | None = None
    short_description: str                    # <= 240 chars, sanitized
    usage_guidance: str | None = None         # "when to use" — sanitized

    # schema
    input_schema: dict[str, Any] | None = None        # full JSON Schema (for tools)
    argument_summary: list[ArgumentSummary] = Field(default_factory=list)

    # taxonomy
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    # safety / policy
    risk_level: RiskLevel = RiskLevel.MEDIUM
    requires_approval: bool = False
    requires_auth: bool = False

    # versioning
    schema_hash: str | None = None
    version: str | None = None

    # state
    cataloged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class PublishedPrimitive(BaseModel):
    """Per-session record of a published catalog entry."""
    canonical_name: str
    primitive_type: PrimitiveType
    enabled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    enabled_by: str = "client"                # "client" | "profile:<name>" | "default"
    callable: bool = True                     # may be false if upstream is down


class Session(BaseModel):
    """Server-side session state. Indexed by MCP-Session-Id."""
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str = "default"                # forward compatibility for multi-tenant
    auth_subject: str | None = None           # populated by AuthProvider
    client_info: dict[str, Any] = Field(default_factory=dict)
    protocol_version: str | None = None

    # publishing state
    published_tools: dict[str, PublishedPrimitive] = Field(default_factory=dict)
    published_resources: dict[str, PublishedPrimitive] = Field(default_factory=dict)
    published_prompts: dict[str, PublishedPrimitive] = Field(default_factory=dict)
    active_profiles: list[str] = Field(default_factory=list)

    # bookkeeping
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def published_set(self, ptype: PrimitiveType) -> dict[str, PublishedPrimitive]:
        if ptype == PrimitiveType.TOOL:
            return self.published_tools
        if ptype == PrimitiveType.RESOURCE:
            return self.published_resources
        return self.published_prompts


# ---------------------------------------------------------------------------
# JSON-RPC + MCP shapes (subset we actually use on the wire)
# ---------------------------------------------------------------------------

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] | list[Any] | None = None


class JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None
    result: Any | None = None
    error: dict[str, Any] | None = None


class JsonRpcNotification(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Adapter status (used by AdapterManager / circuit breaker)
# ---------------------------------------------------------------------------

class AdapterHealth(BaseModel):
    server_id: str
    transport: TransportType
    connected: bool
    last_error: str | None = None
    last_connected_at: datetime | None = None
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None


# ---------------------------------------------------------------------------
# Request context — threaded through policy/audit/adapter layers.
# ---------------------------------------------------------------------------

class RequestContext(BaseModel):
    session_id: str
    tenant_id: str = "default"
    auth_subject: str | None = None
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    origin: str | None = None
