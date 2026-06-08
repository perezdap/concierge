"""
FastAPI application factory.

Wires every subsystem together from a GatewayConfig and returns a runnable app.
"""
from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from ..adapters.auth_headers import AuthHeaderProvider, OAuthAuthHeaderProvider
from ..adapters.base import UpstreamAdapter
from ..adapters.caching import CachingAdapter
from ..adapters.custom import build_custom_adapter
from ..adapters.manager import AdapterManager
from ..adapters.sse_legacy import LegacySseAdapter
from ..adapters.stdio import StdioAdapter
from ..adapters.streamable_http import StreamableHttpAdapter
from ..admin.config_store import SqliteConfigStore
from ..admin.credential_store import UpstreamCredentialStore
from ..admin.oauth import OAuthPendingStore, UpstreamOAuthService
from ..admin.reload import ReloadCoordinator, RuntimeBundle
from ..admin.secrets import InMemoryCredentialStore, resolve_fernet_key
from ..config import (
    CacheConfig,
    GatewayConfig,
    OutputConfig,
    StorageConfig,
    UpstreamServerConfig,
)
from ..core.catalog import Catalog, InMemoryCatalogStore
from ..core.catalog_store import PostgresCatalogStore, SqliteCatalogStore
from ..core.notifications import NotificationBus
from ..core.publishing import PublishingService
from ..core.session import RedisSessionManager, SessionManager
from ..core.types import PrimitiveType
from ..errors import (
    GW_APPROVAL_REQUIRED,
    GW_FORBIDDEN,
    GW_NOT_PUBLISHED,
    GW_RATE_LIMITED,
    GW_SANITIZATION_FAILED,
    GW_UNAUTHORIZED,
    GW_UNKNOWN_PRIMITIVE,
    GW_UPSTREAM_CIRCUIT_OPEN,
    GW_UPSTREAM_PROTOCOL,
    GW_UPSTREAM_TIMEOUT,
    GW_UPSTREAM_UNAVAILABLE,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    GatewayError,
    Unauthorized,
)
from ..gateway.profiles import Profile, ProfileRegistry, ProfileSelector
from ..gateway.service import GatewayService
from ..observability import (
    HttpAuditSink,
    MetricAuditSink,
    MetricRegistry,
    build_observability_router,
    record_request_metrics,
)
from ..policy.approval import (
    AllowListApprovalBroker,
    ApprovalBroker,
    DenyByDefaultApprovalBroker,
    QueuedApprovalBroker,
)
from ..policy.approval_store import (
    ApprovalStore,
    InMemoryApprovalStore,
    PostgresApprovalStore,
    RedisApprovalStore,
)
from ..policy.engine import PolicyEngine
from ..policy.ratelimit import (
    InMemoryTokenBucketRateLimiter,
    RateLimiter,
    RedisTokenBucketRateLimiter,
    TenantQuota,
)
from ..policy.webhook import WebhookConfig as WebhookDispatchConfig
from ..policy.webhook import WebhookDispatcher
from ..util.audit import AuditLogger
from ..util.log import configure_logging, get_logger
from ..util.output_filter import ContentTypeFilter, LengthCapper, OutputFilter, SecretRedactor
from ..util.payload import PayloadOptions
from .admin import build_admin_router
from .admin_oauth import AdminOAuthDeps, build_admin_oauth_router
from .admin_profiles import AdminProfilesDeps, build_admin_profiles_router
from .admin_static import install_admin_ui
from .admin_upstreams import AdminUpstreamsDeps, build_admin_upstreams_router
from .auth import (
    AuthProvider,
    LocalhostAllowAuth,
    MtlsForwardedAuth,
    NoAuth,
    OidcAuth,
    ProviderChain,
    RevocationEnforcingAuth,
    StaticBearerAuth,
    TenantBearerAuth,
)
from .facade import build_facade_router
from .lifecycle import DrainController
from .origin import OriginAllowlistMiddleware
from .revocation import (
    InMemoryRevocationStore,
    PostgresRevocationStore,
    RedisRevocationStore,
    RevocationStore,
)
from .tenant_tokens import (
    InMemoryTenantTokenStore,
    PostgresTenantTokenStore,
    RedisTenantTokenStore,
    TenantTokenStore,
)

_log = get_logger("concierge.app")

# Gateway-specific JSON-RPC code → HTTP status mapping (errors.py).
# Codes outside this map fall back to 500 (logged as a server error).
_HTTP_STATUS_FOR_GATEWAY_CODE: dict[int, int] = {
    GW_UNAUTHORIZED: 401,
    GW_FORBIDDEN: 403,
    GW_RATE_LIMITED: 429,
    GW_APPROVAL_REQUIRED: 403,
    GW_NOT_PUBLISHED: 404,
    GW_UNKNOWN_PRIMITIVE: 404,
    GW_UPSTREAM_UNAVAILABLE: 502,
    GW_UPSTREAM_TIMEOUT: 504,
    GW_UPSTREAM_CIRCUIT_OPEN: 503,
    GW_UPSTREAM_PROTOCOL: 502,
    GW_SANITIZATION_FAILED: 502,  # treat as bad gateway
    JSONRPC_INVALID_PARAMS: 400,
    JSONRPC_METHOD_NOT_FOUND: 404,
    JSONRPC_INVALID_REQUEST: 400,
    JSONRPC_PARSE_ERROR: 400,
}


