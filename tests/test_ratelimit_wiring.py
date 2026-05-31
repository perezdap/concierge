"""Wiring tests for P1-4: error envelope, metrics, config-selectable backend."""
from __future__ import annotations

import pytest

from concierge.config import (
    GatewayConfig,
    PolicyConfig,
    RateLimitConfig,
    StorageConfig,
    TenantQuotaConfig,
)
from concierge.core.session import SessionManager
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType
from concierge.errors import GW_RATE_LIMITED, RateLimited
from concierge.observability import MetricRegistry
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import (
    InMemoryTokenBucketRateLimiter,
    RedisTokenBucketRateLimiter,
)
from concierge.server.app import _build_rate_limiter
from concierge.util.audit import AuditLogger


def _entry(**kwargs) -> CatalogEntry:
    defaults = dict(
        server_id="demo",
        upstream_name="alpha",
        canonical_name="demo__alpha",
        primitive_type=PrimitiveType.TOOL,
        transport=TransportType.STDIO,
        display_label="Alpha",
        short_description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        requires_auth=False,
    )
    defaults.update(kwargs)
    return CatalogEntry(**defaults)


def test_rate_limited_carries_retry_after_in_data():
    err = RateLimited.with_retry_after("slow down", 2.3)
    assert err.code == GW_RATE_LIMITED
    # Rounded up to whole seconds for HTTP Retry-After semantics.
    assert err.data == {"retry_after": 3}
    rpc = err.to_jsonrpc()
    assert rpc["code"] == GW_RATE_LIMITED
    assert rpc["data"]["retry_after"] == 3


def test_rate_limited_infinite_retry_after_is_finite_serializable():
    err = RateLimited.with_retry_after("never", float("inf"))
    assert err.data["retry_after"] == 86400


@pytest.mark.asyncio
async def test_engine_emits_metrics_and_retry_after():
    metrics = MetricRegistry()
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=1, default_refill=2.0)
    engine = PolicyEngine(limiter, DenyByDefaultApprovalBroker(), AuditLogger(), metrics=metrics)
    session = await SessionManager().create(tenant_id="acme")
    entry = _entry()

    await engine.authorize_call(session, entry, {})  # allowed
    with pytest.raises(RateLimited) as ei:
        await engine.authorize_call(session, entry, {})  # denied
    assert ei.value.data["retry_after"] >= 1

    rendered = metrics.render(upstream_health=[], active_sessions=0, queue_depths={})
    assert "concierge_ratelimit_allowed_total" in rendered
    assert "concierge_ratelimit_denied_total" in rendered
    assert 'tenant="acme"' in rendered
    assert 'tool="demo__alpha"' in rendered


def _cfg(rl: RateLimitConfig, storage: StorageConfig | None = None) -> GatewayConfig:
    return GatewayConfig(
        policy=PolicyConfig(rate_limit_capacity=5, rate_limit_refill_per_sec=1.0, ratelimit=rl),
        storage=storage or StorageConfig(),
    )


def test_factory_defaults_to_in_memory():
    limiter = _build_rate_limiter(_cfg(RateLimitConfig()))
    assert isinstance(limiter, InMemoryTokenBucketRateLimiter)


def test_factory_in_memory_applies_tenant_overrides():
    rl = RateLimitConfig(tenant_quotas={"vip": TenantQuotaConfig(capacity=99, refill_per_sec=1)})
    limiter = _build_rate_limiter(_cfg(rl))
    assert isinstance(limiter, InMemoryTokenBucketRateLimiter)
    assert limiter._quotas.quota_for("vip").capacity == 99
    assert limiter._quotas.quota_for("other").capacity == 5  # default


def test_factory_redis_requires_url():
    with pytest.raises(ValueError, match="redis_url"):
        _build_rate_limiter(_cfg(RateLimitConfig(backend="redis")))


def test_factory_redis_falls_back_to_storage_redis_url():
    rl = RateLimitConfig(backend="redis")
    storage = StorageConfig(redis_url="redis://localhost:6379/0")
    limiter = _build_rate_limiter(_cfg(rl, storage))
    assert isinstance(limiter, RedisTokenBucketRateLimiter)
