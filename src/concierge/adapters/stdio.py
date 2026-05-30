"""Local stdio MCP subprocess adapter.

Frames JSON-RPC over stdin/stdout (newline-delimited; the dominant MCP stdio
framing). Each adapter manages exactly one child process. Crashes are detected,
status flips to disconnected, and `AdapterManager` will reconnect.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ..core.types import AdapterHealth, TransportType
from ..errors import UpstreamProtocolError, UpstreamTimeout, UpstreamUnavailable
from ..util.log import get_logger
from ._jsonrpc import build_notification, build_request, unwrap_result
from .base import UpstreamAdapter

_log = get_logger("concierge.adapter.stdio")


class StdioAdapter(UpstreamAdapter):
    transport = TransportType.STDIO

    def __init__(
        self,
        server_id: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        request_timeout_s: float = 30.0,
    ) -> None:
        self.server_id = server_id
        self.command = command
        self.env = {**os.environ, **(env or {})}
        self.cwd = cwd
        self.request_timeout_s = request_timeout_s

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._change_q: asyncio.Queue[str] = asyncio.Queue()
        self._connected = False
        self._last_error: str | None = None
        self._last_stderr: str | None = None
        self._last_connected_at: datetime | None = None
        self._failures = 0

    # ------------------------------------------------------------------
    async def connect(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
            )
        except OSError as e:
            self._last_error = str(e)
            self._failures += 1
            raise UpstreamUnavailable(f"could not spawn {self.command!r}: {e}") from e

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"stdio-read-{self.server_id}"
        )
        # Continuously drain stderr. An undrained stderr pipe fills its OS buffer
        # and blocks the child mid-write — a deadlock that also stalls stdout
        # (and thus every pending request). Draining keeps the child flowing.
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"stdio-stderr-{self.server_id}"
        )
        self._connected = True
        self._last_connected_at = datetime.now(UTC)
        self._failures = 0

    async def close(self) -> None:
        self._connected = False
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except TimeoutError:
                    self._proc.kill()
            except ProcessLookupError:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(UpstreamUnavailable("adapter closed"))
        self._pending.clear()

    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise UpstreamUnavailable("stdio process not started")
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    _log.warning("stdio %s emitted non-JSON line", self.server_id)
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log.exception("stdio %s reader crashed: %s", self.server_id, e)
        finally:
            self._connected = False
            self._last_error = "stdio reader exited"
            # Fail all pending requests so callers don't hang.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(UpstreamUnavailable("upstream stdio exited"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        """Drain the child's stderr so its pipe buffer never fills and blocks it.

        We read fixed-size chunks rather than lines: ``readline()`` would stall on
        output with no newline (or a single huge line), letting the stream's
        buffer hit its high-water mark, pause the transport, and re-create the
        very deadlock we are preventing. The most recent non-empty line is kept
        for diagnostics. The loop exits on EOF (child closed stderr) or cancel.
        """
        if not self._proc or not self._proc.stderr:
            return
        stream = self._proc.stderr
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                for line in chunk.decode("utf-8", "replace").splitlines():
                    line = line.rstrip()
                    if line:
                        self._last_stderr = line
                        _log.debug("stdio %s stderr: %s", self.server_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — draining must never crash the adapter
            _log.debug("stdio %s stderr drain ended: %s", self.server_id, e)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and msg["id"] in self._pending:
            self._pending.pop(msg["id"]).set_result(msg)
            return
        # Notifications
        method = msg.get("method")
        if isinstance(method, str):
            if method == "notifications/tools/list_changed":
                self._change_q.put_nowait("tools")
            elif method == "notifications/resources/list_changed":
                self._change_q.put_nowait("resources")
            elif method == "notifications/prompts/list_changed":
                self._change_q.put_nowait("prompts")

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._connected or not self._proc or not self._proc.stdin:
            raise UpstreamUnavailable(self.server_id)
        req = build_request(method, params)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req["id"]] = fut
        line = (json.dumps(req) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(req["id"], None)
            self._connected = False
            self._failures += 1
            raise UpstreamUnavailable(str(e)) from e

        try:
            response = await asyncio.wait_for(fut, timeout=self.request_timeout_s)
        except TimeoutError:
            self._pending.pop(req["id"], None)
            self._failures += 1
            raise UpstreamTimeout(f"{self.server_id}.{method}")
        return unwrap_result(response)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._connected or not self._proc or not self._proc.stdin:
            return
        line = (json.dumps(build_notification(method, params)) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._connected = False

    # ------------------------------------------------------------------
    async def initialize(self) -> dict[str, Any]:
        result = await self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"roots": {}, "sampling": {}},
            "clientInfo": {"name": "concierge", "version": "0.1.0"},
        })
        await self._notify("notifications/initialized")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list")
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    async def list_resources(self) -> list[dict[str, Any]]:
        try:
            result = await self._request("resources/list")
        except UpstreamProtocolError:
            return []
        return list(result.get("resources", [])) if isinstance(result, dict) else []

    async def list_prompts(self) -> list[dict[str, Any]]:
        try:
            result = await self._request("prompts/list")
        except UpstreamProtocolError:
            return []
        return list(result.get("prompts", [])) if isinstance(result, dict) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {"content": []}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri})
        return result if isinstance(result, dict) else {}

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {}

    async def list_changed_events(self) -> AsyncIterator[str]:
        while True:
            kind = await self._change_q.get()
            yield kind

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            server_id=self.server_id,
            transport=self.transport,
            connected=self._connected,
            last_error=self._last_error,
            last_connected_at=self._last_connected_at,
            consecutive_failures=self._failures,
        )