def _build_adapter(
    cfg: UpstreamServerConfig,
    *,
    auth_header_provider: AuthHeaderProvider | None = None,
) -> UpstreamAdapter:
    if cfg.transport == "stdio":
        if not cfg.command:
            raise ValueError(f"{cfg.id}: stdio transport requires 'command'")
        return StdioAdapter(
            server_id=cfg.id,
            command=cfg.command,
            env=cfg.env,
            cwd=cfg.cwd,
            request_timeout_s=cfg.request_timeout_s,
        )
    if cfg.transport == "streamable_http":
        if not cfg.url:
            raise ValueError(f"{cfg.id}: streamable_http transport requires 'url'")
        return StreamableHttpAdapter(
            server_id=cfg.id,
            url=cfg.url,
            headers=cfg.headers,
            request_timeout_s=cfg.request_timeout_s,
            auth_header_provider=auth_header_provider,
        )
    if cfg.transport == "sse_legacy":
        if not cfg.sse_url:
            raise ValueError(f"{cfg.id}: sse_legacy transport requires 'sse_url'")
        return LegacySseAdapter(
            server_id=cfg.id,
            sse_url=cfg.sse_url,
            post_url=cfg.post_url,
            headers=cfg.headers,
            request_timeout_s=cfg.request_timeout_s,
            auth_header_provider=auth_header_provider,
        )
    if cfg.transport == "custom":
        if not cfg.custom_kind:
            raise ValueError(f"{cfg.id}: custom transport requires 'custom_kind'")
        return build_custom_adapter(cfg.custom_kind, cfg.id, cfg.custom_params)
    raise ValueError(f"{cfg.id}: unknown transport {cfg.transport}")


def _build_revocation_store(cfg: GatewayConfig) -> RevocationStore:
    """Select the revocation backend (P1-1), mirroring the P1-2/P1-4 pattern."""
    rc = cfg.auth.revocation
    if rc.backend == "redis":
        url = rc.redis_url or cfg.storage.redis_url
        if not url:
            raise ValueError(
                "auth.revocation.redis_url (or storage.redis_url) is required "
                "when auth.revocation.backend=redis"
            )
        return RedisRevocationStore(url)
    if rc.backend == "postgres":
        url = rc.postgres_url or cfg.storage.catalog_postgres_url
        if not url:
            raise ValueError(
                "auth.revocation.postgres_url (or storage.catalog_postgres_url) is "
                "required when auth.revocation.backend=postgres"
            )
        return PostgresRevocationStore(url)
    return InMemoryRevocationStore()


def _build_tenant_token_store(cfg: GatewayConfig) -> TenantTokenStore:
    """Select the tenant-token backend (P1-1)."""
    tt = cfg.auth.tenant_token
    if tt.backend == "redis":
        url = tt.redis_url or cfg.storage.redis_url
        if not url:
            raise ValueError(
                "auth.tenant_token.redis_url (or storage.redis_url) is required "
                "when auth.tenant_token.backend=redis"
            )
        return RedisTenantTokenStore(url)
    if tt.backend == "postgres":
        url = tt.postgres_url or cfg.storage.catalog_postgres_url
        if not url:
            raise ValueError(
                "auth.tenant_token.postgres_url (or storage.catalog_postgres_url) is "
                "required when auth.tenant_token.backend=postgres"
            )
        return PostgresTenantTokenStore(url)
    return InMemoryTenantTokenStore()


def _build_single_provider(
    name: str,
    cfg: GatewayConfig,
    tenant_tokens: TenantTokenStore,
) -> AuthProvider:
    """Construct one named provider for the chain."""
    if name == "bearer":
        return StaticBearerAuth(cfg.auth.bearer_tokens)
    if name == "tenant_token":
        return TenantBearerAuth(tenant_tokens)
    if name == "localhost":
        return LocalhostAllowAuth()
    if name == "oidc":
        if cfg.auth.oidc is None:
            raise ValueError("auth.providers includes 'oidc' but auth.oidc is unset")
        o = cfg.auth.oidc
        return OidcAuth(
            issuer=o.issuer,
            allowed_issuers=o.allowed_issuers or None,
            audiences=o.audiences,
            discovery_url=o.discovery_url,
            jwks_uri=o.jwks_uri,
            jwks_ttl_s=o.jwks_ttl_s,
            leeway_s=o.leeway_s,
            tenant_claim=o.tenant_claim,
            default_tenant=o.default_tenant,
            http_timeout_s=o.http_timeout_s,
        )
    if name == "mtls":
        if cfg.auth.mtls is None:
            raise ValueError("auth.providers includes 'mtls' but auth.mtls is unset")
        m = cfg.auth.mtls
        return MtlsForwardedAuth(
            trusted_proxy_cidrs=m.trusted_proxy_cidrs,
            subject_header=m.subject_header,
            verify_header=m.verify_header,
            subject_tenant_map=m.subject_tenant_map,
            default_tenant=m.default_tenant,
        )
    raise ValueError(f"unknown auth provider: {name!r}")


