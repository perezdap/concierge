"""
Configuration loader.

Single YAML file. Pydantic models validate it and surface clear errors.
"""
from __future__ import annotations

import os
import re
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


class OidcProviderConfig(BaseModel):
    """OIDC/OAuth2 provider (P1-1): verify id_token / JWT access token from an IdP.

    The IdP's signing keys are discovered from ``{issuer}/.well-known/openid-
    configuration`` (override with ``discovery_url`` / ``jwks_uri``) and cached
    for ``jwks_ttl_s`` with automatic ``kid``-rotation refresh. ``audiences`` and
    ``allowed_issuers`` are enforced exactly; ``leeway_s`` tolerates clock skew on
    exp/nbf/iat. The tenant is read from ``tenant_claim`` (falls back to
    ``default_tenant``).
    """
    issuer: str
    audiences: list[str] = Field(min_length=1)
    allowed_issuers: list[str] = Field(default_factory=list)
    discovery_url: str | None = None
    jwks_uri: str | None = None
    jwks_ttl_s: int = Field(default=3600, gt=0)
    leeway_s: int = Field(default=60, ge=0)
    tenant_claim: str = "tenant"
    default_tenant: str = "default"
    http_timeout_s: float = Field(default=5.0, gt=0)


class MtlsProviderConfig(BaseModel):
    """mTLS pass-through provider (P1-1) for service-to-service calls.

    TLS / client-cert validation happens at the reverse proxy or ingress (see
    ``docs/DEPLOYMENT.md``); the gateway reads the forwarded, already-validated
    cert identity from ``subject_header``. Those headers are honored ONLY when the
    request's immediate peer IP falls inside one of ``trusted_proxy_cidrs`` —
    otherwise the request is rejected, so a direct client cannot forge the header.
    An empty CIDR list trusts nobody (deny-by-default).
    """
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    subject_header: str = "x-forwarded-client-cert"
    verify_header: str | None = "ssl-client-verify"
    subject_tenant_map: dict[str, str] = Field(default_factory=dict)
    default_tenant: str = "default"


class RevocationConfig(BaseModel):
    """Shared revocation list (P1-1). Enforced on every auth check, all providers.

    Keyed by token id (OIDC ``jti`` / opaque id for static + tenant tokens). The
    backend must be shared in prod so a revocation survives a replica restart and
    is visible fleet-wide; ``memory`` is for tests/dev only. When ``redis_url`` /
    ``postgres_url`` are unset they fall back to ``storage.redis_url`` /
    ``storage.catalog_postgres_url``.
    """
    backend: Literal["memory", "redis", "postgres"] = "memory"
    redis_url: str | None = None
    postgres_url: str | None = None


class TenantTokenConfig(BaseModel):
    """Per-tenant minted-token store (P1-1). Same backend matrix as revocation."""
    backend: Literal["memory", "redis", "postgres"] = "memory"
    redis_url: str | None = None
    postgres_url: str | None = None


class AuthConfig(BaseModel):
    """Authentication config.

    Backwards compatible: with ``providers`` empty, the legacy ``type`` selects a
    single provider exactly as before (none | bearer | localhost). Set
    ``providers`` to enable the P1-1 chain — multiple providers tried in order,
    first matching credential shape wins, deny if none match. ``tenant_token``,
    ``oidc``, and ``mtls`` are only consulted when the matching provider is listed.
    """
    type: Literal["none", "bearer", "localhost"] = "localhost"
    bearer_tokens: list[str] = Field(default_factory=list)

    # P1-1 provider chain. Each entry names a provider; order is significant.
    providers: list[Literal["bearer", "tenant_token", "oidc", "mtls", "localhost"]] = (
        Field(default_factory=list)
    )
    oidc: OidcProviderConfig | None = None
    mtls: MtlsProviderConfig | None = None
    revocation: RevocationConfig = Field(default_factory=RevocationConfig)
    tenant_token: TenantTokenConfig = Field(default_factory=TenantTokenConfig)


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
    # Publish this profile's tools automatically at session init, so they appear
    # in the very first tools/list. Lets clients that don't react to
    # notifications/tools/list_changed still reach the proxied tools. Opt-in.
    auto_apply: bool = False


class TenantQuotaConfig(BaseModel):
    """Per-tenant token-bucket override. Omitted fields fall back to the default."""
    capacity: float = Field(gt=0)
    refill_per_sec: float = Field(ge=0)


