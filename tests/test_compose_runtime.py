"""Unit tests for compose_runtime() and RuntimeBundle.swap_into().

These tests verify:
1. compose_runtime() builds a fully wired RuntimeBundle without standing up HTTP.
2. RuntimeBundle.swap_into() correctly updates in-place state handles.
3. The swap is driven by the bundle's own __dict__, so a new GatewayService attribute
   would be swapped automatically (no hardcoded attr list can silently drop it).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from concierge.admin.reload import _SERVICE_STABLE_ATTRS, RuntimeBundle  # noqa: PLC2701
from concierge.config import GatewayConfig
from concierge.core.catalog import Catalog, InMemoryCatalogStore
from concierge.core.notifications import NotificationBus
from concierge.core.session import SessionManager
from concierge.gateway.profiles import ProfileRegistry
from concierge.gateway.service import GatewayService
from concierge.observability import MetricRegistry
from concierge.server.app import compose_runtime
from concierge.util.audit import AuditLogger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _noop_evict(_session_id: str) -> None:
    pass


def _minimal_config(**kwargs: Any) -> GatewayConfig:
    return GatewayConfig(**kwargs)


def _build_bundle(config: GatewayConfig | None = None) -> RuntimeBundle:
    """Build a RuntimeBundle without standing up HTTP."""
    cfg = config or _minimal_config()
    sessions = SessionManager()
    bus = NotificationBus()
    audit = AuditLogger()
    metrics = MetricRegistry()
    return compose_runtime(
        cfg,
        sessions=sessions,
        bus=bus,
        audit=audit,
        metrics=metrics,
        on_session_evict=_noop_evict,
    )


# ---------------------------------------------------------------------------
# compose_runtime() wiring tests
# ---------------------------------------------------------------------------

def test_compose_runtime_returns_bundle():
    """compose_runtime produces a RuntimeBundle without building an HTTP app."""
    bundle = _build_bundle()
    assert isinstance(bundle, RuntimeBundle)


def test_compose_runtime_wires_catalog():
    bundle = _build_bundle()
    assert bundle.catalog is not None
    assert hasattr(bundle.catalog, "store")


def test_compose_runtime_wires_adapters():
    bundle = _build_bundle()
    assert bundle.adapters is not None
    assert hasattr(bundle.adapters, "register")


def test_compose_runtime_wires_profiles():
    bundle = _build_bundle()
    assert isinstance(bundle.profiles, ProfileRegistry)


def test_compose_runtime_wires_service():
    bundle = _build_bundle()
    assert isinstance(bundle.service, GatewayService)


def test_compose_runtime_wires_policy():
    bundle = _build_bundle()
    assert bundle.policy is not None


def test_compose_runtime_wires_rate_limiter():
    bundle = _build_bundle()
    assert bundle.rate_limiter is not None


def test_compose_runtime_wires_approval_store():
    bundle = _build_bundle()
    assert bundle.approval_store is not None


def test_compose_runtime_passes_sessions_to_service():
    """sessions is stable — the same object should be in the service."""
    cfg = _minimal_config()
    sessions = SessionManager()
    bus = NotificationBus()
    audit = AuditLogger()
    metrics = MetricRegistry()
    bundle = compose_runtime(
        cfg,
        sessions=sessions,
        bus=bus,
        audit=audit,
        metrics=metrics,
        on_session_evict=_noop_evict,
    )
    assert bundle.service.sessions is sessions


def test_compose_runtime_no_upstreams():
    """Empty upstream_servers config is valid and produces an empty adapter list."""
    cfg = _minimal_config()
    bundle = _build_bundle(cfg)
    assert bundle.adapters.all() == []


def test_compose_runtime_service_references_bundle_catalog():
    """The service's catalog is the same object as bundle.catalog."""
    bundle = _build_bundle()
    assert bundle.service.catalog is bundle.catalog


def test_compose_runtime_service_references_bundle_profiles():
    bundle = _build_bundle()
    assert bundle.service.profiles is bundle.profiles


# ---------------------------------------------------------------------------
# RuntimeBundle.swap_into() tests
# ---------------------------------------------------------------------------

@dataclass
class _FakeState:
    """Minimal stand-in for app.state."""

    catalog: Any = None
    adapters: Any = None
    publishing: Any = None
    profiles: Any = None
    service: Any = None
    rate_limiter: Any = None
    approval_store: Any = None


