"""Token revocation list (P1-1).

Every successful authentication carries a *token id* — the OIDC ``jti`` claim, or
an opaque salted id for static/tenant bearer tokens. A revocation entry keyed by
that id is enforced on *every* auth check, across every provider, so a leaked or
rotated credential can be killed centrally without redeploying.

The store must outlive any single replica (the app tier is stateless — P1-2),
so the same backend matrix as the catalog/session/rate-limiter layers applies:

* :class:`InMemoryRevocationStore` — process-local, **tests/dev only**. A revoked
  id added on one replica is invisible to the others; never use it in prod.
* :class:`RedisRevocationStore`   — shared set in Redis, optional per-entry TTL so
  a revocation can expire alongside the token it kills (no unbounded growth).
* :class:`PostgresRevocationStore` — durable table, survives a full Redis flush.

Only the *token id* is ever stored — never raw token / id_token bytes — matching
the P0-2 no-secret-material rule. The id is itself a non-reversible digest, so the
revocation list leaks nothing about the underlying credential even if dumped.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - exercised only when redis is absent
    aioredis = None  # type: ignore[assignment]

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


class RevocationStore(ABC):
    """Backend-agnostic revocation list keyed by opaque token id."""

    @abstractmethod
    async def is_revoked(self, token_id: str) -> bool:
        """True if ``token_id`` has been revoked. Called on every auth check."""

    @abstractmethod
    async def revoke(self, token_id: str, *, ttl_s: int | None = None) -> None:
        """Add ``token_id`` to the revocation list.

        ``ttl_s`` optionally expires the entry (use the token's own remaining
        lifetime so the list does not grow without bound for short-lived JWTs).
        """

    @abstractmethod
    async def unrevoke(self, token_id: str) -> None:
        """Remove ``token_id`` from the revocation list (e.g. a mis-fire)."""

    @abstractmethod
    async def all(self) -> list[str]:
        """Snapshot of revoked ids (admin/inspection)."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryRevocationStore(RevocationStore):
    """Process-local revocation list. Tests/dev only — never shared across replicas."""

    def __init__(self) -> None:
        # token_id -> expiry epoch (None = never expires)
        self._revoked: dict[str, float | None] = {}
        self._lock = asyncio.Lock()

    async def is_revoked(self, token_id: str) -> bool:
        async with self._lock:
            if token_id not in self._revoked:
                return False
            expiry = self._revoked[token_id]
            if expiry is not None and expiry < time.time():
                del self._revoked[token_id]
                return False
            return True

    async def revoke(self, token_id: str, *, ttl_s: int | None = None) -> None:
        async with self._lock:
            self._revoked[token_id] = (time.time() + ttl_s) if ttl_s else None

    async def unrevoke(self, token_id: str) -> None:
        async with self._lock:
            self._revoked.pop(token_id, None)

    async def all(self) -> list[str]:
        async with self._lock:
            now = time.time()
            return [
                tid for tid, exp in self._revoked.items()
                if exp is None or exp >= now
            ]


class RedisRevocationStore(RevocationStore):
    """Redis-backed revocation list.

    Each revoked id is its own key (``revoke:v1:{id}``) so per-entry TTL works:
    a short-lived JWT's revocation expires when the token would have anyway, so
    the list self-prunes. Reuses the shared redis[asyncio] client style.
    """

    KEY_SCHEMA_VERSION = "v1"

    def __init__(self, redis_url: str, *, key_prefix: str = "revoke") -> None:
        if aioredis is None:
            raise RuntimeError("redis[asyncio] is required for RedisRevocationStore")
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._prefix = f"{key_prefix}:{self.KEY_SCHEMA_VERSION}:"

    def _key(self, token_id: str) -> str:
        return f"{self._prefix}{token_id}"

    async def is_revoked(self, token_id: str) -> bool:
        return await self._redis.exists(self._key(token_id)) > 0

    async def revoke(self, token_id: str, *, ttl_s: int | None = None) -> None:
        # ex must be a positive int; treat <=0 (already-expired token) as a no-op
        # short TTL so we don't reject an id that can never be presented anyway.
        if ttl_s is not None and ttl_s > 0:
            await self._redis.set(self._key(token_id), "1", ex=ttl_s)
        else:
            await self._redis.set(self._key(token_id), "1")

    async def unrevoke(self, token_id: str) -> None:
        await self._redis.delete(self._key(token_id))

    async def all(self) -> list[str]:
        out: list[str] = []
        async for key in self._redis.scan_iter(match=f"{self._prefix}*"):
            out.append(key[len(self._prefix):])
        return out

    async def aclose(self) -> None:
        await self._redis.aclose()


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_revocations (
    token_id   TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ
);
"""


class PostgresRevocationStore(RevocationStore):
    """Durable revocation list in Postgres (asyncpg).

    Survives a Redis flush or restart. Expired entries are filtered at read time
    and opportunistically pruned, so an operator can also rely on the row count
    as the live revocation list.
    """

    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgresRevocationStore")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(_PG_SCHEMA)
        return self._pool

    async def is_revoked(self, token_id: str) -> bool:
        pool = await self._ensure_pool()
        row = await pool.fetchrow(
            "SELECT expires_at FROM auth_revocations WHERE token_id = $1", token_id
        )
        if row is None:
            return False
        expires_at = row["expires_at"]
        if expires_at is not None:
            from datetime import UTC, datetime
            if expires_at < datetime.now(UTC):
                await pool.execute(
                    "DELETE FROM auth_revocations WHERE token_id = $1", token_id
                )
                return False
        return True

    async def revoke(self, token_id: str, *, ttl_s: int | None = None) -> None:
        from datetime import UTC, datetime, timedelta
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=ttl_s)
            if ttl_s is not None and ttl_s > 0
            else None
        )
        pool = await self._ensure_pool()
        await pool.execute(
            """
            INSERT INTO auth_revocations (token_id, expires_at)
            VALUES ($1, $2)
            ON CONFLICT (token_id) DO UPDATE SET expires_at = EXCLUDED.expires_at
            """,
            token_id,
            expires_at,
        )

    async def unrevoke(self, token_id: str) -> None:
        pool = await self._ensure_pool()
        await pool.execute("DELETE FROM auth_revocations WHERE token_id = $1", token_id)

    async def all(self) -> list[str]:
        from datetime import UTC, datetime
        pool = await self._ensure_pool()
        rows = await pool.fetch(
            "SELECT token_id FROM auth_revocations "
            "WHERE expires_at IS NULL OR expires_at >= $1",
            datetime.now(UTC),
        )
        return [r["token_id"] for r in rows]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = [
    "InMemoryRevocationStore",
    "PostgresRevocationStore",
    "RedisRevocationStore",
    "RevocationStore",
]
