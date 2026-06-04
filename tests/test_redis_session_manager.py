"""Unit tests for Redis-backed session manager (P1-2).

Skipped automatically when redis is not installed or the server is unreachable.
"""
from __future__ import annotations

import asyncio

import pytest

from concierge.core.session import RedisSessionManager

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def redis_url():
    try:
        import redis.asyncio as aioredis
    except ImportError:
        pytest.skip("redis[asyncio] not installed")

    url = "redis://localhost:6379/0"
    # Probe connectivity
    async def _probe():
        r = aioredis.from_url(url, decode_responses=True)
        try:
            await r.ping()
        except Exception:
            pytest.skip("redis server not reachable")
        finally:
            await r.close()

    asyncio.run(_probe())
    return url


async def test_create_and_get(redis_url: str):
    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60)
    s = await sm.create(tenant_id="t1", auth_subject="u1")
    assert s.tenant_id == "t1"
    assert s.auth_subject == "u1"

    got = await sm.get(s.session_id)
    assert got is not None
    assert got.session_id == s.session_id
    assert got.tenant_id == "t1"

    # Clean up
    await sm.close(s.session_id)
    await sm.close_redis()


async def test_get_updates_last_seen_and_refreshes_ttl(redis_url: str):
    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60)
    s = await sm.create()
    original_last_seen = s.last_seen_at

    await asyncio.sleep(0.05)
    got = await sm.get(s.session_id)
    assert got is not None
    assert got.last_seen_at > original_last_seen

    await sm.close(s.session_id)
    await sm.close_redis()


async def test_close_removes_session(redis_url: str):
    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60)
    s = await sm.create()
    await sm.close(s.session_id)
    assert await sm.get(s.session_id) is None
    await sm.close_redis()


async def test_all_returns_sessions(redis_url: str):
    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60)
    s1 = await sm.create()
    s2 = await sm.create()

    all_sessions = await sm.all()
    ids = {s.session_id for s in all_sessions}
    assert s1.session_id in ids
    assert s2.session_id in ids

    await sm.close(s1.session_id)
    await sm.close(s2.session_id)
    await sm.close_redis()


async def test_gc_is_noop(redis_url: str):
    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60)
    n = await sm.gc()
    assert n == 0
    await sm.close_redis()


async def test_evict_hook_fired_on_close(redis_url: str):
    evicted: list[str] = []

    async def on_evict(sid: str) -> None:
        evicted.append(sid)

    sm = RedisSessionManager(redis_url, idle_ttl_seconds=60, on_evict=on_evict)
    s = await sm.create()
    await sm.close(s.session_id)
    assert evicted == [s.session_id]
    await sm.close_redis()
