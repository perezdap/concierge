"""
FastAPI application factory.

Wires every subsystem together from a GatewayConfig and returns a runnable app.
"""
from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI

from ..adapters.base import UpstreamAdapter
from ..adapters.caching import CachingAdapter
from ..adapters.custom import build_custom_adapter
from ..adapters.manager import AdapterManager
from ..adapters.sse_legacy import LegacySseAdapter
from ..adapters.stdio import StdioAdapter
from ..adapters.streamable_http import StreamableHttpAdapter
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
)
from ..policy.engine import PolicyEngine
from ..policy.ratelimit import (
    InMemoryTokenBucketRateLimiter,
    RateLimiter,
    RedisTokenBucketRateLimiter,
    TenantQuota,
)
from ..util.audit import AuditLogger
from ..util.log import configure_logging, get_logger
from ..util.output_filter import ContentTypeFilter, LengthCapper, OutputFilter, SecretRedactor
from ..util.payload import PayloadOptions
from .admin import build_admin_router
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


def _build_adapter(cfg: UpstreamServerConfig) -> UpstreamAdapter:
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


def _build_approval(cfg: GatewayConfig) -> ApprovalBroker:
    if cfg.policy.approval_mode == "allow_list":
        return AllowListApprovalBroker(cfg.policy.approval_allow_list)
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

    # Core subsystems
    catalog = _build_catalog_store(config.storage)
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus)
    metrics = MetricRegistry()
    audit_sinks: list[Any] = [MetricAuditSink(metrics)]
    if config.observability.audit_http_sink_url:
        audit_sinks.append(HttpAuditSink(
            config.observability.audit_http_sink_url,
            timeout_s=config.observability.audit_http_timeout_s,
        ))
    audit = AuditLogger(sinks=audit_sinks)

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

    sessions = _build_session_manager(config)
    sessions._on_evict = _on_session_evict
    sessions.idle_ttl = config.session_pool.idle_ttl_s
    for srv in config.upstream_servers:
        adapter = _wrap_cache(_build_adapter(srv), config.cache)

        def adapter_factory(s: UpstreamServerConfig = srv) -> UpstreamAdapter:
            return _wrap_cache(_build_adapter(s), config.cache)

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
    policy = PolicyEngine(
        rate_limiter=rate_limiter,
        approval=_build_approval(config),
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
            # Close the auth stores (revocation list + tenant tokens) (P1-1).
            for store, label in ((revocation, "revocation"), (tenant_tokens, "tenant-token")):
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

    app = FastAPI(title="Concierge", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def _record_metrics(request, call_next):  # type: ignore[no-untyped-def]
        return await record_request_metrics(request, call_next, metrics)

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
    app.include_router(build_admin_router(
        auth=auth,
        catalog=catalog,
        adapters=adapters,
        sessions=sessions,
        service=service,
        revocation=revocation,
        tenant_tokens=tenant_tokens,
    ))
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
    return app
