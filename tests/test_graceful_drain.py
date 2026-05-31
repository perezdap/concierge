"""Graceful-drain / rolling-restart tests (P1-7).

Proves that when the process begins draining on SIGTERM:

  * already-dispatched in-flight calls run to completion (zero dropped), and
  * the new-session path is refused with a retryable 503-equivalent, and
  * /readyz reports not-ready so the load balancer steers traffic away,

then the lifespan shutdown's ``wait_for_idle`` returns only once the in-flight
call has finished.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from concierge.config import AuthConfig, GatewayConfig, GatewayHttpConfig
from concierge.server.app import build_app
from concierge.server.lifecycle import DrainController

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "drain-test", "version": "0.0.1"},
    },
}


def _config() -> GatewayConfig:
    return GatewayConfig(
        auth=AuthConfig(type="none"),
        gateway=GatewayHttpConfig(allowed_origins=["http://localhost"]),
        upstream_servers=[],
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )


async def test_inflight_call_completes_while_new_sessions_get_503() -> None:
    app = build_app(_config())

    # Replace dispatch with a controllable slow call so we can hold one request
    # "in flight" across the drain boundary deterministically. The gate is
    # released only after we have begun draining, proving the in-flight call is
    # not interrupted by the drain.
    gate = asyncio.Event()
    started = asyncio.Event()
    real_dispatch = app.state.service.dispatch

    async def slow_dispatch(session, method, params):  # type: ignore[no-untyped-def]
        if method == "ping":
            started.set()
            await gate.wait()
            return {"slow": "done"}
        return await real_dispatch(session, method, params)

    app.state.service.dispatch = slow_dispatch  # type: ignore[method-assign]
    drain: DrainController = app.state.drain

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost"
    ) as http:
        # Establish a session before draining starts.
        init = await http.post("/mcp", json=_INIT, headers={"Origin": "http://localhost"})
        assert init.status_code == 200
        sid = init.headers["MCP-Session-Id"]

        # Fire a slow in-flight call on the live session.
        slow = asyncio.create_task(
            http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
                headers={"Origin": "http://localhost", "MCP-Session-Id": sid},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert drain.in_flight == 1

        # SIGTERM equivalent: begin draining while the call is still running.
        await drain.begin_drain()

        # Readiness must now report not-ready (503) so the LB stops new traffic.
        ready = await http.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["draining"] is True

        # A brand-new session (initialize, no session header) is refused 503.
        rejected = await http.post(
            "/mcp", json=_INIT, headers={"Origin": "http://localhost"}
        )
        assert rejected.status_code == 503
        assert rejected.headers.get("Retry-After") == "1"

        # The in-flight call must not have been dropped: shutdown would block on
        # it. Releasing the gate lets it finish successfully.
        assert not slow.done()
        gate.set()
        resp = await asyncio.wait_for(slow, timeout=2.0)
        assert resp.status_code == 200
        assert resp.json()["result"]["slow"] == "done"

        # Once the in-flight call drained, the counter is back to zero and the
        # shutdown wait returns True (fully drained) immediately.
        assert await drain.wait_for_idle(timeout_s=2.0) is True
        assert drain.in_flight == 0


async def test_wait_for_idle_times_out_when_call_hangs() -> None:
    drain = DrainController()
    await drain.acquire()
    await drain.begin_drain()
    # Nothing releases the in-flight slot, so the bounded wait must report
    # not-fully-drained rather than blocking forever.
    drained = await drain.wait_for_idle(timeout_s=0.05)
    assert drained is False
    assert drain.in_flight == 1


async def test_release_unblocks_wait_for_idle() -> None:
    drain = DrainController()
    await drain.acquire()

    async def _release_soon() -> None:
        await asyncio.sleep(0.02)
        await drain.release()

    asyncio.create_task(_release_soon())
    assert await drain.wait_for_idle(timeout_s=2.0) is True


@pytest.mark.parametrize("draining", [True, False])
async def test_readyz_reflects_drain_state(draining: bool) -> None:
    app = build_app(_config())
    drain: DrainController = app.state.drain
    if draining:
        await drain.begin_drain()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        resp = await http.get("/readyz")
    if draining:
        assert resp.status_code == 503
        assert resp.json()["draining"] is True
    else:
        # No upstreams configured → all() over empty list is True → ready.
        assert resp.status_code == 200
