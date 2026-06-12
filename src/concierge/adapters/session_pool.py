"""
Per-session upstream adapter pool.

Owned by AdapterManager for upstreams registered with isolation="per_session".
The pool provides LRU-bounded, lazily-connected, router-session-scoped adapters,
with safe concurrent creation (race-to-connect yields a single instance),
duplicate-connect discard, LRU eviction on capacity, and targeted teardown
per router session.

Extracted as Slice 2 of task #4 (Thin AdapterManager).
"""

from __future__ import annotations

import asyncio
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..util.audit import AuditLogger

from ..errors import GatewayError
from ..util.log import get_logger
from .base import UpstreamAdapter

_log = get_logger("concierge.adapter.session_pool")


@dataclass
class _PooledSession:
    """One lazily-created, isolated upstream session for a router session."""
    adapter: UpstreamAdapter
    last_used: datetime


class SessionPool:
    """
    Manages pooled per-(server_id, router_session_id) UpstreamAdapters.

    Responsibilities:
    - resolve() returns a connected adapter for the key, creating lazily on miss.
    - On concurrent misses for the same key, only one connect attempt wins; others
      observe the already-inserted adapter (duplicate connect is closed).
    - LRU eviction when the pool exceeds max_upstream_sessions.
    - evict_router_session(router_session_id) tears down exactly the adapters
      participating in that router session (used on session teardown).
    - drain() cancels in-flight creations and closes all pooled adapters (shutdown).
    """

    def __init__(
        self,
        *,
        max_upstream_sessions: int = 256,
        audit: AuditLogger | None = None,
    ) -> None:
        self.max_upstream_sessions = max_upstream_sessions
        self._audit = audit
        self._pool: OrderedDict[tuple[str, str], _PooledSession] = OrderedDict()
        self._pool_pending: dict[tuple[str, str], asyncio.Task[UpstreamAdapter]] = {}
        self._pool_lock = asyncio.Lock()

    async def resolve(
        self,
        *,
        server_id: str,
        router_session_id: str,
        factory: Callable[[], UpstreamAdapter],
        connect_max_retries: int,
        connect_backoff_base_s: float,
        connect_backoff_max_s: float,
    ) -> UpstreamAdapter:
        """Return (or create) the pooled upstream adapter for this router session."""
        key = (server_id, router_session_id)
        async with self._pool_lock:
            existing = self._pool.get(key)
            if existing is not None:
                existing.last_used = datetime.now(UTC)
                self._pool.move_to_end(key)
                return existing.adapter

            pending = self._pool_pending.get(key)
            if pending is None:
                adapter = factory()
                pending = asyncio.create_task(
                    self._create_pooled(
                        key,
                        adapter,
                        connect_max_retries=connect_max_retries,
                        connect_backoff_base_s=connect_backoff_base_s,
                        connect_backoff_max_s=connect_backoff_max_s,
                    ),
                    name=f"connect-{server_id}-{router_session_id}",
                )
                self._pool_pending[key] = pending

                def _forget(
                    task: asyncio.Task[UpstreamAdapter], k: tuple[str, str] = key
                ) -> None:
                    asyncio.create_task(self._forget_pending(k, task))

                pending.add_done_callback(_forget)

        return await asyncio.shield(pending)

    async def _forget_pending(
        self, key: tuple[str, str], task: asyncio.Task[UpstreamAdapter]
    ) -> None:
        async with self._pool_lock:
            if self._pool_pending.get(key) is task:
                self._pool_pending.pop(key, None)

    async def _create_pooled(
        self,
        key: tuple[str, str],
        adapter: UpstreamAdapter,
        *,
        connect_max_retries: int,
        connect_backoff_base_s: float,
        connect_backoff_max_s: float,
    ) -> UpstreamAdapter:
        """Connect (with backoff) then insert under lock; handle races and LRU victims."""
        server_id, router_session_id = key
        transferred = False
        to_close: UpstreamAdapter | None = None
        victims: list[tuple[str, str, UpstreamAdapter]] = []
        try:
            await self._connect_with_backoff(
                adapter,
                max_retries=connect_max_retries,
                base_s=connect_backoff_base_s,
                max_s=connect_backoff_max_s,
            )
            result = adapter
            async with self._pool_lock:
                existing = self._pool.get(key)
                if existing is not None:
                    existing.last_used = datetime.now(UTC)
                    self._pool.move_to_end(key)
                    to_close = adapter
                    result = existing.adapter
                else:
                    self._pool[key] = _PooledSession(adapter=adapter, last_used=datetime.now(UTC))
                    transferred = True
                    self._pool.move_to_end(key)
                    if self._audit is not None:
                        self._audit.upstream_session("created", server_id, router_session_id)
                    victims = self._pop_lru_over_cap_locked()

            if to_close is not None:
                await _safe_close(to_close)
                to_close = None
            await self._close_lru_victims(victims)
            victims = []
            return result
        except asyncio.CancelledError:
            if to_close is not None:
                await _safe_close(to_close)
            if not transferred:
                await _safe_close(adapter)
            await self._close_lru_victims(victims)
            raise
        except Exception:
            _log.exception("pooled adapter connect for %s/%s failed", server_id, router_session_id)
            if to_close is not None:
                await _safe_close(to_close)
            if not transferred:
                await _safe_close(adapter)
            await self._close_lru_victims(victims)
            raise

    def _pop_lru_over_cap_locked(self) -> list[tuple[str, str, UpstreamAdapter]]:
        """Caller must hold the pool lock."""
        victims: list[tuple[str, str, UpstreamAdapter]] = []
        while len(self._pool) > self.max_upstream_sessions:
            (sid, rsid), victim = self._pool.popitem(last=False)
            victims.append((sid, rsid, victim.adapter))
        return victims

    async def _close_lru_victims(self, victims: list[tuple[str, str, UpstreamAdapter]]) -> None:
        for sid, rsid, adapter in victims:
            await _safe_close(adapter)
            _log.info("evicted LRU upstream session %s/%s", sid, rsid)
            if self._audit is not None:
                self._audit.upstream_session("lru_evicted", sid, rsid)

    async def _enforce_pool_cap(self) -> None:
        """Public hook retained from original API for potential future use."""
        async with self._pool_lock:
            victims = self._pop_lru_over_cap_locked()
        await self._close_lru_victims(victims)

    async def evict_router_session(self, router_session_id: str) -> int:
        """Tear down all pooled sessions that belong to the given router session."""
        async with self._pool_lock:
            victim_keys = [k for k in self._pool if k[1] == router_session_id]
            victims: list[tuple[str, str, UpstreamAdapter]] = []
            for k in victim_keys:
                entry = self._pool.pop(k, None)
                if entry is not None:
                    victims.append((k[0], k[1], entry.adapter))
            pending_keys = [k for k in self._pool_pending if k[1] == router_session_id]
            pending = [self._pool_pending.pop(k) for k in pending_keys]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for sid, rsid, adapter in victims:
            await _safe_close(adapter)
            if self._audit is not None:
                self._audit.upstream_session("evicted", sid, rsid)
        return len(victims)

    async def drain(self) -> None:
        """Cancel pending creations and close all pooled adapters. Used at shutdown."""
        async with self._pool_lock:
            pending = list(self._pool_pending.values())
            self._pool_pending.clear()
            pooled = [e.adapter for e in self._pool.values()]
            self._pool.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for adapter in pooled:
            await _safe_close(adapter)

    def size(self) -> int:
        return len(self._pool)

    async def _connect_with_backoff(
        self,
        adapter: UpstreamAdapter,
        *,
        max_retries: int,
        base_s: float,
        max_s: float,
    ) -> None:
        """Backoff + jitter around connect + initialize. Identical policy to prior inline impl."""
        attempt = 0
        while True:
            try:
                await adapter.connect()
                await adapter.initialize()
                return
            except GatewayError as e:
                attempt += 1
                if attempt > max_retries:
                    await _safe_close(adapter)
                    raise
                backoff = min(max_s, base_s * (2 ** (attempt - 1)))
                delay = backoff * (0.5 + secrets.randbelow(500_001) / 1_000_000)
                _log.warning(
                    "connect %s failed (attempt %d/%d), retrying in %.2fs: %s",
                    adapter.server_id, attempt, max_retries, delay, e,
                )
                await asyncio.sleep(delay)


async def _safe_close(adapter: UpstreamAdapter) -> None:
    try:
        await adapter.close()
    except Exception as e:  # noqa: BLE001
        _log.debug("ignored error closing pooled adapter %s: %s", adapter.server_id, e)
