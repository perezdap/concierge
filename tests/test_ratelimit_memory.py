"""Unit tests for the in-memory token-bucket limiter (P1-4)."""
from __future__ import annotations

import asyncio

import pytest

from concierge.policy.ratelimit import (
    InMemoryTokenBucketRateLimiter,
    RateLimiter,
    TenantQuota,
    TokenBucketRateLimiter,
    encode_key,
)

pytestmark = pytest.mark.asyncio


async def test_alias_points_at_in_memory_impl():
    # Backwards-compat: historical name still resolves and is a RateLimiter.
    assert TokenBucketRateLimiter is InMemoryTokenBucketRateLimiter
    limiter = TokenBucketRateLimiter(default_capacity=5, default_refill=1.0)
    assert isinstance(limiter, RateLimiter)


async def test_allows_until_capacity_then_denies():
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=3, default_refill=0.0)
    for _ in range(3):
        d = await limiter.acquire("t", "s", "tool")
        assert d.allowed
    denied = await limiter.acquire("t", "s", "tool")
    assert not denied.allowed
    assert denied.remaining < 1.0


async def test_retry_after_math_matches_refill_rate():
    # capacity 1, no initial tokens left, refill 2 tokens/sec => need 0.5s for 1 token.
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=1, default_refill=2.0)
    assert (await limiter.acquire("t", "s", "tool")).allowed  # drains the single token
    denied = await limiter.acquire("t", "s", "tool")
    assert not denied.allowed
    # ~0.5s to refill one token; allow generous tolerance for clock granularity.
    assert 0.4 <= denied.retry_after <= 0.6


async def test_refill_restores_tokens_over_time():
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=1, default_refill=50.0)
    assert (await limiter.acquire("t", "s", "tool")).allowed
    assert not (await limiter.acquire("t", "s", "tool")).allowed
    await asyncio.sleep(0.05)  # 50/s * 0.05s = 2.5 tokens, capped at capacity 1
    assert (await limiter.acquire("t", "s", "tool")).allowed


async def test_non_refilling_bucket_reports_infinite_retry_after():
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=1, default_refill=0.0)
    assert (await limiter.acquire("t", "s", "tool")).allowed
    denied = await limiter.acquire("t", "s", "tool")
    assert not denied.allowed
    assert denied.retry_after == float("inf")


async def test_buckets_are_isolated_per_dimension():
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=1, default_refill=0.0)
    assert (await limiter.acquire("t", "s", "toolA")).allowed
    # Different tool dimension is a different bucket → still has its own token.
    assert (await limiter.acquire("t", "s", "toolB")).allowed
    # Different session, same tool → independent bucket.
    assert (await limiter.acquire("t", "s2", "toolA")).allowed
    # Different tenant, same session+tool → independent bucket.
    assert (await limiter.acquire("t2", "s", "toolA")).allowed
    # Re-using the first exhausted bucket denies.
    assert not (await limiter.acquire("t", "s", "toolA")).allowed


async def test_per_tenant_override_capacity():
    limiter = InMemoryTokenBucketRateLimiter(
        default_capacity=1,
        default_refill=0.0,
        tenant_overrides={"vip": TenantQuota(capacity=3, refill_per_sec=0.0)},
    )
    # Default tenant: 1 token.
    assert (await limiter.acquire("free", "s", "tool")).allowed
    assert not (await limiter.acquire("free", "s", "tool")).allowed
    # VIP tenant: 3 tokens.
    for _ in range(3):
        assert (await limiter.acquire("vip", "s", "tool")).allowed
    assert not (await limiter.acquire("vip", "s", "tool")).allowed


async def test_cost_greater_than_one():
    limiter = InMemoryTokenBucketRateLimiter(default_capacity=5, default_refill=0.0)
    assert (await limiter.acquire("t", "s", "tool", cost=3)).allowed
    # 2 tokens left, cost 3 → deny.
    assert not (await limiter.acquire("t", "s", "tool", cost=3)).allowed


async def test_key_encoding_is_versioned():
    assert encode_key("acme", "sess123", "echo__echo") == "rl:v1:acme:sess123:echo__echo"
