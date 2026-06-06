"""
Configuration loader.

Single YAML file. Pydantic models validate it and surface clear errors.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .core.types import RiskLevel

_log = logging.getLogger("concierge.config")


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


class WebhookConfig(BaseModel):
    """P1-3: signed approval-decision callbacks.

    On every grant/deny the gateway POSTs a JSON payload signed with HMAC-SHA256
    (``X-Concierge-Signature: sha256=…``). ``tenant_urls`` / ``tenant_secrets`` are
    keyed by ``tenant_id``; ``default_urls`` / ``default_secret`` apply to tenants
    without an explicit entry. A tenant with URLs but no resolvable secret is *not*
    called (an unsigned callback is untrustworthy). Secrets should come from the
    environment via ``${VAR}`` expansion — never commit raw secrets.
    """
    tenant_urls: dict[str, list[str]] = Field(default_factory=dict)
    default_urls: list[str] = Field(default_factory=list)
    tenant_secrets: dict[str, str] = Field(default_factory=dict)
    default_secret: str | None = None
    max_attempts: int = Field(default=4, ge=1)
    backoff_base_s: float = Field(default=0.5, gt=0)
    backoff_max_s: float = Field(default=8.0, gt=0)
    timeout_s: float = Field(default=5.0, gt=0)


class ApprovalConfig(BaseModel):
    """P1-3: the real out-of-band approval queue (``approval_mode == "queue"``).

    The queue lives in shared state so a parked approval survives a replica
    restart and is visible fleet-wide; backend matrix mirrors P1-2/P1-4 (memory is
    tests/dev only). A gated call waits up to ``wait_timeout_s`` for an operator
    decision before the client sees a structured denial; ``ttl_s`` bounds how long
    a record stays grantable. ``operator_subjects`` is the allow-list of auth
    subjects (from the P1-1 chain) permitted to grant/deny — empty means "any
    authenticated principal may decide approvals for their own tenant".
    """
    backend: Literal["memory", "redis", "postgres"] = "memory"
    redis_url: str | None = None
    postgres_url: str | None = None
    ttl_s: float = Field(default=300.0, gt=0)
    wait_timeout_s: float = Field(default=300.0, gt=0)
    poll_interval_s: float = Field(default=1.0, gt=0)
    # Auth subjects allowed to decide approvals. Empty = any authenticated subject
    # may decide, but only for approvals belonging to their own tenant.
    operator_subjects: list[str] = Field(default_factory=list)
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)


class PolicyConfig(BaseModel):
    rate_limit_capacity: float = 30.0
    rate_limit_refill_per_sec: float = 0.5
    block_dangerous_without_approval: bool = True
    # Approval gating for requires_approval / dangerous tools.
    #   "deny"       — deny-by-default; every gated tool is uninvokable (safe MVP default).
    #   "allow_list" — pre-approve canonical tool names in approval_allow_list; deny others.
    #   "queue"      — P1-3 out-of-band workflow: park the call, wait for an operator
    #                  grant/deny (see ``approval`` below + docs/APPROVALS.md).
    approval_mode: Literal["deny", "allow_list", "queue"] = "deny"
    # Canonical tool names ("<server>__<tool>") pre-approved when approval_mode == "allow_list".
    approval_allow_list: list[str] = Field(default_factory=list)
    # P1-3 queue settings (only consulted when approval_mode == "queue").
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
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


def _comment_start(line: str) -> int | None:
    """Return the index of the first unquoted ``#`` in ``line`` (YAML comment
    start), or ``None`` if the line has no comment.

    Tracks a tiny single/double-quote state machine so that a ``#`` inside a
    quoted scalar (``"a # b"`` / ``'a # b'``) is not treated as a comment
    introducer. Backslash escapes are not handled — YAML single-quoted scalars
    do not process escapes, and double-quoted ones use a small subset that
    does not produce ``#``; keeping this simple matches the operator's
    documentation guidance and avoids pretending to be a full YAML parser.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return i
    return None


def expand_env(text: str) -> str:
    """Substitute ${VAR} / ${VAR:-default} from the environment.

    A ${VAR} with no default that is unset is an error — surfacing it beats
    silently passing the literal "${VAR}" through as (e.g.) a secret.

    YAML ``#`` comments are skipped: ``${VAR}`` references appearing inside
    a comment are not expanded and do not trigger the undefined-variable
    error. This lets operators write helpful documentation like
    ``# set ${BM_LIVE_TOKEN} in .env`` without breaking startup. References
    that genuinely appear in YAML values (outside any comment, and not
    inside quoted scalars that contain a ``#``) still raise as before.

    A ${VAR} (no default) that is set to the empty string emits a
    WARNING-level log entry naming the variable. This catches the common
    "operator copied .env.example and forgot to fill in BM_LIVE_TOKEN" footgun
    without breaking the legitimate use case of an explicit empty
    ``${VAR:-}`` default.
    """
    missing: list[str] = []
    # Track set-but-empty (no-default) variables seen across this call so we
    # log a single warning per variable, not one per occurrence.
    empty_no_default: set[str] = set()

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        value = os.environ.get(name)
        if value is not None:
            if value == "" and default is None:
                # Bare ${VAR} (no default) that is set to "" is a likely
                # misconfiguration (forgot-to-fill-in-the-secret), not an
                # intentional empty substitution. Warn loudly.
                empty_no_default.add(name)
            return value
        if default is not None:
            return default
        missing.append(name)
        return ""

    # A single shared `missing` list across the whole call. References inside
    # YAML comments are never scanned, so they cannot end up here; references
    # in actual values that are unset-without-default all get reported in one
    # sorted, de-duplicated error at the end.
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        # Strip the trailing newline (if any) so _comment_start indexes the
        # raw scalar; we re-attach it afterwards so output byte-equivalence
        # with the input is preserved.
        if line.endswith("\n"):
            body, nl = line[:-1], "\n"
        else:
            body, nl = line, ""
        cut = _comment_start(body)
        if cut is None:
            out_lines.append(_ENV_VAR_RE.sub(_sub, body) + nl)
        else:
            out_lines.append(_ENV_VAR_RE.sub(_sub, body[:cut]) + body[cut:] + nl)

    if empty_no_default:
        for name in sorted(empty_no_default):
            # Distinct from the "undefined" error path: set-but-empty is a
            # likely-misconfigured-secret, not a missing env var. Operators
            # get a WARNING they can grep for, not a crash.
            _log.warning(
                "config references %s which is set to the empty string in the "
                "environment; substituting \"\" — if this is a secret (e.g. "
                "BM_LIVE_TOKEN) you almost certainly forgot to fill in the "
                "value in .env. Use ${%s:-} for an intentional empty value.",
                name,
                name,
            )
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ValueError(
            f"config references undefined environment variable(s): {names}. "
            f"Set them, or supply a default with ${{VAR:-default}}."
        )
    return "".join(out_lines)


# TODO(vNEXT): promote the empty-but-set warning above to a hard ValueError
# after operators have had a release cycle to migrate `${VAR}` →
# `${VAR:-}` where they genuinely want the empty value. The plan/issue
# tracker should record the migration deadline so it isn't lost.


def load_config(path: str | Path) -> GatewayConfig:
    data = yaml.safe_load(expand_env(Path(path).read_text())) or {}
    return GatewayConfig.model_validate(data)
