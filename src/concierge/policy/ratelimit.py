"""Distributed token-bucket rate limiting (P1-4).

Multi-dimensional buckets keyed by ``(tenant, session, tool)``. Two backends
share one async interface (``RateLimiter``):

* :class:`InMemoryTokenBucketRateLimiter` — process-local, the default/fallback
  used when no Redis is configured. Fine for single-replica or dev.
* :class:`RedisTokenBucketRateLimiter` — shared state in Redis so the limit
  holds *globally* across every app replica. Refill+take runs as one atomic
  Lua ``EVALSHA`` round trip, so concurrent replicas can never overspend a
  bucket (plain GET/SET would race).

Per-tenant quotas are configurable: a default quota plus optional per-tenant
overrides. The Redis key schema is versioned so it can evolve without colliding
with old data:

    rl:v1:{tenant}:{session}:{tool}

The public ``acquire`` returns a :class:`RateLimitDecision` carrying both the
allow/deny verdict and a ``retry_after`` (seconds until the bucket can grant the
requested cost) so callers can surface a meaningful ``Retry-After``.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - exercised only when redis is absent
    aioredis = None  # type: ignore[assignment]

# Bump when the key encoding or bucket semantics change in a backward-
# incompatible way; old keys simply expire and new ones take over.
KEY_SCHEMA_VERSION = "v1"


@dataclass(frozen=True)
class TenantQuota:
    """A token-bucket quota: ``capacity`` tokens, refilled at ``refill_per_sec``."""

    capacity: float
    refill_per_sec: float


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of an ``acquire`` call.

    ``retry_after`` is the number of seconds the caller should wait before the
    bucket can grant ``cost`` tokens. It is ``0.0`` when ``allowed`` is True.
    """

    allowed: bool
    retry_after: float = 0.0
    remaining: float = 0.0


def encode_key(tenant: str, session_id: str, name: str) -> str:
    """Canonical Redis key for a ``(tenant, session, tool)`` bucket."""
    return f"rl:{KEY_SCHEMA_VERSION}:{tenant}:{session_id}:{name}"


def _retry_after(tokens: float, cost: float, refill_per_sec: float) -> float:
    """Seconds until ``tokens`` refills enough to cover ``cost``."""
    deficit = cost - tokens
    if deficit <= 0:
        return 0.0
    if refill_per_sec <= 0:
        # Bucket never refills — the request can never be granted.
        return float("inf")
    return deficit / refill_per_sec


class RateLimiter(ABC):
    """Backend-agnostic limiter interface.

    Implementations resolve the per-tenant quota themselves so callers only pass
    identifying coordinates plus a cost.
    """

    @abstractmethod
    async def acquire(
        self,
        tenant: str,
        session_id: str,
        name: str,
        cost: float = 1.0,
    ) -> RateLimitDecision:
        """Atomically attempt to take ``cost`` tokens from the bucket."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any backend resources (connection pools etc.)."""
        return None


class _QuotaResolver:
    """Shared default + per-tenant quota lookup."""

    def __init__(
        self,
        default_capacity: float,
        default_refill: float,
        tenant_overrides: dict[str, TenantQuota] | None = None,
    ) -> None:
        self.default = TenantQuota(default_capacity, default_refill)
        self.tenant_overrides = dict(tenant_overrides or {})

    def quota_for(self, tenant: str) -> TenantQuota:
        return self.tenant_overrides.get(tenant, self.default)


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float


class InMemoryTokenBucketRateLimiter(RateLimiter):
    """Process-local token bucket keyed by ``(tenant, session, tool)``.

    This is the default backend and the fallback when Redis is not configured.
    State is per-process, so in a multi-replica deployment each replica enforces
    its own limit independently — use the Redis backend for a global limit.
    """

    def __init__(
        self,
        default_capacity: float = 30,
        default_refill: float = 0.5,
        *,
        tenant_overrides: dict[str, TenantQuota] | None = None,
    ) -> None:
        self.default_capacity = default_capacity
        self.default_refill = default_refill
        self._quotas = _QuotaResolver(default_capacity, default_refill, tenant_overrides)
        self._buckets: dict[tuple[str, str, str], _Bucket] = {}

    def _bucket(self, tenant: str, session_id: str, name: str) -> _Bucket:
        key = (tenant, session_id, name)
        b = self._buckets.get(key)
        if b is None:
            quota = self._quotas.quota_for(tenant)
            b = _Bucket(
                capacity=quota.capacity,
                refill_per_sec=quota.refill_per_sec,
                tokens=quota.capacity,
                last_refill=time.monotonic(),
            )
            self._buckets[key] = b
        return b

    async def acquire(
        self,
        tenant: str,
        session_id: str,
        name: str,
        cost: float = 1.0,
    ) -> RateLimitDecision:
        b = self._bucket(tenant, session_id, name)
        now = time.monotonic()
        b.tokens = min(b.capacity, b.tokens + (now - b.last_refill) * b.refill_per_sec)
        b.last_refill = now
        if b.tokens >= cost:
            b.tokens -= cost
            return RateLimitDecision(allowed=True, retry_after=0.0, remaining=b.tokens)
        return RateLimitDecision(
            allowed=False,
            retry_after=_retry_after(b.tokens, cost, b.refill_per_sec),
            remaining=b.tokens,
        )


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

