"""Resource-safety regressions (P0-5): no pipe-buffer deadlocks, no leaks."""
from __future__ import annotations

import asyncio
import sys

import pytest

from concierge.adapters.stdio import StdioAdapter


class _FakeStream:
    """Minimal async stream yielding pre-seeded chunks then EOF (b"")."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self, stderr: _FakeStream) -> None:
        self.stderr = stderr
        self.returncode = None


@pytest.mark.asyncio
async def test_drain_stderr_consumes_until_eof_and_keeps_last_line():
    adapter = StdioAdapter("t", ["python", "-c", "pass"])
    adapter._proc = _FakeProc(_FakeStream([b"warn one\nwarn two\n"]))  # type: ignore[assignment]
    # Returns (does not hang) once EOF is reached; last line retained for health.
    await asyncio.wait_for(adapter._drain_stderr(), timeout=1.0)
    assert adapter._last_stderr == "warn two"


@pytest.mark.asyncio
async def test_connect_starts_stderr_drain_task():
    adapter = StdioAdapter("t", [sys.executable, "-c", "pass"])
    await adapter.connect()
    try:
        assert adapter._stderr_task is not None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_large_stderr_burst_does_not_deadlock_stdout_response():
    """A child that floods stderr *before* replying on stdout must not hang.

    Without draining stderr, the child blocks once the OS pipe buffer fills,
    never writes its stdout response, and the request times out. Draining
    stderr concurrently keeps the child unblocked so the response arrives.
    """
    code = (
        "import sys, json\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    req = json.loads(line)\n"
        "    sys.stderr.write('x' * 500000)\n"  # no newline, exceeds the OS pipe buffer
        "    sys.stderr.flush()\n"
        "    sys.stdout.write(json.dumps("
        "{'jsonrpc': '2.0', 'id': req['id'], 'result': {'ok': True}}) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    adapter = StdioAdapter("t", [sys.executable, "-c", code], request_timeout_s=5.0)
    await adapter.connect()
    try:
        result = await adapter._request("ping", {})
        assert result == {"ok": True}
    finally:
        await adapter.close()
