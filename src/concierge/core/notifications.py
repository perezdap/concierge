"""
Per-session notification bus.

When publishing state changes for a session, we enqueue
`notifications/tools/list_changed` (and friends) here. The Streamable HTTP layer
drains the queue into the open SSE stream for that session.

Coalescing: repeated identical notifications within a short window are folded
into one — useful because the publishing engine may emit several mutations in a
single batch.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class NotificationBus:
    def __init__(self, coalesce_window_s: float = 0.05, *, max_queue_size: int = 1024) -> None:
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._last_emit: dict[tuple[str, str], float] = {}
        self._coalesce = coalesce_window_s
        # Per-session queues are bounded: a slow or vanished SSE consumer must
        # not let a session's backlog grow without limit (memory leak). 0 means
        # unbounded — avoid in production.
        self._max_queue_size = max_queue_size

    def queue_for(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        q = self._queues.get(session_id)
        if q is None:
            q = asyncio.Queue(maxsize=self._max_queue_size)
            self._queues[session_id] = q
        return q

    def drop(self, session_id: str) -> None:
        self._queues.pop(session_id, None)

    def publish(self, session_id: str, method: str, params: dict[str, Any] | None = None) -> None:
        now = time.monotonic()
        key = (session_id, method)
        if now - self._last_emit.get(key, 0.0) < self._coalesce:
            return
        self._last_emit[key] = now
        q = self.queue_for(session_id)
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        # asyncio.Queue.put_nowait — no awaiting in publishers. If the consumer
        # has stalled and the queue is full, drop the stalest signal to make
        # room: list_changed notifications are idempotent, so the newest one
        # still tells the client to re-list. This bounds memory instead of
        # raising QueueFull into the publishing path.
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # Helpers
    def tools_list_changed(self, session_id: str) -> None:
        self.publish(session_id, "notifications/tools/list_changed")

    def resources_list_changed(self, session_id: str) -> None:
        self.publish(session_id, "notifications/resources/list_changed")

    def prompts_list_changed(self, session_id: str) -> None:
        self.publish(session_id, "notifications/prompts/list_changed")
