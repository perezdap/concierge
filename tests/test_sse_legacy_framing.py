"""Legacy SSE adapter message framing and request plumbing (unit-level, no network)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from concierge.adapters import sse_legacy
from concierge.adapters.sse_legacy import LegacySseAdapter
from concierge.core.types import TransportType
from concierge.errors import UpstreamTimeout, UpstreamUnavailable


@pytest.mark.asyncio
async def test_handle_data_resolves_pending_jsonrpc():
    adapter = LegacySseAdapter("srv", "http://upstream/sse", post_url="http://upstream/post")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    adapter._pending[7] = fut
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}
    await adapter._handle_data(json.dumps(payload))
    assert (await asyncio.wait_for(fut, timeout=1.0))["result"] == {"tools": []}


@pytest.mark.asyncio
async def test_handle_data_tools_list_changed_notification():
    adapter = LegacySseAdapter("srv", "http://upstream/sse", post_url="http://upstream/post")
    await adapter._handle_data(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    }))
    assert await asyncio.wait_for(adapter._change_q.get(), timeout=1.0) == "tools"


def test_resolve_endpoint_absolute_url():
    adapter = LegacySseAdapter("srv", "http://host/base/sse")
    assert adapter._resolve_endpoint("http://other/post") == "http://other/post"


def test_resolve_endpoint_relative_url():
    adapter = LegacySseAdapter("srv", "http://host/base/sse")
    assert adapter._resolve_endpoint("/message").endswith("/message")


# --- cross-chunk SSE framing (P0-5) ---------------------------------------
#
# `aiter_text()` yields arbitrary network chunks; a single `data:`/`event:`
# line can be split across two chunks. `_iter_sse_events` must buffer until a
# newline so the payload is reassembled rather than truncated/dropped.


class _FakeResp:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def aiter_text(self):
        for c in self._chunks:
            yield c


async def _collect(chunks: list[str]) -> list[tuple[str, str]]:
    return [ev async for ev in LegacySseAdapter._iter_sse_events(_FakeResp(chunks))]


@pytest.mark.asyncio
async def test_iter_sse_events_single_chunk_message():
    events = await _collect(['event: message\ndata: {"a": 1}\n\n'])
    assert events == [("message", '{"a": 1}')]


@pytest.mark.asyncio
async def test_iter_sse_events_parses_endpoint_event():
    events = await _collect(["event: endpoint\ndata: /messages/abc\n\n"])
    assert events == [("endpoint", "/messages/abc")]


@pytest.mark.asyncio
async def test_iter_sse_events_reassembles_data_line_split_mid_payload():
    raw = json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"split": True}})
    mid = len(raw) // 2
    events = await _collect([f"event: message\ndata: {raw[:mid]}", f"{raw[mid:]}\n\n"])
    assert events == [("message", raw)]


@pytest.mark.asyncio
async def test_iter_sse_events_reassembles_event_line_split_across_chunks():
    events = await _collect(["event: mess", 'age\ndata: {"x": 2}\n\n'])
    assert events == [("message", '{"x": 2}')]


@pytest.mark.asyncio
async def test_iter_sse_events_flushes_trailing_data_without_newline():
    events = await _collect(["data: {\"end\": true}"])
    assert events == [("message", '{"end": true}')]


@pytest.mark.asyncio
async def test_iter_sse_events_split_data_dispatches_to_handle_data():
    """End-to-end: a JSON-RPC response split mid-line still resolves its future."""
    adapter = LegacySseAdapter("srv", "http://upstream/sse", post_url="http://upstream/post")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    adapter._pending[9] = fut
    raw = json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"ok": True}})
    mid = len(raw) // 2
    async for event, data in LegacySseAdapter._iter_sse_events(
        _FakeResp([f"data: {raw[:mid]}", f"{raw[mid:]}\n\n"])
    ):
        if event != "endpoint":
            await adapter._handle_data(data)
    assert (await asyncio.wait_for(fut, timeout=1.0))["result"] == {"ok": True}


# --- request/response plumbing (POST → matched on SSE) --------------------


class _LoopbackClient:
    """Fake httpx client: a POSTed request is answered on the SSE stream by
    resolving the adapter's pending future with ``result`` (matched by id)."""

    def __init__(self, adapter: LegacySseAdapter, result: dict[str, Any]) -> None:
        self._adapter = adapter
        self._result = result
        self.posted: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any], headers=None, timeout=None) -> httpx.Response:
        self.posted.append(json)
        rid = json.get("id")
        if rid is not None:
            await self._adapter._handle_data(
                __import__("json").dumps({"jsonrpc": "2.0", "id": rid, "result": self._result})
            )
        return httpx.Response(200)

    async def aclose(self) -> None:
        pass


class _RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def post(self, *a, **k):
        raise self._exc

    async def aclose(self) -> None:
        pass


