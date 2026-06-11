"""Unit tests for compose_runtime — verifies wiring without standing up HTTP."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest

from concierge.admin.reload import SWAPPABLE_SERVICE_DEPS, RuntimeBundle
from concierge.config import GatewayConfig
from concierge.core.notifications import NotificationBus
from concierge.core.session import SessionManager
from concierge.gateway.service import GatewayService
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


def test_swappable_service_deps_match_gateway_service_init():
    """SWAPPABLE_SERVICE_DEPS covers every config-scoped GatewayService field."""
    sig = inspect.signature(GatewayService.__init__)
    init_params = {
        name
        for name in sig.parameters
        if name != "self" and sig.parameters[name].kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    persistent = {"sessions", "audit", "bus"}
    config_scoped = init_params - persistent
    assert set(SWAPPABLE_SERVICE_DEPS) == config_scoped


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


def test_swap_into_updates_empty_state(bundle):
    """RuntimeBundle.swap_into() populates an empty attribute holder."""

    class FakeState:
        catalog = None
        adapters = None
        publishing = None
        profiles = None
        service = None
        rate_limiter = None

    state = FakeState()
    bundle.swap_into(state)
    assert state.catalog is bundle.catalog
    assert state.adapters is bundle.adapters
    assert state.publishing is bundle.publishing
    assert state.profiles is bundle.profiles
    assert state.service is bundle.service
    assert state.rate_limiter is bundle.rate_limiter


@dataclass
class _FakeStore:
    data: str = "v1"


@dataclass
class _FakeCatalog:
    store: _FakeStore = field(default_factory=_FakeStore)


@dataclass
class _FakeAdapters:
    marker: str = "live"
    stop_calls: int = 0

    async def start_all(self) -> None:
        return None

    async def stop_all(self) -> None:
        self.stop_calls += 1

    def all(self) -> list[Any]:
        return []


@dataclass
class _FakeProfiles:
    marker: str = "live"


@dataclass
class _FakeService:
    catalog: Any = None
    publishing: Any = None
    adapters: Any = None
    policy: Any = None
    profiles: Any = None
    payload: Any = None
    output_filter: Any = None
    approval_store: Any = None


def _candidate_bundle(*, store_data: str, adapter_marker: str) -> RuntimeBundle:
    config = _minimal_config()
    catalog = _FakeCatalog(store=_FakeStore(data=store_data))
    adapters = _FakeAdapters(marker=adapter_marker)
    profiles = _FakeProfiles(marker=adapter_marker)
    service = _FakeService(
        catalog=catalog,
        publishing=object(),
        adapters=adapters,
        policy=object(),
        profiles=profiles,
    )
    return RuntimeBundle(
        config=config,
        catalog=catalog,
        adapters=adapters,
        publishing=object(),
        profiles=profiles,
        policy=object(),
        service=service,
        rate_limiter=object(),
    )


def test_swap_into_preserves_live_object_identity():
    """Hot-reload swap keeps catalog/adapters/profiles/service identity on state."""
    live_catalog = _FakeCatalog(store=_FakeStore(data="live"))
    live_adapters = _FakeAdapters(marker="live")
    live_profiles = _FakeProfiles(marker="live")
    live_service = _FakeService(
        catalog=live_catalog,
        publishing=object(),
        adapters=live_adapters,
        policy=object(),
        profiles=live_profiles,
    )

    class LiveState:
        catalog = live_catalog
        adapters = live_adapters
        publishing = object()
        profiles = live_profiles
        service = live_service
        rate_limiter = object()

    state = LiveState()
    candidate = _candidate_bundle(store_data="next", adapter_marker="next")
    candidate.swap_into(state)

    assert state.catalog is live_catalog
    assert state.adapters is live_adapters
    assert state.profiles is live_profiles
    assert state.service is live_service
    assert live_catalog.store.data == "next"
    assert live_adapters.marker == "next"
    assert live_profiles.marker == "next"
    assert candidate.adapters is not live_adapters
    assert candidate.profiles is not live_profiles


@pytest.mark.asyncio
async def test_second_apply_does_not_stop_live_adapters():
    """Stopping a promoted bundle must not stop adapters still mounted on state."""
    live_adapters = _FakeAdapters(marker="live")
    live_service = _FakeService(adapters=live_adapters)

    class LiveState:
        catalog = _FakeCatalog()
        adapters = live_adapters
        publishing = object()
        profiles = _FakeProfiles()
        service = live_service
        rate_limiter = object()

    state = LiveState()
    first = _candidate_bundle(store_data="v1", adapter_marker="v1")
    second = _candidate_bundle(store_data="v2", adapter_marker="v2")

    first.swap_into(state)
    second.swap_into(state)
    await first.stop()
    assert live_adapters.stop_calls == 0
    assert first.adapters.stop_calls == 1
    assert second.adapters.stop_calls == 0
