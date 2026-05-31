"""P1-1 revocation list + tenant-token store backends.

In-memory backends run unconditionally. Redis backends run only when a Redis is
reachable (same skip pattern as the P1-4 Redis tests) — start it with:

    docker compose -f docker-compose.test.yml up -d redis
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from concierge.server.revocation import InMemoryRevocationStore
from concierge.server.tenant_tokens import InMemoryTenantTokenStore

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# In-memory revocation
# ---------------------------------------------------------------------------


async def test_inmem_revocation_basic():
    store = InMemoryRevocationStore()
    assert not await store.is_revoked("a")
    await store.revoke("a")
    assert await store.is_revoked("a")
    assert "a" in await store.all()
    await store.unrevoke("a")
    assert not await store.is_revoked("a")


async def test_inmem_revocation_ttl_expires():
    store = InMemoryRevocationStore()
    await store.revoke("short", ttl_s=-1)  # already expired
    assert not await store.is_revoked("short")
    assert "short" not in await store.all()


# ---------------------------------------------------------------------------
# In-memory tenant tokens
# ---------------------------------------------------------------------------


async def test_inmem_tenant_token_mint_resolve():
    store = InMemoryTenantTokenStore()
    minted = await store.mint("acme")
    assert minted.token  # raw secret exposed once
    lookup = await store.resolve(minted.token)
    assert lookup is not None
    assert lookup.tenant_id == "acme"
    assert lookup.token_id == minted.token_id


async def test_inmem_tenant_token_unknown_returns_none():
    store = InMemoryTenantTokenStore()
    await store.mint("acme")
    assert await store.resolve("not-a-real-token") is None


async def test_inmem_tenant_token_digest_only_no_raw_secret():
    store = InMemoryTenantTokenStore()
    minted = await store.mint("acme")
    records = await store.list_records()
    assert len(records) == 1
    # The store keeps a digest, never the raw secret.
    assert minted.token.encode() not in records[0].digest
    assert records[0].digest != minted.token.encode()


# ---------------------------------------------------------------------------
# Redis backends (skipped when unreachable)
# ---------------------------------------------------------------------------


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


async def test_redis_revocation_roundtrip(redis_url: str):
    from concierge.server.revocation import RedisRevocationStore

    store = RedisRevocationStore(redis_url)
    tid = f"jti-{uuid.uuid4().hex[:8]}"
    try:
        assert not await store.is_revoked(tid)
        await store.revoke(tid)
        assert await store.is_revoked(tid)
        assert tid in await store.all()
        await store.unrevoke(tid)
        assert not await store.is_revoked(tid)
    finally:
        await store.unrevoke(tid)
        await store.aclose()


async def test_redis_tenant_token_roundtrip(redis_url: str):
    from concierge.server.tenant_tokens import RedisTenantTokenStore

    store = RedisTenantTokenStore(redis_url, key_prefix=f"tt_test_{uuid.uuid4().hex[:6]}")
    try:
        minted = await store.mint("acme")
        lookup = await store.resolve(minted.token)
        assert lookup is not None
        assert lookup.tenant_id == "acme"
        rotated = await store.rotate("acme", minted.token_id)
        assert await store.resolve(minted.token) is None
        assert (await store.resolve(rotated.token)).tenant_id == "acme"
    finally:
        for rec in await store.list_records():
            await store._remove(rec.token_id)
        await store.aclose()
