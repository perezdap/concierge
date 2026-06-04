"""TDD tests for P1-8 CachingAdapter (per-tool TTL for idempotent reads).

Per assignment:
- New adapters/caching.py : decorator around UpstreamAdapter for reads.
- AC: cached read serves 2nd identical call without hitting upstream.
- Conservative: only for declared idempotent/read tools; TTL configurable; bypass on error.
"""

import asyncio
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.caching import CachingAdapter
from concierge.core.types import TransportType


class CountingFakeReadAdapter(UpstreamAdapter):
    transport = TransportType.STDIO
    server_id = "cache_test"

    def __init__(self) -> None:
        self.read_count = 0
        self._connected = True

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self):
        return []

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        self.read_count += 1
        await asyncio.sleep(0)  # simulate work
        return {"contents": [{"uri": uri, "text": "resource body for " + uri}]}

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    def health(self):
        class H:
            connected = True
        return H()


@pytest.mark.asyncio
async def test_caching_adapter_hits_cache_on_second_identical_read():
    """Core AC: 2nd identical read within TTL does not hit upstream (count stays 1)."""
    inner = CountingFakeReadAdapter()
    cached = CachingAdapter(inner, default_ttl_s=60)

    r1 = await cached.read_resource("file://test.txt")
    r2 = await cached.read_resource("file://test.txt")

    assert inner.read_count == 1, "second read must be cached, not hit upstream"
    assert r1 == r2


@pytest.mark.asyncio
async def test_caching_adapter_bypasses_after_ttl_expires():
    """After TTL, next read hits upstream again."""
    inner = CountingFakeReadAdapter()
    cached = CachingAdapter(inner, default_ttl_s=0.01)  # short for test

    await cached.read_resource("file://expiring.txt")
    await asyncio.sleep(0.05)
    await cached.read_resource("file://expiring.txt")

    assert inner.read_count == 2