def _build_auth(
    cfg: GatewayConfig,
    revocation: RevocationStore,
    tenant_tokens: TenantTokenStore,
) -> AuthProvider:
    """Build the auth provider (chain or legacy single) with revocation enforced.

    Backwards compatible: when ``auth.providers`` is empty the legacy ``auth.type``
    drives a single provider exactly as before. ``none`` carries no revocable
    credential, so it is returned bare; every other path enforces the revocation
    list (a chain for ``providers``, a single wrapped provider for the legacy types).
    """
    if cfg.auth.providers:
        chain = [
            _build_single_provider(name, cfg, tenant_tokens)
            for name in cfg.auth.providers
        ]
        return ProviderChain(chain, revocation=revocation)

    if cfg.auth.type == "none":
        return NoAuth()
    if cfg.auth.type == "bearer":
        return RevocationEnforcingAuth(
            StaticBearerAuth(cfg.auth.bearer_tokens), revocation
        )
    return RevocationEnforcingAuth(LocalhostAllowAuth(), revocation)


def _wrap_cache(adapter: UpstreamAdapter, cfg: CacheConfig) -> UpstreamAdapter:
    """Apply the optional read-through cache decorator to an upstream adapter."""
    if not cfg.enable_cache:
        return adapter
    return CachingAdapter(
        adapter,
        default_ttl_s=cfg.default_ttl_s,
        cache_reads=cfg.cache_resources,
        cache_prompts=cfg.cache_prompts,
    )


def _build_output_filter(cfg: OutputConfig) -> OutputFilter | None:
    """Build the optional response-side output filter chain."""
    if not cfg.enable_output_filter:
        return None
    filters: list[Any] = []
    if cfg.redact_secrets:
        filters.append(SecretRedactor())
    if cfg.max_result_bytes > 0:
        filters.append(LengthCapper(max_bytes=cfg.max_result_bytes))
    if cfg.allow_content_types:
        filters.append(ContentTypeFilter(set(cfg.allow_content_types)))
    return OutputFilter(filters)


def _build_approval_store(cfg: GatewayConfig) -> ApprovalStore:
    """Select the approval-queue backend (P1-3), mirroring the P1-1/P1-2 pattern."""
    ac = cfg.policy.approval
    if ac.backend == "redis":
        url = ac.redis_url or cfg.storage.redis_url
        if not url:
            raise ValueError(
                "policy.approval.redis_url (or storage.redis_url) is required "
                "when policy.approval.backend=redis"
            )
        return RedisApprovalStore(url)
    if ac.backend == "postgres":
        url = ac.postgres_url or cfg.storage.catalog_postgres_url
        if not url:
            raise ValueError(
                "policy.approval.postgres_url (or storage.catalog_postgres_url) is "
                "required when policy.approval.backend=postgres"
            )
        return PostgresApprovalStore(url)
    return InMemoryApprovalStore()


def _build_webhook_dispatcher(
    cfg: GatewayConfig, audit: AuditLogger
) -> WebhookDispatcher | None:
    """Build the approval-decision webhook dispatcher, or None when unconfigured."""
    w = cfg.policy.approval.webhooks
    if not w.tenant_urls and not w.default_urls:
        return None
    return WebhookDispatcher(
        WebhookDispatchConfig(
            tenant_urls=dict(w.tenant_urls),
            default_urls=list(w.default_urls),
            tenant_secrets=dict(w.tenant_secrets),
            default_secret=w.default_secret,
            max_attempts=w.max_attempts,
            backoff_base_s=w.backoff_base_s,
            backoff_max_s=w.backoff_max_s,
            timeout_s=w.timeout_s,
        ),
        audit=audit,
    )


def _build_approval(
    cfg: GatewayConfig,
    store: ApprovalStore,
    audit: AuditLogger,
    webhooks: WebhookDispatcher | None,
) -> ApprovalBroker:
    if cfg.policy.approval_mode == "allow_list":
        return AllowListApprovalBroker(cfg.policy.approval_allow_list)
    if cfg.policy.approval_mode == "queue":
        ac = cfg.policy.approval
        return QueuedApprovalBroker(
            store,
            ttl_s=ac.ttl_s,
            wait_timeout_s=ac.wait_timeout_s,
            poll_interval_s=ac.poll_interval_s,
            webhooks=webhooks,
            audit=audit,
        )
    return DenyByDefaultApprovalBroker()