# Atomic refill+take. KEYS[1] is the bucket key. ARGV:
#   1 capacity, 2 refill_per_sec, 3 now (unix seconds, float), 4 cost
# Stored as a hash {tokens, ts}. Returns {allowed(0|1), tokens_after, retry_after}.
# Running this server-side means the read-modify-write is atomic across every
# replica that targets the same Redis, so the global limit can never be
# overspent under concurrency.
_LUA_BUCKET_SCRIPT = """
local bucket = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', bucket, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry_after = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  local deficit = cost - tokens
  if refill > 0 then
    retry_after = deficit / refill
  else
    retry_after = -1
  end
end

redis.call('HSET', bucket, 'tokens', tokens, 'ts', now)
-- Expire idle buckets so abandoned (tenant,session,tool) tuples don't leak.
-- A full bucket needs capacity/refill seconds to refill; keep a generous pad.
local ttl = 3600
if refill > 0 then
  ttl = math.ceil(capacity / refill) + 60
end
redis.call('EXPIRE', bucket, ttl)

return {allowed, tostring(tokens), tostring(retry_after)}
"""


class RedisTokenBucketRateLimiter(RateLimiter):
    """Redis-backed distributed token bucket.

    Reuses the same redis[asyncio] client style as the P1-2 session store. The
    refill+take is a single atomic ``EVALSHA`` so concurrent replicas share one
    consistent bucket. Buckets auto-expire (TTL derived from refill time) so
    abandoned ``(tenant, session, tool)`` tuples are reclaimed.
    """

    def __init__(
        self,
        redis_url: str,
        default_capacity: float = 30,
        default_refill: float = 0.5,
        *,
        tenant_overrides: dict[str, TenantQuota] | None = None,
        max_connections: int = 64,
    ) -> None:
        if aioredis is None:
            raise RuntimeError("redis[asyncio] is required for RedisTokenBucketRateLimiter")
        # The limiter sits in the hot path of every tool call. Use a *blocking*
        # connection pool: when all connections are busy, extra callers wait for
        # one to free up (each Lua round trip is sub-millisecond) instead of the
        # default pool raising MaxConnectionsError under a burst. This bounds
        # Redis connection usage while degrading gracefully under load.
        pool = aioredis.BlockingConnectionPool.from_url(
            redis_url,
            decode_responses=True,
            max_connections=max_connections,
            timeout=None,
        )
        self._redis = aioredis.Redis(connection_pool=pool)
        self._quotas = _QuotaResolver(default_capacity, default_refill, tenant_overrides)
        self._script = self._redis.register_script(_LUA_BUCKET_SCRIPT)

    async def acquire(
        self,
        tenant: str,
        session_id: str,
        name: str,
        cost: float = 1.0,
    ) -> RateLimitDecision:
        quota = self._quotas.quota_for(tenant)
        key = encode_key(tenant, session_id, name)
        # register_script handles EVALSHA with EVAL fallback on NOSCRIPT.
        result = await self._script(
            keys=[key],
            args=[quota.capacity, quota.refill_per_sec, time.time(), cost],
        )
        allowed = bool(int(result[0]))
        tokens_after = float(result[1])
        raw_retry = float(result[2])
        retry_after = float("inf") if raw_retry < 0 else max(0.0, raw_retry)
        return RateLimitDecision(
            allowed=allowed,
            retry_after=0.0 if allowed else retry_after,
            remaining=tokens_after,
        )

    async def aclose(self) -> None:
        await self._redis.aclose()


# Backwards-compatible alias. The historical name kept the same constructor
# signature ``(default_capacity, default_refill)``; callers and existing tests
# that reference ``TokenBucketRateLimiter`` keep working unchanged.
TokenBucketRateLimiter = InMemoryTokenBucketRateLimiter


__all__ = [
    "KEY_SCHEMA_VERSION",
    "InMemoryTokenBucketRateLimiter",
    "RateLimitDecision",
    "RateLimiter",
    "RedisTokenBucketRateLimiter",
    "TenantQuota",
    "TokenBucketRateLimiter",
    "encode_key",
]
