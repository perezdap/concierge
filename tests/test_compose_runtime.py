"""Unit tests for compose_runtime — verifies wiring without standing up HTTP."""
from __future__ import annotations

import pytest

from concierge.config import GatewayConfig
from concierge.core.notifications import NotificationBus
from concierge.core.session import SessionManager
from concierge.observability import MetricRegistry
from concierge.server.app import compose_runtime
from concierge.util.audit import AuditLogger


def _minimal_config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "gateway": {"host": "127.0.0.1", "port": 8765},
            "auth": {"type": "none"},
            "upstream_servers": [],
            "profiles": [],
        }
    )


async def _noop_evict(session_id: str) -> None:  # pragma: no cover
    pass


@pytest.fixture()
def bundle():
    config = _minimal_config()
    sessions = SessionManager()
    bus = NotificationBus()
    audit = AuditLogger(sinks=[])
    metrics = MetricRegistry()
    return compose_runtime(
        config,
        sessions=sessions,
        bus=bus,
        audit=audit,
        metrics=metrics,
        on_session_evict=_noop_evict,
    )


def test_bundle_has_all_required_fields(bundle):
    """compose_runtime returns a RuntimeBundle with every wired subsystem."""
    assert bundle.catalog is not None
    assert bundle.adapters is not None
    assert bundle.publishing is not None
    assert bundle.profiles is not None
    assert bundle.policy is not None
    assert bundle.service is not None
    assert bundle.rate_limiter is not None


def test_service_references_bundle_catalog(bundle):
    """GatewayService.catalog is the same object as RuntimeBundle.catalog."""
    assert bundle.service.catalog is bundle.catalog


def test_service_references_bundle_adapters(bundle):
    """GatewayService.adapters is the same object as RuntimeBundle.adapters."""
    assert bundle.service.adapters is bundle.adapters


def test_service_references_bundle_profiles(bundle):
    """GatewayService.profiles is the same object as RuntimeBundle.profiles."""
    assert bundle.service.profiles is bundle.profiles


def test_service_references_bundle_policy(bundle):
    """GatewayService.policy is the same object as RuntimeBundle.policy."""
    assert bundle.service.policy is bundle.policy


def test_two_calls_produce_independent_bundles():
    """Each compose_runtime call returns a fresh, independent RuntimeBundle."""
    config = _minimal_config()
    sessions = SessionManager()
    bus = NotificationBus()
    audit = AuditLogger(sinks=[])
    metrics = MetricRegistry()
    kwargs = dict(
        sessions=sessions,
        bus=bus,
        audit=audit,
        metrics=metrics,
        on_session_evict=_noop_evict,
    )
    b1 = compose_runtime(config, **kwargs)
    b2 = compose_runtime(config, **kwargs)
    assert b1.catalog is not b2.catalog
    assert b1.adapters is not b2.adapters
    assert b1.service is not b2.service


def test_swap_into_updates_state(bundle):
    """RuntimeBundle.swap_into() updates an attribute holder in-place."""

    class FakeState:
        catalog = None
        adapters = None
        publishing = None
        profiles = None
        service = None
        rate_limiter = None

    state = FakeState()
    # First swap: no existing objects — should populate from bundle.
    bundle.swap_into(state)
    assert state.catalog is bundle.catalog
    assert state.adapters is bundle.adapters
    assert state.publishing is bundle.publishing
    assert state.profiles is bundle.profiles
    assert state.service is bundle.service
    assert state.rate_limiter is bundle.rate_limiter