def _build_catalog_store(cfg: StorageConfig) -> Catalog:
    if cfg.catalog_store == "sqlite":
        path = cfg.catalog_sqlite_path or ":memory:"
        return Catalog(SqliteCatalogStore(path))
    if cfg.catalog_store == "postgres":
        if not cfg.catalog_postgres_url:
            raise ValueError("catalog_postgres_url is required when catalog_store=postgres")
        return Catalog(PostgresCatalogStore(cfg.catalog_postgres_url))
    return Catalog(InMemoryCatalogStore())


def _build_session_manager(cfg: GatewayConfig) -> SessionManager:
    spool = cfg.session_pool
    storage = cfg.storage
    if storage.session_store == "redis":
        if not storage.redis_url:
            raise ValueError("redis_url is required when session_store=redis")
        return RedisSessionManager(
            storage.redis_url,
            idle_ttl_seconds=spool.idle_ttl_s,
        )
    return SessionManager(idle_ttl_seconds=spool.idle_ttl_s)


def _runtime_config_db_path(storage: StorageConfig) -> str:
    """SQLite path for admin runtime ConfigStore (shares catalog DB file when set)."""
    if storage.catalog_sqlite_path:
        return storage.catalog_sqlite_path
    return "concierge-runtime-config.db"


@dataclass
class _AppRuntimeSwap:
    """Applies a ReloadCoordinator bundle onto live ``app.state`` handles."""

    app: FastAPI

    def apply_runtime(self, bundle: RuntimeBundle) -> None:
        state = self.app.state
        catalog = getattr(state, "catalog", None)
        if catalog is not None:
            catalog.store = bundle.catalog.store
        else:
            catalog = bundle.catalog

        adapters = getattr(state, "adapters", None)
        if adapters is not None:
            adapters.__dict__.clear()
            adapters.__dict__.update(bundle.adapters.__dict__)
        else:
            adapters = bundle.adapters

        profiles = getattr(state, "profiles", None)
        if profiles is not None:
            profiles.__dict__.clear()
            profiles.__dict__.update(bundle.profiles.__dict__)
        else:
            profiles = bundle.profiles

        service = getattr(state, "service", None)
        if service is not None:
            for attr in (
                "catalog",
                "publishing",
                "adapters",
                "policy",
                "profiles",
                "payload",
                "output_filter",
                "approval_store",
            ):
                setattr(service, attr, getattr(bundle.service, attr))
            service.catalog = catalog
            service.adapters = adapters
            service.profiles = profiles
        else:
            service = bundle.service

        state.catalog = catalog
        state.adapters = adapters
        state.publishing = bundle.publishing
        state.profiles = profiles
        state.service = service
        state.rate_limiter = bundle.rate_limiter


def _assemble_runtime_bundle(
    config: GatewayConfig,
    *,
    sessions: SessionManager,
    bus: NotificationBus,
    audit: AuditLogger,
    metrics: MetricRegistry,
    on_session_evict: Callable[[str], Awaitable[None]],
    auth_header_provider: AuthHeaderProvider | None = None,
) -> RuntimeBundle:
    """Build swappable gateway runtime from a GatewayConfig (hot-reload path)."""
    catalog = _build_catalog_store(config.storage)
    publishing = PublishingService(catalog, bus, sessions=sessions)  # sessions already passed in
    adapters = AdapterManager(
        catalog,
        refresh_interval_s=config.catalog_refresh_interval_s,
        max_upstream_sessions=config.session_pool.max_upstream_sessions,
        audit=audit,
    )
    sessions._on_evict = on_session_evict
    sessions.idle_ttl = config.session_pool.idle_ttl_s
    for srv in config.upstream_servers:
        adapter = _wrap_cache(
            _build_adapter(srv, auth_header_provider=auth_header_provider), config.cache
        )

        def adapter_factory(s: UpstreamServerConfig = srv) -> UpstreamAdapter:
            return _wrap_cache(
                _build_adapter(s, auth_header_provider=auth_header_provider), config.cache
            )

        adapters.register(
            adapter,
            default_risk=srv.default_risk,
            default_tags=srv.default_tags,
            default_categories=srv.default_categories,
            requires_auth=srv.requires_auth,
            requires_approval_for=srv.requires_approval_for,
            isolation=srv.isolation,
            factory=adapter_factory,
            connect_max_retries=srv.connect_max_retries,
            connect_backoff_base_s=srv.connect_backoff_base_s,
            connect_backoff_max_s=srv.connect_backoff_max_s,
        )

    profile_registry = ProfileRegistry()
    for pcfg in config.profiles:
        profile_registry.register(Profile(
            name=pcfg.name,
            description=pcfg.description,
            auto_apply=pcfg.auto_apply,
            selectors=[
                ProfileSelector(
                    server=s.server,
                    tags=s.tags,
                    categories=s.categories,
                    primitive_type=None if s.primitive_type is None else
                        PrimitiveType(s.primitive_type),
                    names=s.names,
                )
                for s in pcfg.selectors
            ],
        ))

    rate_limiter = _build_rate_limiter(config)
    approval_store = _build_approval_store(config)
    webhooks = _build_webhook_dispatcher(config, audit)
    approval_broker = _build_approval(config, approval_store, audit, webhooks)
    policy = PolicyEngine(
        rate_limiter=rate_limiter,
        approval=approval_broker,
        audit=audit,
        block_dangerous_without_approval=config.policy.block_dangerous_without_approval,
        metrics=metrics,
    )
    service = GatewayService(
        catalog=catalog,
        publishing=publishing,
        sessions=sessions,
        adapters=adapters,
        policy=policy,
        audit=audit,
        bus=bus,
        profiles=profile_registry,
        payload=PayloadOptions(**config.payload.model_dump()),
        output_filter=_build_output_filter(config.output),
        approval_store=approval_store,
    )
    return RuntimeBundle(
        config=config,
        catalog=catalog,
        adapters=adapters,
        publishing=publishing,
        profiles=profile_registry,
        policy=policy,
        service=service,
        output_filter=_build_output_filter(config.output),
        rate_limiter=rate_limiter,
    )


