"""Session manager — owns Session lifecycle keyed by MCP-Session-Id."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from ..errors import session_unauthorized
from .types import Session

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore[assignment]


class SessionManager:
    """In-memory session manager."""

    def __init__(
        self,
        idle_ttl_seconds: int = 60 * 60,
        *,
        on_evict: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.idle_ttl = idle_ttl_seconds
        self._on_evict = on_evict

    async def create(
        self, *, tenant_id: str = "default", auth_subject: str | None = None
    ) -> Session:
        async with self._lock:
            s = Session(tenant_id=tenant_id, auth_subject=auth_subject)
            self._sessions[s.session_id] = s
            return s

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                s.last_seen_at = datetime.now(UTC)
            return s

    async def save(self, session: Session) -> None:
        """Persist mutated session state.

        For the in-memory backend this is a no-op because callers hold a direct
        reference to the stored object.  For backends that deserialise a fresh
        copy on every get() (e.g. RedisSessionManager) callers MUST call save()
        after mutating published buckets or active_profiles so the changes are
        not silently dropped.
        """
        ...

    async def require(self, session_id: str | None) -> Session:
        if not session_id:
            raise session_unauthorized(reason="session_missing_header")
        s = await self.get(session_id)
        if s is None:
            raise session_unauthorized(reason="session_not_found")
        return s

    async def close(self, session_id: str) -> None:
        existed = self._sessions.pop(session_id, None) is not None
        if existed and self._on_evict is not None:
            await self._on_evict(session_id)

    async def aclose(self, session_id: str) -> None:
        """Close a session and fire the eviction hook (async-safe)."""
        await self.close(session_id)

    async def all(self) -> list[Session]:
        return list(self._sessions.values())

    async def gc(self) -> int:
        now = datetime.now(UTC)
        victims = [
            sid for sid, s in self._sessions.items()
            if (now - s.last_seen_at).total_seconds() > self.idle_ttl
        ]
        async with self._lock:
            for sid in victims:
                self._sessions.pop(sid, None)
        if self._on_evict is not None:
            for sid in victims:
                await self._on_evict(sid)
        return len(victims)


class RedisSessionManager(SessionManager):
    """Redis-backed session manager for stateless app tiers.

    Stores session JSON in Redis with TTL. ``idle_ttl`` is mapped to Redis EX.
    The in-memory ``_sessions`` dict is unused; all operations hit Redis.
    """

    def __init__(
        self,
        redis_url: str,
        idle_ttl_seconds: int = 60 * 60,
        *,
        on_evict: Callable[[str], Awaitable[None]] | None = None,
        key_prefix: str = "concierge:session:",
    ) -> None:
        if aioredis is None:
            raise RuntimeError("redis[asyncio] is required for RedisSessionManager")
        super().__init__(idle_ttl_seconds, on_evict=on_evict)
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._key_prefix = key_prefix

    def _key(self, session_id: str) -> str:
        return f"{self._key_prefix}{session_id}"

    async def create(
        self, *, tenant_id: str = "default", auth_subject: str | None = None
    ) -> Session:
        s = Session(tenant_id=tenant_id, auth_subject=auth_subject)
        await self._redis.set(
            self._key(s.session_id),
            s.model_dump_json(),
            ex=self.idle_ttl,
        )
        return s

    async def get(self, session_id: str) -> Session | None:
        data = await self._redis.get(self._key(session_id))
        if data is None:
            return None
        s = Session.model_validate_json(data)
        s.last_seen_at = datetime.now(UTC)
        # Refresh TTL and last_seen_at in Redis (idempotent, cheap).
        await self._redis.set(self._key(session_id), s.model_dump_json(), ex=self.idle_ttl)
        return s

    async def save(self, session: Session) -> None:
        """Write mutated session state back to Redis.

        Must be called after any mutation to published buckets or active_profiles
        because get() returns a freshly deserialised copy each time — in-place
        mutations are not visible to subsequent get() calls without an explicit save.
        """
        await self._redis.set(
            self._key(session.session_id),
            session.model_dump_json(),
            ex=self.idle_ttl,
        )

    async def close(self, session_id: str) -> None:
        existed = await self._redis.delete(self._key(session_id)) > 0
        if existed and self._on_evict is not None:
            await self._on_evict(session_id)

    async def all(self) -> list[Session]:
        keys: list[str] = []
        async for key in self._redis.scan_iter(match=f"{self._key_prefix}*"):
            keys.append(key)
        if not keys:
            return []
        data_list = await self._redis.mget(keys)
        return [Session.model_validate_json(d) for d in data_list if d is not None]

    async def gc(self) -> int:
        # Redis TTL handles expiry; this is a no-op.
        return 0

    async def close_redis(self) -> None:
        """Clean up the Redis connection pool on application shutdown."""
        await self._redis.close()