def _connected_adapter(client: Any) -> LegacySseAdapter:
    adapter = LegacySseAdapter("srv", "http://up/sse", post_url="http://up/post")
    adapter._connected = True
    adapter._client = client  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_delegation_methods_route_through_post():
    result = {
        "tools": [{"name": "a"}],
        "resources": [{"uri": "u"}],
        "prompts": [{"name": "p"}],
        "content": [{"type": "text", "text": "x"}],
    }
    adapter = _connected_adapter(None)
    adapter._client = _LoopbackClient(adapter, result)  # type: ignore[assignment]

    assert await adapter.initialize() == result
    assert await adapter.list_tools() == [{"name": "a"}]
    assert await adapter.list_resources() == [{"uri": "u"}]
    assert await adapter.list_prompts() == [{"name": "p"}]
    assert (await adapter.call_tool("a", {}))["content"][0]["text"] == "x"
    assert await adapter.read_resource("u") == result
    assert await adapter.get_prompt("p") == result


@pytest.mark.asyncio
async def test_post_raises_when_not_connected():
    adapter = LegacySseAdapter("srv", "http://up/sse", post_url="http://up/post")
    with pytest.raises(UpstreamUnavailable):
        await adapter.list_tools()


@pytest.mark.asyncio
async def test_post_maps_timeout_to_upstream_timeout():
    adapter = _connected_adapter(_RaisingClient(httpx.TimeoutException("slow")))
    with pytest.raises(UpstreamTimeout):
        await adapter.list_tools()
    assert adapter._failures == 1


@pytest.mark.asyncio
async def test_post_maps_http_error_to_unavailable():
    adapter = _connected_adapter(_RaisingClient(httpx.ConnectError("boom")))
    with pytest.raises(UpstreamUnavailable):
        await adapter.list_tools()
    assert adapter._connected is False


@pytest.mark.asyncio
async def test_post_error_status_is_unavailable():
    class _Status500:
        async def post(self, *a, **k):
            return httpx.Response(500)

        async def aclose(self):
            pass

    adapter = _connected_adapter(_Status500())
    with pytest.raises(UpstreamUnavailable):
        await adapter.list_tools()


@pytest.mark.asyncio
async def test_post_notification_swallows_errors():
    adapter = _connected_adapter(_RaisingClient(httpx.ConnectError("x")))
    # Notifications are fire-and-forget — a transport error must not propagate.
    await adapter._post_notification("notifications/initialized")


@pytest.mark.asyncio
async def test_list_changed_events_yields_queue_items():
    adapter = LegacySseAdapter("srv", "http://up/sse", post_url="http://up/post")
    adapter._change_q.put_nowait("tools")
    agen = adapter.list_changed_events()
    assert await asyncio.wait_for(agen.__anext__(), timeout=1.0) == "tools"


def test_health_reports_transport_and_state():
    adapter = LegacySseAdapter("srv", "http://up/sse")
    h = adapter.health()
    assert h.server_id == "srv"
    assert h.transport == TransportType.SSE_LEGACY
    assert h.connected is False


@pytest.mark.asyncio
async def test_close_aborts_pending_and_closes_client():
    closed = {"v": False}

    class _C:
        async def aclose(self):
            closed["v"] = True

    adapter = _connected_adapter(_C())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    adapter._pending[1] = fut
    await adapter.close()
    assert closed["v"] is True
    assert adapter._connected is False
    with pytest.raises(UpstreamUnavailable):
        await fut


# --- _sse_loop end-to-end (endpoint discovery + dispatch + error) ---------


class _StreamCM:
    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *a) -> bool:
        return False


class _StreamResp:
    def __init__(self, chunks: list[str], status: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status

    async def aiter_text(self):
        for c in self._chunks:
            yield c


class _StreamingClient:
    def __init__(self, resp: _StreamResp) -> None:
        self._resp = resp

    def stream(self, method: str, url: str, headers=None):
        return _StreamCM(self._resp)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_sse_loop_discovers_endpoint_and_dispatches_response(monkeypatch):
    resp = _StreamResp([
        "event: endpoint\ndata: http://up/messages\n\n",
        'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}\n\n',
    ])
    monkeypatch.setattr(sse_legacy.httpx, "AsyncClient", lambda *a, **k: _StreamingClient(resp))

    adapter = LegacySseAdapter("srv", "http://up/sse")  # no post_url → must learn it
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    adapter._pending[1] = fut  # _handle_data pops it on dispatch
    await adapter.connect()
    assert adapter._post_url == "http://up/messages"
    assert (await asyncio.wait_for(fut, timeout=1.0))["result"] == {"ok": 1}
    await adapter.close()


@pytest.mark.asyncio
async def test_sse_loop_records_error_status(monkeypatch):
    resp = _StreamResp([], status=503)
    monkeypatch.setattr(sse_legacy.httpx, "AsyncClient", lambda *a, **k: _StreamingClient(resp))

    adapter = LegacySseAdapter("srv", "http://up/sse", post_url="http://up/post")
    await adapter.connect()  # post_url set → connect does not wait on endpoint
    await asyncio.sleep(0)  # let the loop observe the bad status
    assert adapter._last_error and "503" in adapter._last_error
    await adapter.close()