def _build_oauth_service(audit: AuditLogger) -> UpstreamOAuthService:
    """Construct the shared upstream OAuth service + encrypted credential store.

    The same instance backs both the ``/admin/oauth/*`` routes (mint/refresh) and
    the adapter-side :class:`OAuthAuthHeaderProvider` (token injection), so a
    token saved by the flow is immediately visible to upstream requests.
    """
    key_material = os.environ.get("CONCIERGE_CREDENTIAL_KEY", "concierge-dev-credential-key")
    backend = InMemoryCredentialStore(key=resolve_fernet_key(key_material))
    credentials = UpstreamCredentialStore(backend=backend)
    return UpstreamOAuthService(
        credential_store=credentials,
        pending=OAuthPendingStore(),
        audit=audit,
    )


def _build_oauth_deps(
    *,
    auth: AuthProvider,
    audit: AuditLogger,
    config: GatewayConfig,
    oauth: UpstreamOAuthService | None = None,
) -> AdminOAuthDeps:
    """Wire upstream OAuth admin routes (P2-ADMIN-7) for the running gateway."""
    oauth = oauth or _build_oauth_service(audit)
    gw = config.gateway
    host = gw.host if gw.host not in ("0.0.0.0", "::") else "127.0.0.1"  # nosec B104
    public_base_url = f"http://{host}:{gw.port}"
    return AdminOAuthDeps(
        auth=auth,
        oauth=oauth,
        credentials=oauth.credentials,
        audit=audit,
        public_base_url=public_base_url,
    )


def _include_admin_routers(
    app: FastAPI,
    *,
    auth: AuthProvider,
    catalog: Catalog,
    adapters: AdapterManager,
    sessions: SessionManager,
    service: GatewayService,
    revocation: RevocationStore,
    tenant_tokens: TenantTokenStore,
    approval_broker: object,
    approval_store: ApprovalStore,
    operator_subjects: list[str],
    config_store: SqliteConfigStore,
    reload_coordinator: ReloadCoordinator,
    oauth_deps: AdminOAuthDeps | None = None,
    extra_routers: list[APIRouter] | None = None,
) -> None:
    """Mount admin sub-routers (config, oauth, future upstreams/profiles)."""
    app.include_router(build_admin_router(
        auth=auth,
        catalog=catalog,
        adapters=adapters,
        sessions=sessions,
        service=service,
        revocation=revocation,
        tenant_tokens=tenant_tokens,
        approval_broker=approval_broker,
        approval_store=approval_store,
        operator_subjects=operator_subjects,
        config_store=config_store,
        reload_coordinator=reload_coordinator,
    ))
    if oauth_deps is not None:
        app.include_router(build_admin_oauth_router(oauth_deps))
    for router in extra_routers or []:
        app.include_router(router)


def _build_rate_limiter(cfg: GatewayConfig) -> RateLimiter:
    """Select the rate-limiter backend (P1-4), same pattern as catalog/session.

    Default quota comes from the legacy ``policy.rate_limit_*`` fields; per-tenant
    overrides layer on top via ``policy.ratelimit.tenant_quotas``. Redis is used
    only when explicitly selected *and* a URL is resolvable (its own or the
    shared ``storage.redis_url`` from P1-2); otherwise we fall back to in-memory.
    """
    policy = cfg.policy
    rl = policy.ratelimit
    overrides = {
        tenant: TenantQuota(q.capacity, q.refill_per_sec)
        for tenant, q in rl.tenant_quotas.items()
    }
    if rl.backend == "redis":
        redis_url = rl.redis_url or cfg.storage.redis_url
        if not redis_url:
            raise ValueError(
                "policy.ratelimit.redis_url (or storage.redis_url) is required "
                "when policy.ratelimit.backend=redis"
            )
        return RedisTokenBucketRateLimiter(
            redis_url,
            default_capacity=policy.rate_limit_capacity,
            default_refill=policy.rate_limit_refill_per_sec,
            tenant_overrides=overrides,
        )
    return InMemoryTokenBucketRateLimiter(
        default_capacity=policy.rate_limit_capacity,
        default_refill=policy.rate_limit_refill_per_sec,
        tenant_overrides=overrides,
    )


