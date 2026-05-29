"""Token bucket rate limiter keyed by (session, canonical_name)."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(self, default_capacity: float = 30, default_refill: float = 0.5) -> None:
        self.default_capacity = default_capacity
        self.default_refill = default_refill
        self._buckets: dict[tuple[str, str], Bucket] = {}

    def _bucket(self, session_id: str, name: str) -> Bucket:
        key = (session_id, name)
        b = self._buckets.get(key)
        if b is None:
            b = Bucket(
                capacity=self.default_capacity,
                refill_per_sec=self.default_refill,
                tokens=self.default_capacity,
                last_refill=time.monotonic(),
            )
            self._buckets[key] = b
        return b

    def allow(self, session_id: str, name: str, cost: float = 1.0) -> bool:
        b = self._bucket(session_id, name)
        now = time.monotonic()
        b.tokens = min(b.capacity, b.tokens + (now - b.last_refill) * b.refill_per_sec)
        b.last_refill = now
        if b.tokens >= cost:
            b.tokens -= cost
            return True
        return False