class RateLimitConfig(BaseModel):
    """P1-4: distributed rate limiter backend selection + per-tenant quotas.

    Buckets are keyed by ``(tenant, session, tool)``. ``backend="redis"`` shares
    state across replicas via an atomic Lua token bucket; ``backend="memory"``
    (default) keeps state in-process. When ``redis_url`` is unset it falls back
    to ``storage.redis_url`` so a single Redis can back both P1-2 and P1-4.
    """
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    # Per-tenant capacity/refill overrides, keyed by tenant_id.
    tenant_quotas: dict[str, TenantQuotaConfig] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    rate_limit_capacity: float = 30.0
    rate_limit_refill_per_sec: float = 0.5
    block_dangerous_without_approval: bool = True
    # Approval gating for requires_approval / dangerous tools.
    #   "deny"       — deny-by-default; every gated tool is uninvokable (safe MVP default).
    #   "allow_list" — pre-approve canonical tool names in approval_allow_list; deny others.
    # The full out-of-band approval workflow (queue + operator console) is P1-3.
    approval_mode: Literal["deny", "allow_list"] = "deny"
    # Canonical tool names ("<server>__<tool>") pre-approved when approval_mode == "allow_list".
    approval_allow_list: list[str] = Field(default_factory=list)
    # P1-4: distributed limiter. The legacy rate_limit_* fields above remain the
    # *default* quota; ratelimit.tenant_quotas layers per-tenant overrides on top.
    ratelimit: RateLimitConfig = Field(default_factory=RateLimitConfig)


class SessionPoolConfig(BaseModel):
    """Tuning for router-session lifecycle and the per-session upstream pool."""
    # Idle router sessions older than this are garbage-collected.
    idle_ttl_s: int = Field(default=60 * 60, gt=0)
    # How often the background GC sweep runs.
    gc_interval_s: float = Field(default=60.0, gt=0)
    # Global cap on pooled per_session upstream sessions (LRU-evicted past this).
    max_upstream_sessions: int = Field(default=256, gt=0)
    # P1-7: on SIGTERM, how long to wait for in-flight calls to finish before
    # tearing down adapters/stores. Keep below the orchestrator's grace period
    # (k8s terminationGracePeriodSeconds) so the app drains before SIGKILL.
    drain_grace_period_s: float = Field(default=25.0, ge=0)


class StorageConfig(BaseModel):
    """P1-2: persistent/shared store selection."""
    catalog_store: Literal["memory", "sqlite", "postgres"] = "memory"
    catalog_sqlite_path: str | None = None
    catalog_postgres_url: str | None = None
    session_store: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None


class PayloadConfig(BaseModel):
    """Controls outbound payload trimming toward downstream LLM clients."""
    # Slim published-tool input schemas in tools/list (model-facing surface).
    # Opt-in for compatibility.
    slim_tools_list: bool = False
    max_schema_description_chars: int = Field(default=160, ge=0)
    drop_schema_examples: bool = True
    # Cap heavy tool-result text (bytes). 0 = disabled (never truncate silently).
    max_result_bytes: int = Field(default=0, ge=0)


class OutputConfig(BaseModel):
    """P1-8: response-side output filtering (secret/PII redaction, caps, content-type).
    Conservative defaults: disabled (pass-through) until operator explicitly enables.
    """
    enable_output_filter: bool = False
    max_result_bytes: int = Field(default=8192, ge=0)  # soft cap for filter
    redact_secrets: bool = True
    allow_content_types: list[str] = Field(default_factory=lambda: ["text", "json", "markdown"])


class CacheConfig(BaseModel):
    """P1-8: TTL caching for idempotent reads (resources/prompts). Disabled by default."""
    enable_cache: bool = False
    default_ttl_s: float = Field(default=300.0, gt=0)
    cache_resources: bool = True
    cache_prompts: bool = False


class ObservabilityConfig(BaseModel):
    """P1-6: health/readiness, Prometheus metrics, traces, and audit sinks."""
    enable_metrics: bool = True
    metrics_path: str = "/metrics"
    health_path: str = "/healthz"
    ready_path: str = "/readyz"
    audit_http_sink_url: str | None = None
    audit_http_timeout_s: float = Field(default=2.0, gt=0)


class GatewayConfig(BaseModel):
    gateway: GatewayHttpConfig = Field(default_factory=GatewayHttpConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    upstream_servers: list[UpstreamServerConfig] = Field(default_factory=list)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    session_pool: SessionPoolConfig = Field(default_factory=SessionPoolConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)  # P1-2
    payload: PayloadConfig = Field(default_factory=PayloadConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)  # P1-8 additive
    cache: CacheConfig = Field(default_factory=CacheConfig)      # P1-8 additive
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    log_level: str = "INFO"
    catalog_refresh_interval_s: float = 300.0


# ${VAR} or ${VAR:-default}. Names follow shell identifier rules.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    """Substitute ${VAR} / ${VAR:-default} from the environment.

    A ${VAR} with no default that is unset is an error — surfacing it beats
    silently passing the literal "${VAR}" through as (e.g.) a secret.
    """
    missing: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        value = os.environ.get(name)
        if value is not None:
            return value
        if default is not None:
            return default
        missing.append(name)
        return ""

    expanded = _ENV_VAR_RE.sub(_sub, text)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ValueError(
            f"config references undefined environment variable(s): {names}. "
            "Set them, or supply a default with ${VAR:-default}."
        )
    return expanded


def load_config(path: str | Path) -> GatewayConfig:
    data = yaml.safe_load(expand_env(Path(path).read_text())) or {}
    return GatewayConfig.model_validate(data)
