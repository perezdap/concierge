"""
Streamable HTTP upstream adapter.

The current ("Streamable HTTP") MCP transport uses a single endpoint that:
 - accepts JSON-RPC requests via POST,
 - returns either a JSON response or a streaming response,
 - supports an optional GET that opens a server-initiated SSE stream for
   notifications (e.g. tools/list_changed).

This adapter speaks the synchronous request/response half of that contract
plus an optional SSE listener for list_changed notifications.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

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

_log = get_logger("concierge.adapter.streamable_http")


class StreamableHttpAdapter(UpstreamAdapter):
    transport = TransportType.STREAMABLE_HTTP

    def __init__(
        self,
        server_id: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        request_timeout_s: float = 30.0,
        listen_for_notifications: bool = True,
    ) -> None:
        self.server_id = server_id
        self.url = url
        self.base_headers = headers or {}
        self.request_timeout_s = request_timeout_s
        self.listen_for_notifications = listen_for_notifications

        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._listener: asyncio.Task[None] | None = None
        self._change_q: asyncio.Queue[str] = asyncio.Queue()

        self._connected = False
        self._last_error: str | None = None
        self._last_connected_at: datetime | None = None
        self._failures = 0

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
        self._connected = True
        self._last_connected_at = datetime.now(timezone.utc)
        self._failures = 0

    async def close(self) -> None:
        self._connected = False
        if self._listener:
            self._listener.cancel()
            self._listener = None
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.base_headers,
        }
        if self._session_id:
            h["MCP-Session-Id"] = self._session_id
        return h

    async def _post(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._connected or not self._client:
            raise UpstreamUnavailable(self.server_id)
        req = build_request(method, params)
        try:
            resp = await self._client.post(self.url, json=req, headers=self._headers())
        except httpx.TimeoutException:
            self._failures += 1
            raise UpstreamTimeout(f"{self.server_id}.{method}")
        except httpx.HTTPError as e:
            self._failures += 1
            self._connected = False
            self._last_error = str(e)
            raise UpstreamUnavailable(str(e)) from e

        sid = resp.headers.get("MCP-Session-Id")
        if sid and not self._session_id:
            self._session_id = sid

        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            # The response is a single-request SSE — find the first message.
            payload = await self._read_first_sse_message(resp)
        else:
            try:
                payload = resp.json()
            except ValueError as e:
                raise UpstreamProtocolError(f"non-JSON response: {e}")
        return unwrap_result(payload)

    @staticmethod
    async def _read_first_sse_message(resp: httpx.Response) -> dict[str, Any]:
        async for chunk in resp.aiter_text():
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    raw = line[len("data:"):].strip()
                    if not raw:
                        continue
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        continue
        raise UpstreamProtocolError("empty SSE response")

    async def _post_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._client:
            return
        try:
            await self._client.post(self.url, json=build_notification(method, params), headers=self._headers())
        except httpx.HTTPError:
            self._connected = False

    async def _start_listener(self) -> None:
        if not self.listen_for_notifications or self._listener or not self._client:
            return
        self._listener = asyncio.create_task(self._listen_loop(), name=f"sht-listen-{self.server_id}")

    async def _listen_loop(self) -> None:
        assert self._client
        try:
            async with self._client.stream("GET", self.url, headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    return
                async for chunk in resp.aiter_text():
                    for line in chunk.splitlines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:"):].strip()
                        if not raw:
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        method = msg.get("method")
                        if method == "notifications/tools/list_changed":
                            self._change_q.put_nowait("tools")
                        elif method == "notifications/resources/list_changed":
                            self._change_q.put_nowait("resources")
                        elif method == "notifications/prompts/list_changed":
                            self._change_q.put_nowait("prompts")
        except (asyncio.CancelledError, httpx.HTTPError):
            return
        except Exception as e:  # noqa: BLE001
            _log.warning("streamable_http %s listener error: %s", self.server_id, e)

    # ------------------------------------------------------------------
    async def initialize(self) -> dict[str, Any]:
        result = await self._post("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"roots": {}, "sampling": {}},
            "clientInfo": {"name": "concierge", "version": "0.1.0"},
        })
        await self._post_notification("notifications/initialized")
        await self._start_listener()
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

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
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
