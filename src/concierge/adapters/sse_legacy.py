"""
Legacy HTTP+SSE adapter.

The pre-Streamable-HTTP transport pattern:
 - GET <sse_url> opens an SSE stream; the server's first SSE event carries an
   `endpoint` URL that the client must POST messages to.
 - All client→server JSON-RPC requests are POSTed to that endpoint.
 - All server→client responses + notifications arrive on the SSE stream.

We bridge that pattern back to the unified UpstreamAdapter interface.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from ..core.types import AdapterHealth, TransportType
from ..errors import (
    UpstreamProtocolError,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from ..util.log import get_logger
from ._jsonrpc import build_notification, build_request, unwrap_result
from .base import UpstreamAdapter

if TYPE_CHECKING:
    from .auth_headers import AuthHeaderProvider

_log = get_logger("concierge.adapter.sse_legacy")


class LegacySseAdapter(UpstreamAdapter):
    transport = TransportType.SSE_LEGACY

    def __init__(
        self,
        server_id: str,
        sse_url: str,
        *,
        headers: dict[str, str] | None = None,
        request_timeout_s: float = 30.0,
        post_url: str | None = None,
        auth_header_provider: AuthHeaderProvider | None = None,
    ) -> None:
        self.server_id = server_id
        self.sse_url = sse_url
        self.headers = headers or {}
        self.request_timeout_s = request_timeout_s
        self._post_url_override = post_url
        self._auth_header_provider = auth_header_provider

        self._client: httpx.AsyncClient | None = None
        self._post_url: str | None = post_url
        self._stream_task: asyncio.Task[None] | None = None
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._endpoint_ready = asyncio.Event()
        self._change_q: asyncio.Queue[str] = asyncio.Queue()
        self._connected = False
        self._last_error: str | None = None
        self._last_connected_at: datetime | None = None
        self._failures = 0

    async def connect(self) -> None:
        # Bound connect/write/pool so a stuck upstream can't hang the adapter,
        # but keep read=None: the SSE GET stream is long-lived by design and
        # must not be cut off by a read timeout.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        )
        self._stream_task = asyncio.create_task(self._sse_loop(), name=f"sse-{self.server_id}")
        if self._post_url is None:
            # Wait for the upstream to send the `endpoint` event.
            try:
                await asyncio.wait_for(self._endpoint_ready.wait(), timeout=10)
            except TimeoutError:
                await self.close()
                raise UpstreamUnavailable("never received endpoint event")
        else:
            self._endpoint_ready.set()
        self._connected = True
        self._last_connected_at = datetime.now(UTC)
        self._failures = 0

    async def close(self) -> None:
        self._connected = False
        if self._stream_task:
            self._stream_task.cancel()
            self._stream_task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(UpstreamUnavailable("adapter closed"))
        self._pending.clear()

    @staticmethod
    async def _iter_sse_events(resp: Any) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(event, data)`` pairs from an SSE stream, buffered across chunks.

        ``aiter_text()`` yields arbitrary network chunks, so a single ``data:``
        or ``event:`` line can be split across two chunks. We accumulate the
        buffer and only consume a line once its terminating newline has been
        seen, reassembling a payload split mid-line instead of truncating it. A
        blank line resets the current event type to the default ``"message"``
        (standard SSE dispatch semantics).
        """
        buffer = ""
        current_event = "message"
        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    current_event = "message"
                elif line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    yield current_event, line[len("data:"):].strip()
        # Flush a trailing data line that arrived without a terminating newline.
        tail = buffer.rstrip("\r\n")
        if tail.startswith("data:"):
            yield current_event, tail[len("data:"):].strip()

    async def _sse_loop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            headers = {"Accept": "text/event-stream", **(await self._auth_headers())}
            async with client.stream("GET", self.sse_url, headers=headers) as resp:
                if resp.status_code >= 400:
                    self._last_error = f"SSE connect failed: {resp.status_code}"
                    return
                async for event, raw in self._iter_sse_events(resp):
                    if event == "endpoint":
                        self._post_url = self._resolve_endpoint(raw)
                        self._endpoint_ready.set()
                    else:
                        await self._handle_data(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log.warning("legacy sse %s stream error: %s", self.server_id, e)
        finally:
            self._connected = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(UpstreamUnavailable("sse stream closed"))
            self._pending.clear()

    async def _auth_headers(self) -> dict[str, str]:
        """Static config headers plus dynamic auth headers (auth wins)."""
        h = dict(self.headers)
        if self._auth_header_provider is not None:
            h.update(await self._auth_header_provider.headers(self.server_id))
        return h

    def _resolve_endpoint(self, raw: str) -> str:
        # The endpoint can be absolute or relative to the SSE URL host.
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        # Strip path from sse_url to form base.
        from urllib.parse import urljoin
        return urljoin(self.sse_url, raw)

    async def _handle_data(self, raw: str) -> None:
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict) and "id" in msg and msg["id"] in self._pending:
            self._pending.pop(msg["id"]).set_result(msg)
            return
        method = msg.get("method") if isinstance(msg, dict) else None
        if method == "notifications/tools/list_changed":
            self._change_q.put_nowait("tools")
        elif method == "notifications/resources/list_changed":
            self._change_q.put_nowait("resources")
        elif method == "notifications/prompts/list_changed":
            self._change_q.put_nowait("prompts")

    async def _post(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._connected or not self._client or not self._post_url:
            raise UpstreamUnavailable(self.server_id)
        req = build_request(method, params)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req["id"]] = fut
        try:
            resp = await self._client.post(
                self._post_url, json=req, headers=await self._auth_headers(),
                timeout=self.request_timeout_s,
            )
            if resp.status_code >= 400:
                self._pending.pop(req["id"], None)
                self._failures += 1
                raise UpstreamUnavailable(f"POST {self._post_url}: HTTP {resp.status_code}")
        except httpx.TimeoutException:
            self._pending.pop(req["id"], None)
            self._failures += 1
            raise UpstreamTimeout(f"{self.server_id}.{method}")
        except httpx.HTTPError as e:
            self._pending.pop(req["id"], None)
            self._failures += 1
            self._connected = False
            raise UpstreamUnavailable(str(e)) from e

        try:
            response = await asyncio.wait_for(fut, timeout=self.request_timeout_s)
        except TimeoutError:
            self._pending.pop(req["id"], None)
            self._failures += 1
            raise UpstreamTimeout(f"{self.server_id}.{method}")
        return unwrap_result(response)

    async def _post_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._client or not self._post_url:
            return
        try:
            await self._client.post(
                self._post_url, json=build_notification(method, params),
                headers=await self._auth_headers(), timeout=self.request_timeout_s,
            )
        except httpx.HTTPError:
            pass

    # ------------------------------------------------------------------
    async def initialize(self) -> dict[str, Any]:
        result = await self._post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"roots": {}, "sampling": {}},
            "clientInfo": {"name": "concierge", "version": "0.1.0"},
        })
        await self._post_notification("notifications/initialized")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._post("tools/list")
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    async def list_resources(self) -> list[dict[str, Any]]:
        try:
            result = await self._post("resources/list")
        except UpstreamProtocolError:
            return []
        return list(result.get("resources", [])) if isinstance(result, dict) else []

    async def list_prompts(self) -> list[dict[str, Any]]:
        try:
            result = await self._post("prompts/list")
        except UpstreamProtocolError:
            return []
        return list(result.get("prompts", [])) if isinstance(result, dict) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._post("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {"content": []}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._post("resources/read", {"uri": uri})
        return result if isinstance(result, dict) else {}

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await self._post("prompts/get", {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {}

    async def list_changed_events(self) -> AsyncIterator[str]:
        while True:
            yield await self._change_q.get()

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            server_id=self.server_id,
            transport=self.transport,
            connected=self._connected,
            last_error=self._last_error,
            last_connected_at=self._last_connected_at,
            consecutive_failures=self._failures,
        )
