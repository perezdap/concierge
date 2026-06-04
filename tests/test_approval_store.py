"""P1-3 ApprovalStore backends + queued broker semantics.

In-memory backend runs unconditionally. Redis / Postgres backends run only when
those services are reachable (same skip pattern as the P1-1 / P1-2 / P1-4
integration tests).
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

from concierge.policy.approval_store import (
    ApprovalStatus,
    InMemoryApprovalStore,
)

pytestmark = pytest.mark.asyncio


async def _mk(store, *, tenant="acme", tool="srv__danger", ttl=300.0):
    return await store.create(
        tenant_id=tenant,
        session_id="sess-1",
        canonical_name=tool,
        args_summary='{"x": 1}',
        ttl_s=ttl,
    )


# --- in-memory store -------------------------------------------------------


async def test_create_and_fetch_pending():
    store = InMemoryApprovalStore()
    rec = await _mk(store)
    assert rec.status is ApprovalStatus.PENDING
    fetched = await store.get(rec.approval_id)
    assert fetched is not None
    assert fetched.canonical_name == "srv__danger"
    assert rec.approval_id in [r.approval_id for r in await store.list_pending()]


async def test_grant_decision_is_recorded():
    store = InMemoryApprovalStore()
    rec = await _mk(store)
    decided = await store.decide(
        rec.approval_id, granted=True, decided_by="ops@acme", tenant_id="acme"
    )
    assert decided is not None
    assert decided.status is ApprovalStatus.GRANTED
    assert decided.decided_by == "ops@acme"
    # No longer pending.
    assert await store.list_pending() == []


async def test_deny_decision_is_recorded():
    store = InMemoryApprovalStore()
    rec = await _mk(store)
    decided = await store.decide(
        rec.approval_id, granted=False, decided_by="ops@acme", reason="nope"
    )
    assert decided.status is ApprovalStatus.DENIED
    assert decided.reason == "nope"


async def test_decision_is_idempotent_first_wins():
    store = InMemoryApprovalStore()
    rec = await _mk(store)
    first = await store.decide(rec.approval_id, granted=True, decided_by="a")
    second = await store.decide(rec.approval_id, granted=False, decided_by="b")
    assert first.status is ApprovalStatus.GRANTED
    # The later deny is a no-op: grant stands.
    assert second.status is ApprovalStatus.GRANTED
    assert second.decided_by == "a"


async def test_ttl_expiry_flips_to_expired_on_read():
    store = InMemoryApprovalStore()
    rec = await _mk(store, ttl=-1)  # already expired
    fetched = await store.get(rec.approval_id)
    assert fetched.status is ApprovalStatus.EXPIRED
    assert await store.list_pending() == []


async def test_cross_tenant_decision_refused():
    store = InMemoryApprovalStore()
    rec = await _mk(store, tenant="acme")
    # Operator scoped to a different tenant cannot decide.
    result = await store.decide(
        rec.approval_id, granted=True, decided_by="evil", tenant_id="other"
    )
    assert result is None
    # Record is still pending.
    assert (await store.get(rec.approval_id)).status is ApprovalStatus.PENDING


async def test_list_pending_tenant_scoped():
    store = InMemoryApprovalStore()
    await _mk(store, tenant="acme")
    await _mk(store, tenant="globex")
    assert len(await store.list_pending(tenant_id="acme")) == 1
    assert len(await store.list_pending()) == 2


# --- Redis backend (skipped when unreachable) ------------------------------


@pytest.fixture(scope="module")
def redis_url():
    try:
        import redis.asyncio as aioredis
    except ImportError:
        pytest.skip("redis[asyncio] not installed")
    url = "redis://localhost:6379/0"

    async def _probe():
        r = aioredis.from_url(url, decode_responses=True)
        try:
            await r.ping()
        except Exception:
            pytest.skip("redis server not reachable")
        finally:
            await r.aclose()

    asyncio.run(_probe())
    return url


async def test_redis_store_roundtrip(redis_url: str):
    from concierge.policy.approval_store import RedisApprovalStore

    store = RedisApprovalStore(redis_url, key_prefix=f"ap_test_{uuid.uuid4().hex[:6]}")
    try:
        rec = await _mk(store)
        fetched = await store.get(rec.approval_id)
        assert fetched is not None and fetched.tenant_id == "acme"
        decided = await store.decide(rec.approval_id, granted=True, decided_by="ops")
        assert decided.status is ApprovalStatus.GRANTED
        # idempotent
        again = await store.decide(rec.approval_id, granted=False, decided_by="x")
        assert again.status is ApprovalStatus.GRANTED
    finally:
        await store.aclose()


# --- Postgres backend (skipped when unreachable) ---------------------------


_DSN = os.environ.get(
    "CONCIERGE_TEST_PG_DSN",
    "postgresql://postgres:concierge@localhost:5432/concierge",
)


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    async def _probe():
        try:
            conn = await asyncpg.connect(_DSN)
        except Exception:
            pytest.skip("postgres server not reachable")
        else:
            await conn.close()

    asyncio.run(_probe())
    return _DSN


async def test_postgres_store_roundtrip(pg_dsn: str):
    from concierge.policy.approval_store import PostgresApprovalStore

    store = PostgresApprovalStore(pg_dsn)
    try:
        rec = await _mk(store, tool=f"srv__t_{uuid.uuid4().hex[:6]}")
        fetched = await store.get(rec.approval_id)
        assert fetched is not None
        decided = await store.decide(
            rec.approval_id, granted=False, decided_by="ops", reason="audit"
        )
        assert decided.status is ApprovalStatus.DENIED
        assert decided.reason == "audit"
    finally:
        # Best-effort cleanup of the row we created.
        pool = await store._ensure_pool()
        await pool.execute("DELETE FROM approvals WHERE approval_id = $1", rec.approval_id)
        await store.aclose()


# --- queued broker bounded-await ------------------------------------------


async def test_broker_resumes_on_grant():
    from concierge.core.types import (
        CatalogEntry,
        PrimitiveType,
        RiskLevel,
        Session,
        TransportType,
    )
    from concierge.policy.approval import QueuedApprovalBroker

    store = InMemoryApprovalStore()
    broker = QueuedApprovalBroker(store, ttl_s=5.0, wait_timeout_s=5.0, poll_interval_s=0.05)
    session = Session(tenant_id="acme")
    entry = CatalogEntry(
        canonical_name="srv__danger",
        upstream_name="danger",
        server_id="srv",
        transport=TransportType.STDIO,
        primitive_type=PrimitiveType.TOOL,
        display_label="danger",
        short_description="dangerous",
        risk_level=RiskLevel.DANGEROUS,
        requires_approval=True,
    )

    async def grant_soon():
        # Wait for the record to be parked, then grant it.
        for _ in range(100):
            pending = await store.list_pending(tenant_id="acme")
            if pending:
                await broker.decide(
                    pending[0].approval_id, granted=True, decided_by="ops", tenant_id="acme"
                )
                return
            await asyncio.sleep(0.02)

    granter = asyncio.create_task(grant_soon())
    decision = await broker.evaluate(session, entry, {"k": "v"})
    await granter
    assert decision.approved is True
    assert decision.record.status is ApprovalStatus.GRANTED


async def test_broker_denies_on_deny():
    from concierge.core.types import (
        CatalogEntry,
        PrimitiveType,
        RiskLevel,
        Session,
        TransportType,
    )
    from concierge.policy.approval import QueuedApprovalBroker

    store = InMemoryApprovalStore()
    broker = QueuedApprovalBroker(store, ttl_s=5.0, wait_timeout_s=5.0, poll_interval_s=0.05)
    session = Session(tenant_id="acme")
    entry = CatalogEntry(
        canonical_name="srv__danger", upstream_name="danger", server_id="srv",
        transport=TransportType.STDIO, primitive_type=PrimitiveType.TOOL,
        display_label="danger", short_description="d", risk_level=RiskLevel.DANGEROUS,
        requires_approval=True,
    )

    async def deny_soon():
        for _ in range(100):
            pending = await store.list_pending(tenant_id="acme")
            if pending:
                await broker.decide(pending[0].approval_id, granted=False, decided_by="ops")
                return
            await asyncio.sleep(0.02)

    t = asyncio.create_task(deny_soon())
    decision = await broker.evaluate(session, entry, {})
    await t
    assert decision.approved is False
    assert decision.reason == "denied"


async def test_broker_ttl_expiry_returns_denial():
    from concierge.core.types import (
        CatalogEntry,
        PrimitiveType,
        RiskLevel,
        Session,
        TransportType,
    )
    from concierge.policy.approval import QueuedApprovalBroker

    store = InMemoryApprovalStore()
    # Tiny TTL so the bounded await elapses fast with no decision.
    broker = QueuedApprovalBroker(store, ttl_s=0.2, wait_timeout_s=0.2, poll_interval_s=0.05)
    session = Session(tenant_id="acme")
    entry = CatalogEntry(
        canonical_name="srv__danger", upstream_name="danger", server_id="srv",
        transport=TransportType.STDIO, primitive_type=PrimitiveType.TOOL,
        display_label="danger", short_description="d", risk_level=RiskLevel.DANGEROUS,
        requires_approval=True,
    )
    start = time.time()
    decision = await broker.evaluate(session, entry, {})
    assert decision.approved is False
    assert decision.reason == "expired"
    assert time.time() - start < 2.0  # did not hang
