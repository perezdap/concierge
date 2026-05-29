"""Audit logger.

Append-only structured event stream. Backed by stdlib logging in the MVP so it
can be redirected to syslog / journald / a sidecar collector. The interface is
narrow so a Kafka / object-store backend can drop in later.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .redact import redact

_audit = logging.getLogger("concierge.audit")


class AuditLogger:
    def __init__(self, logger: logging.Logger = _audit) -> None:
        self.logger = logger

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "event": event,
            "ts": time.time(),
            **redact(fields),
        }
        self.logger.info(event, extra={"extra_data": payload})

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
    ) -> None:
        self.emit(
            "tool.call",
            session_id=session_id,
            canonical_name=canonical_name,
            server_id=server_id,
            ok=ok,
            latency_ms=round(latency_ms, 2),
            error_code=error_code,
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