def test_swap_into_updates_catalog_store_in_place():
    """swap_into replaces catalog.store in-place so existing references stay valid."""
    old_store = InMemoryCatalogStore()
    old_catalog = Catalog(old_store)

    new_bundle = _build_bundle()
    state = _FakeState(
        catalog=old_catalog,
        adapters=new_bundle.adapters,  # reuse to avoid None issues
        publishing=new_bundle.publishing,
        profiles=new_bundle.profiles,
        service=new_bundle.service,
        rate_limiter=new_bundle.rate_limiter,
    )
    new_bundle.swap_into(state)

    # The catalog object identity is preserved; only its store changed.
    assert state.catalog is old_catalog
    assert state.catalog.store is new_bundle.catalog.store


def test_swap_into_updates_profiles_in_place():
    """ProfileRegistry._profiles is replaced in-place."""
    old_registry = ProfileRegistry()
    new_bundle = _build_bundle()
    state = _FakeState(
        catalog=new_bundle.catalog,
        adapters=new_bundle.adapters,
        publishing=new_bundle.publishing,
        profiles=old_registry,
        service=new_bundle.service,
        rate_limiter=new_bundle.rate_limiter,
    )
    new_bundle.swap_into(state)

    # The ProfileRegistry identity is preserved; its _profiles dict is replaced.
    assert state.profiles is old_registry
    assert old_registry._profiles is new_bundle.profiles._profiles


def test_swap_into_updates_service_swappable_attrs():
    """swap_into updates GatewayService attrs that aren't in _SERVICE_STABLE_ATTRS.

    Note: catalog/adapters/profiles are swapped *in-place* so the service ends
    up pointing at the same *objects* that state.catalog etc. were updated to,
    not the new bundle's catalog object directly.  The test checks that the
    non-structural swappable attrs (policy, payload, output_filter, approval_store)
    come from the new bundle.
    """
    bundle = _build_bundle()
    old_service = bundle.service

    # Build a second bundle to act as the new runtime.
    new_bundle = _build_bundle()

    state = _FakeState(
        catalog=bundle.catalog,
        adapters=bundle.adapters,
        publishing=bundle.publishing,
        profiles=bundle.profiles,
        service=old_service,
        rate_limiter=bundle.rate_limiter,
    )
    new_bundle.swap_into(state)

    # catalog/adapters/profiles are swapped in-place so the service references the
    # same state-held objects (not the new bundle's fresh objects).
    in_place_structural = {"catalog", "adapters", "profiles"}

    # Swappable non-structural attrs should come from the new bundle's service.
    for attr in vars(new_bundle.service):
        if attr in _SERVICE_STABLE_ATTRS or attr in in_place_structural:
            continue
        assert getattr(state.service, attr) is getattr(new_bundle.service, attr), (
            f"Swappable attr {attr!r} should have been updated"
        )

    # Stable attrs must NOT have changed.
    for attr in _SERVICE_STABLE_ATTRS:
        if hasattr(old_service, attr):
            assert getattr(state.service, attr) is getattr(old_service, attr), (
                f"Stable attr {attr!r} should not have been swapped"
            )


def test_swap_into_preserves_stable_service_attrs():
    """sessions / audit / bus / primitives are never overwritten on swap."""
    bundle = _build_bundle()
    original_sessions = bundle.service.sessions
    original_audit = bundle.service.audit
    original_bus = bundle.service.bus
    original_primitives = bundle.service.primitives

    new_bundle = _build_bundle()
    state = _FakeState(
        catalog=bundle.catalog,
        adapters=bundle.adapters,
        publishing=bundle.publishing,
        profiles=bundle.profiles,
        service=bundle.service,
        rate_limiter=bundle.rate_limiter,
    )
    new_bundle.swap_into(state)

    assert state.service.sessions is original_sessions
    assert state.service.audit is original_audit
    assert state.service.bus is original_bus
    assert state.service.primitives is original_primitives


def test_swap_into_updates_rate_limiter():
    """swap_into updates state.rate_limiter."""
    bundle = _build_bundle()
    new_bundle = _build_bundle()
    state = _FakeState(
        catalog=bundle.catalog,
        adapters=bundle.adapters,
        publishing=bundle.publishing,
        profiles=bundle.profiles,
        service=bundle.service,
        rate_limiter=bundle.rate_limiter,
    )
    new_bundle.swap_into(state)
    assert state.rate_limiter is new_bundle.rate_limiter


def test_swap_into_fresh_state_sets_all_attrs():
    """When state attributes are all None, swap_into initializes them from the bundle."""
    bundle = _build_bundle()
    state = _FakeState()  # all None

    bundle.swap_into(state)

    assert state.catalog is bundle.catalog
    assert state.adapters is bundle.adapters
    assert state.publishing is bundle.publishing
    assert state.profiles is bundle.profiles
    assert state.rate_limiter is bundle.rate_limiter
