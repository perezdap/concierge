"""Phase 1 — upstream session registry, pooling, eviction, and backoff."""
import asyncio
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.manager import AdapterManager
from concierge.core.catalog import Catalog
from concierge.core.session import SessionManager
from concierge.core.types import AdapterHealth, TransportType
from concierge.errors import UpstreamUnavailable


class FakeAdapter(UpstreamAdapter):
    transport = TransportType.STREAMABLE_HTTP

    def __init__(self, server_id: str, *, fail_connects: int = 0) -> None:
        self.server_id = server_id
        self._connected = False
        self.connect_attempts = 0
        self.fail_connects = fail_connects
        self.calls = 0
        self.closed = False

    async def connect(self) -> None:
        self.connect_attempts += 1
        if self.connect_attempts <= self.fail_connects:
            raise UpstreamUnavailable("transient")
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self.closed = True

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self):
        return []

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"content": [{"type": "text", "text": f"ok:{id(self)}"}]}

    async def read_resource(self, uri: str):
        return {}

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None):
        return {}

    def health(self) -> AdapterHealth:
        return AdapterHealth(server_id=self.server_id, transport=self.transport, connected=self._connected)


class BlockingConnectAdapter(FakeAdapter):
    def __init__(
        self,
        server_id: str,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(server_id)
        self.started = started
        self.release = release

    async def connect(self) -> None:
        self.connect_attempts += 1
        self.started.set()
        await self.release.wait()
        self._connected = True


class SignalingInitializeAdapter(FakeAdapter):
    def __init__(self, server_id: str, *, initialized: asyncio.Event) -> None:
        super().__init__(server_id)
        self.initialized = initialized

    async def initialize(self) -> dict[str, Any]:
        self.initialized.set()
        await asyncio.sleep(0)
        return {}


def _make_tracking_factory():
    made: list[FakeAdapter] = []

    def factory(**kw):
        a = FakeAdapter("demo", **kw)
        made.append(a)
        return a

    return made, factory


@pytest.mark.asyncio
async def test_shared_isolation_reuses_control_adapter():
    adapters = AdapterManager(Catalog())
    control = FakeAdapter("demo")
    control._connected = True
    adapters.register(control, isolation="shared", factory=lambda: FakeAdapter("demo"))

    await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    await adapters.call_tool("demo", "ping", {}, router_session_id="s2")

    # The single control adapter served both router sessions; pool unused.
    assert control.calls == 2
    assert adapters.pool_size() == 0


@pytest.mark.asyncio
async def test_per_session_isolation_pools_per_router_session():
    adapters = AdapterManager(Catalog())
    made, factory = _make_tracking_factory()
    control = FakeAdapter("demo")
    control._connected = True
    adapters.register(
        control, isolation="per_session", factory=factory, connect_backoff_base_s=0.001,
    )

    # Two calls in the same router session reuse one pooled adapter.
    await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    assert len(made) == 1
    assert made[0].calls == 2
    assert control.calls == 0          # control plane is not used for calls
    assert adapters.pool_size() == 1

    # A different router session gets its own isolated adapter.
    await adapters.call_tool("demo", "ping", {}, router_session_id="s2")
    assert len(made) == 2
    assert adapters.pool_size() == 2


@pytest.mark.asyncio
async def test_evict_router_session_tears_down_pool():
    adapters = AdapterManager(Catalog())
    made, factory = _make_tracking_factory()
    adapters.register(
        FakeAdapter("demo"), isolation="per_session", factory=factory, connect_backoff_base_s=0.001,
    )

    await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    pooled = made[0]

    n = await adapters.evict_router_session("s1")
    assert n == 1
    assert pooled.closed is True
    assert adapters.pool_size() == 0


@pytest.mark.asyncio
async def test_lru_eviction_past_cap():
    adapters = AdapterManager(Catalog(), max_upstream_sessions=2)
    made, factory = _make_tracking_factory()
    adapters.register(
        FakeAdapter("demo"), isolation="per_session", factory=factory, connect_backoff_base_s=0.001,
    )

    for sid in ("s1", "s2", "s3"):
        await adapters.call_tool("demo", "ping", {}, router_session_id=sid)

    assert adapters.pool_size() == 2
    assert made[0].closed is True       # oldest (s1) evicted
    assert made[1].closed is False
    assert made[2].closed is False


@pytest.mark.asyncio
async def test_connect_backoff_retries_transient_failure():
    adapters = AdapterManager(Catalog())
    created: dict[str, FakeAdapter] = {}

    def factory():
        a = FakeAdapter("demo", fail_connects=2)
        created["a"] = a
        return a

    adapters.register(
        FakeAdapter("demo"), isolation="per_session", factory=factory,
        connect_max_retries=3, connect_backoff_base_s=0.001, connect_backoff_max_s=0.01,
    )

    res = await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    assert created["a"].connect_attempts == 3   # 2 failures + 1 success
    assert created["a"].calls == 1
    assert res["content"][0]["text"].startswith("ok:")


@pytest.mark.asyncio
async def test_connect_backoff_gives_up_after_max_retries():
    adapters = AdapterManager(Catalog())

    def factory():
        return FakeAdapter("demo", fail_connects=99)

    adapters.register(
        FakeAdapter("demo"), isolation="per_session", factory=factory,
        connect_max_retries=2, connect_backoff_base_s=0.001, connect_backoff_max_s=0.01,
    )

    with pytest.raises(UpstreamUnavailable):
        await adapters.call_tool("demo", "ping", {}, router_session_id="s1")
    assert adapters.pool_size() == 0


@pytest.mark.asyncio
async def test_pending_connect_does_not_block_unrelated_session_creation():
    adapters = AdapterManager(Catalog())
    started = asyncio.Event()
    release = asyncio.Event()
    made: list[FakeAdapter] = []

    def factory():
        if not made:
            adapter = BlockingConnectAdapter("demo", started=started, release=release)
        else:
            adapter = FakeAdapter("demo")
        made.append(adapter)
        return adapter

    adapters.register(FakeAdapter("demo"), isolation="per_session", factory=factory)

    first = asyncio.create_task(adapters.call_tool("demo", "ping", {}, router_session_id="s1"))
    await started.wait()

    second = await asyncio.wait_for(
        adapters.call_tool("demo", "ping", {}, router_session_id="s2"),
        timeout=0.1,
    )
    assert second["content"][0]["text"].startswith("ok:")

    release.set()
    await first
    assert len(made) == 2


@pytest.mark.asyncio
async def test_evict_router_session_cancels_pending_connect_without_blocking():
    adapters = AdapterManager(Catalog())
    started = asyncio.Event()
    release = asyncio.Event()

    def factory():
        return BlockingConnectAdapter("demo", started=started, release=release)

    adapters.register(FakeAdapter("demo"), isolation="per_session", factory=factory)

    call = asyncio.create_task(adapters.call_tool("demo", "ping", {}, router_session_id="s1"))
    await started.wait()

    freed = await asyncio.wait_for(adapters.evict_router_session("s1"), timeout=0.1)
    assert freed == 0
    with pytest.raises(asyncio.CancelledError):
        await call


@pytest.mark.asyncio
async def test_cancelled_pending_publish_closes_connected_adapter():
    adapters = AdapterManager(Catalog())
    initialized = asyncio.Event()
    made: list[SignalingInitializeAdapter] = []

    def factory():
        adapter = SignalingInitializeAdapter("demo", initialized=initialized)
        made.append(adapter)
        return adapter

    adapters.register(FakeAdapter("demo"), isolation="per_session", factory=factory)

    call = asyncio.create_task(adapters.call_tool("demo", "ping", {}, router_session_id="s1"))
    await initialized.wait()
    await adapters._pool_lock.acquire()
    try:
        await asyncio.sleep(0)
        pending = next(iter(adapters._pool_pending.values()))
        pending.cancel()
    finally:
        adapters._pool_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await call
    assert made[0].closed is True
    assert adapters.pool_size() == 0


@pytest.mark.asyncio
async def test_session_gc_invokes_on_evict():
    evicted: list[str] = []

    async def on_evict(sid: str) -> None:
        evicted.append(sid)

    sm = SessionManager(idle_ttl_seconds=-1, on_evict=on_evict)
    s = await sm.create()

    n = await sm.gc()
    assert n == 1
    assert evicted == [s.session_id]


@pytest.mark.asyncio
async def test_aclose_invokes_on_evict_once():
    evicted: list[str] = []

    async def on_evict(sid: str) -> None:
        evicted.append(sid)

    sm = SessionManager(on_evict=on_evict)
    s = await sm.create()

    await sm.aclose(s.session_id)
    await sm.aclose(s.session_id)        # idempotent — no second fire
    await sm.aclose("does-not-exist")    # unknown — no fire
    assert evicted == [s.session_id]


@pytest.mark.asyncio
async def test_close_invokes_on_evict_and_removes_session():
    evicted: list[str] = []

    async def on_evict(sid: str) -> None:
        evicted.append(sid)

    sm = SessionManager(on_evict=on_evict)
    s = await sm.create()

    await sm.close(s.session_id)
    assert evicted == [s.session_id]
    assert await sm.get(s.session_id) is None
