"""Session manager — owns Session lifecycle keyed by MCP-Session-Id."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from ..errors import Unauthorized
from .types import Session


class SessionManager:
    def __init__(self, idle_ttl_seconds: int = 60 * 60) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.idle_ttl = idle_ttl_seconds

    async def create(self, *, tenant_id: str = "default", auth_subject: str | None = None) -> Session:
        async with self._lock:
            s = Session(tenant_id=tenant_id, auth_subject=auth_subject)
            self._sessions[s.session_id] = s
            return s

    def get(self, session_id: str) -> Optional[Session]:
        s = self._sessions.get(session_id)
        if s is not None:
            s.last_seen_at = datetime.now(timezone.utc)
        return s

    def require(self, session_id: str | None) -> Session:
        if not session_id:
            raise Unauthorized("missing MCP-Session-Id")
        s = self.get(session_id)
        if s is None:
            raise Unauthorized("unknown or expired session")
        return s

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    async def gc(self) -> int:
        now = datetime.now(timezone.utc)
        victims = [
            sid for sid, s in self._sessions.items()
            if (now - s.last_seen_at).total_seconds() > self.idle_ttl
        ]
        async with self._lock:
            for sid in victims:
                self._sessions.pop(sid, None)
        return len(victims)
