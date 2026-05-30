"""Audit logger.

Append-only structured event stream. Backed by stdlib logging in the MVP so it
can be redirected to syslog / journald / a sidecar collector. The interface is
narrow so a Kafka / object-store backend can drop in later.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from .redact import redact

_audit = logging.getLogger("concierge.audit")


class AuditSink(Protocol):
    def emit(self, event: str, payload: dict[str, Any]) -> None: ...


class AuditLogger:
    def __init__(
        self,
        logger: logging.Logger = _audit,
        *,
        sinks: list[AuditSink] | None = None,
    ) -> None:
        self.logger = logger
        self.sinks = sinks or []

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "event": event,
            "ts": time.time(),
            **redact(fields),
        }
        self.logger.info(event, extra={"extra_data": payload})
        for sink in self.sinks:
            try:
                sink.emit(event, payload)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    "audit sink %s failed: %s",
                    sink.__class__.__name__,
                    e,
                )

    # Convenience helpers — one method per audited transition.
    def session_created(self, session_id: str, **f: Any) -> None:
        self.emit("session.created", session_id=session_id, **f)

    def session_closed(self, session_id: str, **f: Any) -> None:
        self.emit("session.closed", session_id=session_id, **f)

    def tool_enabled(self, session_id: str, names: list[str], by: str) -> None:
        self.emit("publish.enable", session_id=session_id, names=names, by=by)

    def tool_disabled(self, session_id: str, names: list[str]) -> None:
        self.emit("publish.disable", session_id=session_id, names=names)

    def tool_called(
        self,
        session_id: str,
        canonical_name: str,
        server_id: str,
        ok: bool,
        latency_ms: float,
        error_code: int | None = None,
        request_bytes: int | None = None,
        response_bytes: int | None = None,
    ) -> None:
        self.emit(
            "tool.call",
            session_id=session_id,
            canonical_name=canonical_name,
            server_id=server_id,
            ok=ok,
            latency_ms=round(latency_ms, 2),
            error_code=error_code,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
        )

    def session_evicted(self, session_id: str, **f: Any) -> None:
        self.emit("session.evicted", session_id=session_id, **f)

    def upstream_session(
        self, event: str, server_id: str, router_session_id: str | None = None, **f: Any
    ) -> None:
        """event ∈ {created, evicted, lru_evicted} — tracks the per-session pool."""
        self.emit(
            f"upstream_session.{event}",
            server_id=server_id,
            router_session_id=router_session_id,
            **f,
        )

    def policy_decision(
        self, session_id: str, canonical_name: str, decision: str, reason: str = ""
    ) -> None:
        self.emit(
            "policy.decision",
            session_id=session_id,
            canonical_name=canonical_name,
            decision=decision,
            reason=reason,
        )
