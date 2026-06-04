"""Graceful-drain coordination for rolling restarts (P1-7).

On SIGTERM, uvicorn runs the FastAPI lifespan shutdown. We want that shutdown to:

  1. Flip readiness to *not ready* so the load balancer / k8s endpoints
     controller stops sending new connections to this pod.
  2. Refuse to mint brand-new sessions (the ``initialize`` handshake) with a
     retryable, 503-equivalent error so clients reconnect to a healthy replica.
  3. Let already-dispatched, in-flight calls run to completion before upstream
     adapters and stores are torn down — so a rolling restart drops zero
     in-flight requests.

The controller is intentionally tiny and dependency-free: a boolean flag plus an
in-flight counter guarded by an asyncio.Condition. It is created in
``build_app`` and shared by the metrics middleware (counts in-flight requests),
the facade (rejects new sessions when draining), the readiness probe (reports
not-ready when draining), and the lifespan hook (waits for the count to reach
zero, bounded by a deadline).
"""
from __future__ import annotations

import asyncio
import time


class DrainController:
    """Tracks drain state and the number of in-flight gateway requests."""

    def __init__(self) -> None:
        self._draining = False
        self._in_flight = 0
        self._cond = asyncio.Condition()

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def begin_drain(self) -> None:
        """Mark the process as draining. Idempotent."""
        async with self._cond:
            self._draining = True
            self._cond.notify_all()

    async def acquire(self) -> None:
        """Register one in-flight request.

        Always increments the counter so a concurrent shutdown waits for this
        request to finish. The caller MUST call ``release`` exactly once. Use
        the ``draining`` property to decide whether to *admit* new work (e.g.
        reject brand-new sessions) before acquiring.
        """
        async with self._cond:
            self._in_flight += 1

    async def release(self) -> None:
        async with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight == 0:
                self._cond.notify_all()

    async def wait_for_idle(self, timeout_s: float) -> bool:
        """Block until in-flight reaches zero or the timeout elapses.

        Returns True if fully drained, False if the deadline was hit (callers
        should log the residual count and proceed with shutdown).
        """
        deadline = time.monotonic() + timeout_s
        async with self._cond:
            while self._in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._in_flight == 0
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except TimeoutError:
                    return self._in_flight == 0
            return True
