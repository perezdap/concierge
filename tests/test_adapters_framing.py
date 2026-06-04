"""Adapter framing and circuit-breaker tests with fakes (no real upstream)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from concierge.adapters.manager import AdapterManager, _CircuitBreaker
from concierge.adapters.stdio import StdioAdapter
from concierge.adapters.streamable_http import StreamableHttpAdapter
from concierge.core.catalog import Catalog
from concierge.core.types import TransportType
from concierge.errors import UpstreamCircuitOpen, UpstreamUnavailable
from tests.test_discovery import MultiToolAdapter


def test_circuit_breaker_opens_after_threshold():
    cb = _CircuitBreaker(failure_threshold=3, cooldown_s=60.0)
    assert not cb.is_open("srv")
    cb.record_failure("srv")
    cb.record_failure("srv")
    assert not cb.is_open("srv")
    cb.record_failure("srv")
    assert cb.is_open("srv")


def test_circuit_breaker_closes_after_cooldown():
    cb = _CircuitBreaker(failure_threshold=1, cooldown_s=0.01)
    cb.record_failure("srv")
    assert cb.is_open("srv")
    until = datetime.now(UTC) + timedelta(seconds=1)
    with patch("concierge.adapters.manager.datetime") as mock_dt:
        mock_dt.now.return_value = until
        assert not cb.is_open("srv")


def test_circuit_breaker_success_resets_failures():
    cb = _CircuitBreaker(failure_threshold=2, cooldown_s=60.0)
    cb.record_failure("srv")
    cb.record_success("srv")
    cb.record_failure("srv")
    assert not cb.is_open("srv")


@pytest.mark.asyncio
async def test_manager_call_tool_opens_circuit_on_transport_failures():
    class FailingAdapter(MultiToolAdapter):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise UpstreamUnavailable("upstream down")

    catalog = Catalog()
    mgr = AdapterManager(catalog, circuit_failure_threshold=2, circuit_cooldown_s=120.0)
    mgr.register(FailingAdapter("demo"))
    await mgr.refresh_server("demo")

    with pytest.raises(UpstreamUnavailable):
        await mgr.call_tool("demo", "alpha", {})
    with pytest.raises(UpstreamUnavailable):
        await mgr.call_tool("demo", "alpha", {})
    with pytest.raises(UpstreamCircuitOpen):
        await mgr.call_tool("demo", "alpha", {})


@pytest.mark.asyncio
async def test_stdio_dispatch_resolves_pending_request():
    adapter = StdioAdapter("t", ["python", "-c", "pass"])
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    adapter._pending[42] = fut
    await adapter._dispatch({"jsonrpc": "2.0", "id": 42, "result": {"tools": []}})
    msg = await asyncio.wait_for(fut, timeout=1.0)
    assert msg["result"] == {"tools": []}


@pytest.mark.asyncio
async def test_stdio_dispatch_tools_list_changed_notification():
    adapter = StdioAdapter("t", ["python", "-c", "pass"])
    await adapter._dispatch({
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    })
    kind = await asyncio.wait_for(adapter._change_q.get(), timeout=1.0)
    assert kind == "tools"


@pytest.mark.asyncio
async def test_streamable_http_read_first_sse_message_single_chunk():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    class FakeResp:
        async def aiter_text(self):
            yield f"event: message\ndata: {json.dumps(payload)}\n\n"

    result = await StreamableHttpAdapter._read_first_sse_message(FakeResp())  # type: ignore[arg-type]
    assert result["result"]["ok"] is True


@pytest.mark.asyncio
async def test_streamable_http_read_first_sse_message_whole_line_in_one_chunk():
    payload = {"jsonrpc": "2.0", "id": 2, "result": {"n": 3}}

    class FakeResp:
        async def aiter_text(self):
            yield f"data: {json.dumps(payload)}\n\n"

    result = await StreamableHttpAdapter._read_first_sse_message(FakeResp())  # type: ignore[arg-type]
    assert result["id"] == 2


@pytest.mark.asyncio
async def test_streamable_http_read_first_sse_message_split_mid_data_line():
    payload = {"jsonrpc": "2.0", "id": 3, "result": {"split": True}}
    raw = json.dumps(payload)
    mid = len(raw) // 2

    class FakeResp:
        async def aiter_text(self):
            yield f"data: {raw[:mid]}"
            yield f"{raw[mid:]}\n\n"

    result = await StreamableHttpAdapter._read_first_sse_message(FakeResp())  # type: ignore[arg-type]
    assert result["result"]["split"] is True


@pytest.mark.asyncio
async def test_stdio_health_reflects_connection_state():
    adapter = StdioAdapter("t", ["python", "-c", "pass"])
    h = adapter.health()
    assert h.server_id == "t"
    assert h.transport == TransportType.STDIO
    assert h.connected is False
