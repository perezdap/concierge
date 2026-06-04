"""
CachingAdapter decorator for idempotent upstream read operations (resources/prompts).

P1-8: wraps an UpstreamAdapter; for read_resource (and optionally prompts) serves
from in-memory TTL cache on identical args, avoiding repeated upstream hits.

Conservative: opt-in per instance; only caches successful reads; bypasses on error;
no caching for call_tool (side-effecting by default).

Mirrors project style: small, explicit, no heavy deps.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .base import UpstreamAdapter


class CachingAdapter(UpstreamAdapter):
    """TTL cache decorator for read-heavy upstream calls. Safe defaults (no cache for writes)."""

    def __init__(
        self,
        inner: UpstreamAdapter,
        *,
        default_ttl_s: float = 300.0,
        cache_reads: bool = True,
        cache_prompts: bool = False,
    ) -> None:
        self.inner = inner
        self.default_ttl = default_ttl_s
        self.cache_reads = cache_reads
        self.cache_prompts = cache_prompts
        self.server_id = inner.server_id
        self.transport = inner.transport
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}  # (op, key) -> (expiry, value)
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()

    async def initialize(self) -> dict[str, Any]:
        return await self.inner.initialize()

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self.inner.list_tools()

    async def list_resources(self):
        return await self.inner.list_resources()

    async def list_prompts(self):
        return await self.inner.list_prompts()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Never cache side-effecting calls
        return await self.inner.call_tool(name, arguments)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        if not self.cache_reads:
            return await self.inner.read_resource(uri)
        key = ("read", uri)
        async with self._lock:
            now = time.monotonic()
            if key in self._cache:
                exp, val = self._cache[key]
                if now < exp:
                    return val  # cache hit, no upstream
                else:
                    self._cache.pop(key, None)
        # miss or expired -> fetch
        val = await self.inner.read_resource(uri)
        async with self._lock:
            self._cache[key] = (time.monotonic() + self.default_ttl, val)
        return val

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.cache_prompts:
            return await self.inner.get_prompt(name, arguments)
        key = (
            "prompt",
            f"{name}:{hash(frozenset((arguments or {}).items())) if arguments else ''}",
        )
        async with self._lock:
            now = time.monotonic()
            if key in self._cache:
                exp, val = self._cache[key]
                if now < exp:
                    return val
                self._cache.pop(key, None)
        val = await self.inner.get_prompt(name, arguments)
        async with self._lock:
            self._cache[key] = (time.monotonic() + self.default_ttl, val)
        return val

    def health(self):
        return self.inner.health()
