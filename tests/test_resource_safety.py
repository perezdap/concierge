"""Resource-safety regressions (P0-5): no pipe-buffer deadlocks, no leaks."""
from __future__ import annotations

import asyncio
import sys

import pytest

from concierge.adapters.stdio import StdioAdapter
from concierge.core.notifications import NotificationBus


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


# --- bounded notification queues (P0-5) -----------------------------------
#
# A per-session SSE queue must not grow without bound when its consumer is
# slow or absent (a disconnected client that never drains its stream), or the
# gateway leaks memory. list_changed signals are idempotent, so dropping the
# stalest one on overflow is safe.


@pytest.mark.asyncio
async def test_notification_queue_is_bounded_and_keeps_newest():
    bus = NotificationBus(coalesce_window_s=0.0, max_queue_size=3)
    sid = "s1"
    for i in range(10):  # distinct methods so coalescing never folds them
        bus.publish(sid, f"notifications/x/{i}")
    q = bus.queue_for(sid)
    assert q.qsize() == 3  # bounded, not 10
    drained = [q.get_nowait()["method"] for _ in range(3)]
    assert drained == [
        "notifications/x/7",
        "notifications/x/8",
        "notifications/x/9",
    ]


@pytest.mark.asyncio
async def test_notification_publish_still_delivers_under_cap():
    bus = NotificationBus(max_queue_size=8)
    bus.tools_list_changed("s2")
    msg = bus.queue_for("s2").get_nowait()
    assert msg["method"] == "notifications/tools/list_changed"
