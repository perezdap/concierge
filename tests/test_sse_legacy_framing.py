"""Legacy SSE adapter message framing (unit-level, no network)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from concierge.adapters.sse_legacy import LegacySseAdapter


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