def build_app(config: GatewayConfig) -> FastAPI:
    configure_logging(config.log_level)

    # Startup-restore: open the config store early so dynamic fields (upstreams,
    # profiles, policy …) applied via the admin panel survive process restarts.
    # Infra fields (auth, gateway host/port, storage) always come from the YAML.
    config_store = SqliteConfigStore(_runtime_config_db_path(config.storage))
    _stored = config_store.get_active_config_sync()
    if _stored is not None:
        from ..admin.config_store import overlay_dynamic_from_store
        _merged = overlay_dynamic_from_store(config.model_dump(mode="json"), _stored)
        try:
            config = GatewayConfig.model_validate(_merged)
            _log.info(
                "startup-restore: %d upstream(s), %d profile(s) from config store",
                len(config.upstream_servers),
                len(config.profiles),
            )
        except Exception as _exc:  # noqa: BLE001
            _log.warning("startup-restore: stored config invalid, falling back to YAML: %s", _exc)

    # Core subsystems — sessions built first so it can be passed into PublishingService
    # at construction time (avoids post-hoc attribute assignment).
    sessions = _build_session_manager(config)
    sessions.idle_ttl = config.session_pool.idle_ttl_s
    catalog = _build_catalog_store(config.storage)
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus, sessions=sessions)
    metrics = MetricRegistry()
    audit_sinks: list[Any] = [MetricAuditSink(metrics)]
    if config.observability.audit_http_sink_url:
        audit_sinks.append(HttpAuditSink(
            config.observability.audit_http_sink_url,
            timeout_s=config.observability.audit_http_timeout_s,
        ))
    audit = AuditLogger(sinks=audit_sinks)

    # Shared upstream OAuth service: backs both the admin OAuth routes and the
    # adapter-side token injection so a token minted via the flow is sent on
    # upstream requests (and refreshed on expiry).
    oauth_service = _build_oauth_service(audit)
    auth_header_provider = OAuthAuthHeaderProvider(oauth_service)

    adapters = AdapterManager(
        catalog,
        refresh_interval_s=config.catalog_refresh_interval_s,
        max_upstream_sessions=config.session_pool.max_upstream_sessions,
        audit=audit,
    )

    # Tearing down a router session also tears down its pooled upstream sessions,
    # and records the eviction (and how many upstream sessions it freed).
    async def _on_session_evict(session_id: str) -> None:
        freed = await adapters.evict_router_session(session_id)
        audit.session_evicted(session_id, upstream_sessions_freed=freed)

    sessions._on_evict = _on_session_evict
    for srv in config.upstream_servers:
        adapter = _wrap_cache(
            _build_adapter(srv, auth_header_provider=auth_header_provider), config.cache
        )

        def adapter_factory(s: UpstreamServerConfig = srv) -> UpstreamAdapter:
            return _wrap_cache(
                _build_adapter(s, auth_header_provider=auth_header_provider), config.cache
            )

        adapters.register(
            adapter,
            default_risk=srv.default_risk,
            default_tags=srv.default_tags,
            default_categories=srv.default_categories,
            requires_auth=srv.requires_auth,
            requires_approval_for=srv.requires_approval_for,
            isolation=srv.isolation,
            factory=adapter_factory,
            connect_max_retries=srv.connect_max_retries,
            connect_backoff_base_s=srv.connect_backoff_base_s,
            connect_backoff_max_s=srv.connect_backoff_max_s,
        )

    # Profiles
    profile_registry = ProfileRegistry()
    for pcfg in config.profiles:
        profile_registry.register(Profile(
            name=pcfg.name,
            description=pcfg.description,
            auto_apply=pcfg.auto_apply,
            selectors=[
                ProfileSelector(
                    server=s.server,
                    tags=s.tags,
                    categories=s.categories,
                    primitive_type=None if s.primitive_type is None else
                        PrimitiveType(s.primitive_type),
                    names=s.names,
                )
                for s in pcfg.selectors
            ],
        ))

    # Policy
    rate_limiter = _build_rate_limiter(config)
    approval_store = _build_approval_store(config)
    webhooks = _build_webhook_dispatcher(config, audit)
    approval_broker = _build_approval(config, approval_store, audit, webhooks)
    policy = PolicyEngine(
        rate_limiter=rate_limiter,
        approval=approval_broker,
        audit=audit,
        block_dangerous_without_approval=config.policy.block_dangerous_without_approval,
        metrics=metrics,
    )

    service = GatewayService(
        catalog=catalog,
        publishing=publishing,
        sessions=sessions,
        adapters=adapters,
        policy=policy,
        audit=audit,
        bus=bus,
        profiles=profile_registry,
        payload=PayloadOptions(**config.payload.model_dump()),
        output_filter=_build_output_filter(config.output),
        approval_store=approval_store,
    )
    revocation = _build_revocation_store(config)
    tenant_tokens = _build_tenant_token_store(config)
    auth = _build_auth(config, revocation, tenant_tokens)
    drain = DrainController()
    async def _session_gc_loop() -> None:
        interval = config.session_pool.gc_interval_s
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    n = await sessions.gc()
                    if n:
                        _log.info("session gc evicted %d idle session(s)", n)
                except Exception as e:  # noqa: BLE001
                    _log.warning("session gc failed: %s", e)
        except asyncio.CancelledError:
            return

    def _install_sigterm_drain() -> None:
        """Flip the drain flag the instant SIGTERM arrives (P1-7).

        uvicorn also catches SIGTERM and begins its own shutdown, but it first
        stops accepting connections and only then runs lifespan shutdown. By
        flipping ``drain`` here as well, /readyz reports not-ready immediately —
        before the socket closes — so a k8s preStop / endpoints update can steer
        new traffic away while this replica finishes in-flight work. Best-effort:
        on platforms without add_signal_handler (e.g. Windows) we simply rely on
        the lifespan-shutdown path below.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(
                signal.SIGTERM,
                lambda: asyncio.ensure_future(drain.begin_drain()),
            )
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            pass

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _log.info("starting upstream adapters: %d configured", len(adapters.all()))
        await adapters.start_all()
        _install_sigterm_drain()
        gc_task = asyncio.create_task(_session_gc_loop(), name="session-gc")
        try:
            yield
        finally:
            # Graceful drain: stop admitting new sessions and let in-flight calls
            # finish before we tear down adapters/stores (P1-7). begin_drain is
            # idempotent — the SIGTERM handler above may have already set it.
            await drain.begin_drain()
            grace = config.session_pool.drain_grace_period_s
            if drain.in_flight:
                _log.info("draining: waiting for %d in-flight call(s)", drain.in_flight)
            drained = await drain.wait_for_idle(grace)
            if not drained:
                _log.warning(
                    "drain grace period (%.1fs) elapsed with %d call(s) still in flight",
                    grace, drain.in_flight,
                )
            gc_task.cancel()
            with suppress(asyncio.CancelledError):
                await gc_task
            await adapters.stop_all()
            # Close persistent stores on shutdown (P1-2)
            if hasattr(catalog.store, "close"):
                try:
                    close_fn = catalog.store.close
                    if asyncio.iscoroutinefunction(close_fn):
                        await close_fn()
                    else:
                        close_fn()
                except Exception as e:  # noqa: BLE001
                    _log.warning("catalog store close failed: %s", e)
            if hasattr(sessions, "close_redis"):
                try:
                    await sessions.close_redis()  # type: ignore[attr-defined]
                except Exception as e:  # noqa: BLE001
                    _log.warning("redis session store close failed: %s", e)
            # Close the rate-limiter backend (Redis pool, if any) (P1-4).
            try:
                await rate_limiter.aclose()
            except Exception as e:  # noqa: BLE001
                _log.warning("rate limiter close failed: %s", e)
            # Close the auth stores (revocation list + tenant tokens) (P1-1) and
            # the approval queue store (P1-3).
            for store, label in (
                (revocation, "revocation"),
                (tenant_tokens, "tenant-token"),
                (approval_store, "approval"),
            ):
                try:
                    await store.aclose()
                except Exception as e:  # noqa: BLE001
                    _log.warning("%s store close failed: %s", label, e)
            for sink in audit_sinks:
                close_fn = getattr(sink, "close", None)
                if close_fn is not None:
                    try:
                        close_fn()
                    except Exception as e:  # noqa: BLE001
                        _log.warning("audit sink close failed: %s", e)
            try:
                config_store.close()
            except Exception as e:  # noqa: BLE001
                _log.warning("config store close failed: %s", e)

    app = FastAPI(title="Concierge", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def _record_metrics(request, call_next):  # type: ignore[no-untyped-def]
        return await record_request_metrics(request, call_next, metrics)

    # Map GatewayError subclasses to clean JSON-RPC-shaped responses with the
    # correct HTTP status, instead of bubbling them up as unhandled exceptions
    # (which uvicorn logs as a multi-line traceback per request — noise on every
    # unauthenticated browser probe like /favicon.ico).
    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError):  # type: ignore[no-untyped-def]
        # Unauthorized is a GatewayError subclass but has its own handler below;
        # Starlette resolves by MRO specificity, so it never reaches here. This
        # handler only sees the genuine error subclasses (Forbidden, RateLimited,
        # upstream failures, ...), which warrant a warning.
        _log.warning(
            "gateway error on %s %s: code=%d message=%s",
            request.method, request.url.path, exc.code, exc.message,
        )
        return JSONResponse(
            status_code=_HTTP_STATUS_FOR_GATEWAY_CODE.get(exc.code, 500),
            content={"error": exc.to_jsonrpc()},
        )

    # Only advertise a Bearer challenge when a bearer-style credential is
    # actually accepted. The provider chain (or the legacy ``type``) may select
    # localhost / mTLS / OIDC, for which ``WWW-Authenticate: Bearer`` is wrong
    # and can mislead clients.
    if config.auth.providers:
        _active_auth = [str(p) for p in config.auth.providers]
    else:
        _active_auth = [str(config.auth.type)]
    _bearer_challenge = any(p in ("bearer", "tenant_token") for p in _active_auth)

    @app.exception_handler(Unauthorized)
    async def _unauthorized_handler(request: Request, exc: Unauthorized):  # type: ignore[no-untyped-def]
        # Auth failures are routine (missing/expired tokens, browser probes) and
        # should never emit a stack trace. Log a single line; return a clean 401
        # with a WWW-Authenticate hint only when a Bearer scheme is in play.
        _log.info(
            "unauthorized on %s %s from %s: %s",
            request.method,
            request.url.path,
            request.client.host if request.client else "?",
            exc.message,
        )
        headers = {"WWW-Authenticate": "Bearer"} if _bearer_challenge else None
        return JSONResponse(
            status_code=401,
            content={"error": exc.to_jsonrpc()},
            headers=headers,
        )

    # Enforce the Origin allow-list globally, ahead of every route handler, so no
    # MCP route (POST/GET/DELETE) can be reached with an untrusted browser Origin
    # — a structural guarantee independent of any per-handler check (P0-1).
    app.add_middleware(
        OriginAllowlistMiddleware,
        allowed_origins=config.gateway.allowed_origins,
        guarded_paths=[config.gateway.path],
    )
    app.include_router(build_facade_router(
        service=service,
        sessions=sessions,
        bus=bus,
        auth=auth,
        allowed_origins=config.gateway.allowed_origins,
        path=config.gateway.path,
        drain=drain,
    ))

    async def _build_runtime(cfg: GatewayConfig) -> RuntimeBundle:
        return _assemble_runtime_bundle(
            cfg,
            sessions=sessions,
            bus=bus,
            audit=audit,
            metrics=metrics,
            on_session_evict=_on_session_evict,
            auth_header_provider=auth_header_provider,
        )

    reload_coordinator = ReloadCoordinator(
        audit=audit,
        build_runtime=_build_runtime,
        swap_target=_AppRuntimeSwap(app),
    )
    upstreams_router = build_admin_upstreams_router(
        AdminUpstreamsDeps(
            auth=auth,
            config_store=config_store,
            adapters=adapters,
            reload_coordinator=reload_coordinator,
        )
    )
    profiles_router = build_admin_profiles_router(
        AdminProfilesDeps(
            auth=auth,
            config_store=config_store,
            catalog=catalog,
            reload_coordinator=reload_coordinator,
        )
    )
    oauth_deps = _build_oauth_deps(auth=auth, audit=audit, config=config, oauth=oauth_service)
    _include_admin_routers(
        app,
        auth=auth,
        catalog=catalog,
        adapters=adapters,
        sessions=sessions,
        service=service,
        revocation=revocation,
        tenant_tokens=tenant_tokens,
        approval_broker=approval_broker,
        approval_store=approval_store,
        operator_subjects=config.policy.approval.operator_subjects,
        config_store=config_store,
        reload_coordinator=reload_coordinator,
        oauth_deps=oauth_deps,
        extra_routers=[upstreams_router, profiles_router],
    )
    app.include_router(build_observability_router(
        metrics=metrics,
        adapters=adapters,
        sessions=sessions,
        bus=bus,
        metrics_path=config.observability.metrics_path,
        health_path=config.observability.health_path,
        ready_path=config.observability.ready_path,
        drain=drain,
    ))
    install_admin_ui(app)

    # Expose for tests / programmatic access.
    app.state.catalog = catalog
    app.state.sessions = sessions
    app.state.publishing = publishing
    app.state.adapters = adapters
    app.state.service = service
    app.state.bus = bus
    app.state.profiles = profile_registry
    app.state.metrics = metrics
    app.state.drain = drain
    app.state.revocation = revocation
    app.state.tenant_tokens = tenant_tokens
    app.state.approval_store = approval_store
    app.state.approval_broker = approval_broker
    app.state.config_store = config_store
    app.state.reload_coordinator = reload_coordinator
    app.state.rate_limiter = rate_limiter
    return app
