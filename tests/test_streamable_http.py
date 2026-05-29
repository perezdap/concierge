"""Streamable HTTP adapter tests with mocked httpx (no real upstream)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from concierge.adapters.streamable_http import StreamableHttpAdapter
from concierge.errors import UpstreamTimeout, UpstreamUnavailable


@pytest.mark.asyncio
async def test_connect_and_close():
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp")
    await adapter.connect()
    assert adapter._connected
    await adapter.close()
    assert not adapter._connected


@pytest.mark.asyncio
async def test_post_json_response():
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp", listen_for_notifications=False)
    await adapter.connect()
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": "t"}]},
    }
    adapter._client.post = AsyncMock(return_value=mock_resp)
    tools = await adapter.list_tools()
    assert tools[0]["name"] == "t"


@pytest.mark.asyncio
async def test_post_sse_response():
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp", listen_for_notifications=False)
    await adapter.connect()
    payload = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}

    class SseResp:
        headers = {"Content-Type": "text/event-stream"}

        async def aiter_text(self):
            yield f"data: {json.dumps(payload)}\n\n"

    adapter._client.post = AsyncMock(return_value=SseResp())
    result = await adapter._post("initialize", {})
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_post_sse_response_data_split_across_three_chunks():
    """A data: line fragmented across several network chunks is reassembled."""
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp", listen_for_notifications=False)
    await adapter.connect()
    raw = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"deep": {"v": 42}}})
    a, b = len(raw) // 3, 2 * (len(raw) // 3)

    class SseResp:
        headers = {"Content-Type": "text/event-stream"}

        async def aiter_text(self):
            yield f"data: {raw[:a]}"
            yield raw[a:b]
            yield f"{raw[b:]}\n\n"

    adapter._client.post = AsyncMock(return_value=SseResp())
    result = await adapter._post("initialize", {})
    assert result["deep"]["v"] == 42


@pytest.mark.asyncio
async def test_post_unavailable_when_not_connected():
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp")
    with pytest.raises(UpstreamUnavailable):
        await adapter._post("tools/list")


@pytest.mark.asyncio
async def test_post_timeout_increments_failures():
    adapter = StreamableHttpAdapter("srv", "http://upstream/mcp", request_timeout_s=0.01)
    await adapter.connect()
    adapter._client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(UpstreamTimeout):
        await adapter._post("tools/list")
    assert adapter._failures >= 1
