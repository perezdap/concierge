"""Redis-backed distributed rate limiter integration + load tests (P1-4).

Skipped automatically when redis is not installed or the server is unreachable.
Run the backing Redis with the P1-2 test compose stack:

    docker compose -f docker-compose.test.yml up -d redis
    pytest -q tests/test_ratelimit_redis.py
    docker compose -f docker-compose.test.yml down

The load test (``test_load_global_limit_holds_under_concurrency``) fans out N
concurrent ``acquire`` calls against the Redis backend and asserts the total
grants never exceed the bucket capacity — i.e. the atomic Lua bucket holds the
global limit even under contention.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from concierge.policy.ratelimit import (
    RedisTokenBucketRateLimiter,
    TenantQuota,
    encode_key,
)

pytestmark = pytest.mark.asyncio


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


def _tenant() -> str:
    # Unique tenant per test so buckets never collide across runs/tests.
    return f"t-{uuid.uuid4().hex[:8]}"


async def test_allows_until_capacity_then_denies(redis_url: str):
    limiter = RedisTokenBucketRateLimiter(redis_url, default_capacity=3, default_refill=0.0)
    tenant = _tenant()
    try:
        for _ in range(3):
            assert (await limiter.acquire(tenant, "s", "tool")).allowed
        denied = await limiter.acquire(tenant, "s", "tool")
        assert not denied.allowed
    finally:
        await limiter.aclose()


async def test_retry_after_matches_refill_math(redis_url: str):
    # capacity 1, refill 2/s → after draining, ~0.5s to refill one token.
    limiter = RedisTokenBucketRateLimiter(redis_url, default_capacity=1, default_refill=2.0)
    tenant = _tenant()
    try:
        assert (await limiter.acquire(tenant, "s", "tool")).allowed
        denied = await limiter.acquire(tenant, "s", "tool")
        assert not denied.allowed
        assert 0.4 <= denied.retry_after <= 0.6
    finally:
        await limiter.aclose()


async def test_bucket_key_written_with_ttl(redis_url: str):
    import redis.asyncio as aioredis

    limiter = RedisTokenBucketRateLimiter(redis_url, default_capacity=2, default_refill=1.0)
    tenant = _tenant()
    raw = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await limiter.acquire(tenant, "s", "tool")
        key = encode_key(tenant, "s", "tool")
        assert await raw.exists(key) == 1
        ttl = await raw.ttl(key)
        # capacity/refill + 60 pad = 2/1 + 60 = 62s, minus a little elapsed.
        assert 0 < ttl <= 62
    finally:
        await raw.aclose()
        await limiter.aclose()


async def test_two_replicas_share_one_bucket(redis_url: str):
    """Two separate limiter clients (simulated replicas) → one global bucket."""
    tenant = _tenant()
    replica_a = RedisTokenBucketRateLimiter(redis_url, default_capacity=4, default_refill=0.0)
    replica_b = RedisTokenBucketRateLimiter(redis_url, default_capacity=4, default_refill=0.0)
    try:
        # A spends 2, B spends 2 → bucket exhausted; the 5th (from either) denies.
        assert (await replica_a.acquire(tenant, "s", "tool")).allowed
        assert (await replica_a.acquire(tenant, "s", "tool")).allowed
        assert (await replica_b.acquire(tenant, "s", "tool")).allowed
        assert (await replica_b.acquire(tenant, "s", "tool")).allowed
        assert not (await replica_a.acquire(tenant, "s", "tool")).allowed
        assert not (await replica_b.acquire(tenant, "s", "tool")).allowed
    finally:
        await replica_a.aclose()
        await replica_b.aclose()


async def test_atomicity_two_concurrent_replicas(redis_url: str):
    """Concurrency check: two replicas hammer the same bucket simultaneously.

    Even with both racing, total grants must equal capacity exactly — the atomic
    Lua refill+take makes overspend impossible (a plain GET/SET would let both
    read the same token count and double-spend).
    """
    capacity = 50
    tenant = _tenant()
    replica_a = RedisTokenBucketRateLimiter(
        redis_url, default_capacity=capacity, default_refill=0.0
    )
    replica_b = RedisTokenBucketRateLimiter(
        redis_url, default_capacity=capacity, default_refill=0.0
    )
    try:
        async def take(limiter):
            d = await limiter.acquire(tenant, "s", "tool")
            return 1 if d.allowed else 0

        # 2x capacity attempts split across two replicas, all in flight at once.
        tasks = []
        for i in range(capacity * 2):
            limiter = replica_a if i % 2 == 0 else replica_b
            tasks.append(take(limiter))
        results = await asyncio.gather(*tasks)
        granted = sum(results)
        assert granted == capacity, f"expected exactly {capacity} grants, got {granted}"
    finally:
        await replica_a.aclose()
        await replica_b.aclose()


async def test_per_tenant_override(redis_url: str):
    vip = _tenant()
    free = _tenant()
    limiter = RedisTokenBucketRateLimiter(
        redis_url,
        default_capacity=1,
        default_refill=0.0,
        tenant_overrides={vip: TenantQuota(capacity=3, refill_per_sec=0.0)},
    )
    try:
        assert (await limiter.acquire(free, "s", "tool")).allowed
        assert not (await limiter.acquire(free, "s", "tool")).allowed
        for _ in range(3):
            assert (await limiter.acquire(vip, "s", "tool")).allowed
        assert not (await limiter.acquire(vip, "s", "tool")).allowed
    finally:
        await limiter.aclose()


async def test_load_global_limit_holds_under_concurrency(redis_url: str):
    """Load-style test: N concurrent callers, allowed-count <= capacity per window.

    Also asserts every denied caller gets a finite, positive Retry-After that is
    consistent with the refill rate (within tolerance).
    """
    capacity = 100
    refill = 10.0  # tokens/sec
    n = 400
    tenant = _tenant()
    limiter = RedisTokenBucketRateLimiter(
        redis_url, default_capacity=capacity, default_refill=refill
    )
    try:
        async def call():
            return await limiter.acquire(tenant, "s", "tool")

        decisions = await asyncio.gather(*[call() for _ in range(n)])
        allowed = [d for d in decisions if d.allowed]
        denied = [d for d in decisions if not d.allowed]

        # The window started full; a tiny amount may refill during the burst, so
        # allow a small slack but never less than capacity and never wildly more.
        assert capacity <= len(allowed) <= capacity + int(refill) + 1
        assert len(denied) == n - len(allowed)

        # Backoff signaling: max retry_after is at most (capacity / refill) seconds.
        max_expected = capacity / refill
        for d in denied:
            assert 0.0 < d.retry_after <= max_expected + 0.5
    finally:
        await limiter.aclose()
