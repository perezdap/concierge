"""Regression tests for the SessionManager write seam (fix/session-write-seam).

The core bug: PublishingService and GatewayService mutated the Session object
returned by SessionManager.get() directly.  For the in-memory backend this
aliases the stored object so mutations are visible on the next get().  For
RedisSessionManager get() returns a fresh deserialised copy each time, so
mutations were silently dropped — enable_tools / disable_tools / use_profile
would appear to succeed but the state was never persisted to Redis.

These tests use fakeredis to reproduce the bug without a real Redis server.
The in-memory path is tested to confirm it is unaffected.
"""
from __future__ import annotations

import pytest

from concierge.core.catalog import Catalog
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import RedisSessionManager, SessionManager
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(name: str, server_id: str = "test") -> CatalogEntry:
    return CatalogEntry(
        canonical_name=name,
        upstream_name=name.split("__", 1)[-1],
        server_id=server_id,
        transport=TransportType.STREAMABLE_HTTP,
        primitive_type=PrimitiveType.TOOL,
        display_label=name,
        short_description="test tool",
        risk_level=RiskLevel.LOW,
    )


async def _make_catalog(*names: str) -> Catalog:
    catalog = Catalog()
    for name in names:
        await catalog.upsert(_make_entry(name))
    return catalog


# ---------------------------------------------------------------------------
# fakeredis fixture — no real Redis server required
# ---------------------------------------------------------------------------

@pytest.fixture()
def redis_session_manager():
    """RedisSessionManager backed by an in-process fakeredis server."""
    try:
        import fakeredis.aioredis as fake_aioredis  # type: ignore[import]
    except ImportError:
        pytest.skip("fakeredis not installed")

    import redis.asyncio as aioredis  # noqa: F401 (required by RedisSessionManager)

    # Patch redis.asyncio.from_url so RedisSessionManager.__init__ picks up fakeredis.
    import concierge.core.session as session_mod
    real_aioredis = session_mod.aioredis

    fake_server = fake_aioredis.FakeRedis(decode_responses=True)

    class _FakeModule:
        @staticmethod
        def from_url(url: str, **kwargs):  # noqa: ARG004
            return fake_server

    session_mod.aioredis = _FakeModule  # type: ignore[assignment]
    try:
        sm = RedisSessionManager("redis://localhost/0", idle_ttl_seconds=60)
        yield sm
    finally:
        session_mod.aioredis = real_aioredis


# ---------------------------------------------------------------------------
# Core regression: enable → get round-trips on RedisSessionManager
# ---------------------------------------------------------------------------

async def test_enable_persists_to_redis(redis_session_manager: RedisSessionManager):
    """Publishing a tool must survive a fresh get() on the Redis backend."""
    sm = redis_session_manager
    catalog = await _make_catalog("svc__tool_a", "svc__tool_b")
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus, sessions=sm)

    session = await sm.create()
    enabled, skipped = await publishing.enable(session, ["svc__tool_a"], by="client")
    assert enabled == ["svc__tool_a"]
    assert skipped == []

    # Reload from Redis — without the fix this returns an empty published_tools dict.
    reloaded = await sm.get(session.session_id)
    assert reloaded is not None
    assert "svc__tool_a" in reloaded.published_tools, (
        "enable() must persist to Redis; published_tools was not written back"
    )


async def test_disable_persists_to_redis(redis_session_manager: RedisSessionManager):
    """Disabling a tool must also survive a fresh get()."""
    sm = redis_session_manager
    catalog = await _make_catalog("svc__tool_a")
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus, sessions=sm)

    session = await sm.create()
    await publishing.enable(session, ["svc__tool_a"], by="client")

    # Reload then disable — this exercises that disable() also calls save()
    session2 = await sm.get(session.session_id)
    assert session2 is not None
    removed = await publishing.disable(session2, ["svc__tool_a"])
    assert removed == ["svc__tool_a"]

    reloaded = await sm.get(session.session_id)
    assert reloaded is not None
    assert "svc__tool_a" not in reloaded.published_tools, (
        "disable() must persist to Redis"
    )


async def test_disable_all_persists_to_redis(redis_session_manager: RedisSessionManager):
    """disable_all() must clear state in Redis."""
    sm = redis_session_manager
    catalog = await _make_catalog("svc__tool_a", "svc__tool_b")
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus, sessions=sm)

    session = await sm.create()
    await publishing.enable(session, ["svc__tool_a", "svc__tool_b"], by="client")

    session2 = await sm.get(session.session_id)
    assert session2 is not None
    n = await publishing.disable_all(session2)
    assert n == 2

    reloaded = await sm.get(session.session_id)
    assert reloaded is not None
    assert reloaded.published_tools == {}, (
        "disable_all() must persist cleared state to Redis"
    )


async def test_active_profiles_persists_via_save(redis_session_manager: RedisSessionManager):
    """SessionManager.save() must persist active_profiles to Redis."""
    sm = redis_session_manager
    session = await sm.create()
    session.active_profiles.append("my-profile")
    await sm.save(session)

    reloaded = await sm.get(session.session_id)
    assert reloaded is not None
    assert "my-profile" in reloaded.active_profiles, (
        "save() must write active_profiles back to Redis"
    )


# ---------------------------------------------------------------------------
# In-memory backend is unaffected
# ---------------------------------------------------------------------------

async def test_enable_persists_in_memory():
    """In-memory backend should continue to work (alias means no save needed)."""
    sm = SessionManager()
    catalog = await _make_catalog("svc__tool_a")
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus, sessions=sm)

    session = await sm.create()
    await publishing.enable(session, ["svc__tool_a"], by="client")

    reloaded = await sm.get(session.session_id)
    assert reloaded is not None
    assert "svc__tool_a" in reloaded.published_tools


async def test_save_noop_for_in_memory():
    """SessionManager.save() on in-memory backend is a no-op (no error)."""
    sm = SessionManager()
    session = await sm.create()
    await sm.save(session)  # should not raise


# ---------------------------------------------------------------------------
# PublishingService without sessions= still works (backwards compat)
# ---------------------------------------------------------------------------

async def test_publishing_without_sessions_arg():
    """sessions=None (old call-site default) must not break publish operations."""
    sm = SessionManager()
    catalog = await _make_catalog("svc__tool_a")
    bus = NotificationBus()
    publishing = PublishingService(catalog, bus)  # no sessions kwarg

    session = await sm.create()
    enabled, _ = await publishing.enable(session, ["svc__tool_a"], by="client")
    assert enabled == ["svc__tool_a"]
