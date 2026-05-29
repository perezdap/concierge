"""
FastAPI application factory.

Wires every subsystem together from a GatewayConfig and returns a runnable app.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..adapters.custom import build_custom_adapter
from ..adapters.manager import AdapterManager
from ..adapters.sse_legacy import LegacySseAdapter
from ..adapters.stdio import StdioAdapter
from ..adapters.streamable_http import StreamableHttpAdapter
from ..config import GatewayConfig, UpstreamServerConfig
from ..core.catalog import Catalog, InMemoryCatalogStore
from ..core.notifications import NotificationBus
from ..core.publishing import PublishingService
from ..core.session import SessionManager
from ..gateway.profiles import Profile, ProfileRegistry, ProfileSelector
from ..gateway.service import GatewayService
from ..policy.approval import DenyByDefaultApprovalBroker
from ..policy.engine import PolicyEngine
from ..policy.ratelimit import TokenBucketRateLimiter
from ..util.audit import AuditLogger
from ..util.log import configure_logging, get_logger
from .admin import build_admin_router
from .auth import AuthProvider, LocalhostAllowAuth, NoAuth, StaticBearerAuth
from .facade import build_facade_router


_log = get_logger("concierge.app")


def _build_adapter(cfg: UpstreamServerConfig):
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


def _build_auth(cfg) -> AuthProvider:
    if cfg.auth.type == "none":
        return NoAuth()
    if cfg.auth.type == "bearer":
        return StaticBearerAuth(cfg.auth.bearer_tokens)
    return LocalhostAllowAuth()


def build_app(config: GatewayConfig) -> FastAPI:
    configure_logging(config.log_level)

    # Core subsystems
    catalog = Catalog(InMemoryCatalogStore())
    bus = NotificationBus()
    sessions = SessionManager()
    publishing = PublishingService(catalog, bus)
    audit = AuditLogger()

    adapters = AdapterManager(
        catalog,
        refresh_interval_s=config.catalog_refresh_interval_s,
    )
    for srv in config.upstream_servers:
        adapter = _build_adapter(srv)
        adapters.register(
            adapter,
            default_risk=srv.default_risk,
            default_tags=srv.default_tags,
            default_categories=srv.default_categories,
            requires_auth=srv.requires_auth,
            requires_approval_for=srv.requires_approval_for,
        )

    # Profiles
    profile_registry = ProfileRegistry()
    for pcfg in config.profiles:
        profile_registry.register(Profile(
            name=pcfg.name,
            description=pcfg.description,
            selectors=[
                ProfileSelector(
                    server=s.server,
                    tags=s.tags,
                    categories=s.categories,
                    primitive_type=None if s.primitive_type is None else
                        __import__("concierge.core.types", fromlist=["PrimitiveType"]).PrimitiveType(s.primitive_type),
                    names=s.names,
                )
                for s in pcfg.selectors
            ],
        ))

    # Policy
    rate_limiter = TokenBucketRateLimiter(
        default_capacity=config.policy.rate_limit_capacity,
        default_refill=config.policy.rate_limit_refill_per_sec,
    )
    policy = PolicyEngine(
        rate_limiter=rate_limiter,
        approval=DenyByDefaultApprovalBroker(),
        audit=audit,
        block_dangerous_without_approval=config.policy.block_dangerous_without_approval,
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
    )
    auth = _build_auth(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _log.info("starting upstream adapters: %d configured", len(adapters.all()))
        await adapters.start_all()
        try:
            yield
        finally:
            await adapters.stop_all()

    app = FastAPI(title="Concierge", version="0.1.0", lifespan=lifespan)
    app.include_router(build_facade_router(
        service=service,
        sessions=sessions,
        bus=bus,
        auth=auth,
        allowed_origins=config.gateway.allowed_origins,
        path=config.gateway.path,
    ))
    app.include_router(build_admin_router(
        auth=auth,
        catalog=catalog,
        adapters=adapters,
        sessions=sessions,
        service=service,
    ))

    # Expose for tests / programmatic access.
    app.state.catalog = catalog
    app.state.sessions = sessions
    app.state.publishing = publishing
    app.state.adapters = adapters
    app.state.service = service
    app.state.bus = bus
    app.state.profiles = profile_registry
    return app
