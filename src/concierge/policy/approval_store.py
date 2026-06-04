"""Approval queue store (P1-3).

A *pending approval* is created the moment a dangerous / ``requires_approval``
tool call reaches the policy engine under ``approval_mode == "queue"``. The
original call then *waits* on the record's terminal state instead of being
denied outright. An operator grants or denies it out-of-band (HTTP admin
endpoint or CLI), the decision is written back here, and the waiting call
resumes (grant → execute upstream) or returns a structured denial (deny / TTL).

The app tier is stateless (P1-2), so the queue must live in shared state to
survive a replica restart and be visible fleet-wide. The backend matrix mirrors
the P1-1 / P1-2 / P1-4 stores exactly: one async ABC, with in-memory (tests/dev
only), Redis (ephemeral), and Postgres (durable + audit) implementations behind
it, all config-selectable.

Security / hygiene (carried from P0-2 / P1-1):

* Only an ``args_summary`` (string) is persisted — never the raw argument dict,
  which may carry secrets. The summary is produced by the broker with the same
  redaction the audit layer uses.
* Decisions are *idempotent*: the first terminal decision wins; a later grant on
  an already-denied record (or vice-versa) is a no-op that returns the existing
  record. This makes the HTTP + CLI + TTL-sweeper paths safe to race.
* TTL expiry is computed at read time (and pruned opportunistically), so a never-
  decided record cannot pin a tool call open forever even if no sweeper runs.
"""
from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any
from uuid import uuid4

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - exercised only when redis is absent
    aioredis = None  # type: ignore[assignment]

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class ApprovalRecord:
    """One parked approval. Carries no raw argument bytes — only a summary."""

    approval_id: str
    tenant_id: str
    session_id: str
    canonical_name: str
    args_summary: str
    requested_at: float
    ttl_s: float
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: float | None = None
    decided_by: str | None = None  # operator principal (opaque audit subject)
    reason: str | None = None

    @property
    def expires_at(self) -> float:
        return self.requested_at + self.ttl_s

    def is_expired(self, *, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def is_terminal(self) -> bool:
        return self.status is not ApprovalStatus.PENDING

    def to_public(self) -> dict[str, object]:
        """Inspection view (admin / MCP resource). No secrets, summary only."""
        return {
            "approval_id": self.approval_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "tool": self.canonical_name,
            "args_summary": self.args_summary,
            "status": self.status.value,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "reason": self.reason,
        }


def new_approval_id() -> str:
    return "ap_" + uuid4().hex[:20]


class ApprovalStore(ABC):
    """Backend-agnostic pending-approval queue keyed by ``approval_id``.

    The base class owns the *semantics* (create, decide-idempotently, list-
    pending, TTL expiry); subclasses provide only the raw persistence hooks.
    """

    async def create(
        self,
        *,
        tenant_id: str,
        session_id: str,
        canonical_name: str,
        args_summary: str,
        ttl_s: float,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=new_approval_id(),
            tenant_id=tenant_id,
            session_id=session_id,
            canonical_name=canonical_name,
            args_summary=args_summary,
            requested_at=time.time(),
            ttl_s=ttl_s,
        )
        await self._put(record)
        return record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        """Fetch a record, lazily flipping a lapsed PENDING to EXPIRED."""
        record = await self._fetch(approval_id)
        if record is None:
            return None
        if record.status is ApprovalStatus.PENDING and record.is_expired():
            record.status = ApprovalStatus.EXPIRED
            record.decided_at = record.expires_at
            await self._put(record)
        return record

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        decided_by: str,
        tenant_id: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRecord | None:
        """Resolve a pending record. First terminal decision wins (idempotent).

        ``tenant_id``, when given, scopes the decision: a record belonging to a
        different tenant is left untouched and ``None`` is returned, so a cross-
        tenant grant is impossible at the store layer too (defence in depth — the
        admin handler also checks).
        """
        record = await self.get(approval_id)
        if record is None:
            return None
        if tenant_id is not None and record.tenant_id != tenant_id:
            return None
        if record.is_terminal():
            # Idempotent: a prior grant/deny/expiry stands.
            return record
        record.status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DENIED
        record.decided_at = time.time()
        record.decided_by = decided_by
        record.reason = reason
        await self._put(record)
        return record

    async def list_pending(self, *, tenant_id: str | None = None) -> list[ApprovalRecord]:
        """All still-pending (non-expired) records, optionally tenant-scoped."""
        now = time.time()
        out: list[ApprovalRecord] = []
        for record in await self._all():
            if record.status is not ApprovalStatus.PENDING:
                continue
            if record.is_expired(now=now):
                continue
            if tenant_id is not None and record.tenant_id != tenant_id:
                continue
            out.append(record)
        out.sort(key=lambda r: r.requested_at)
        return out

    @abstractmethod
    async def _put(self, record: ApprovalRecord) -> None: ...

    @abstractmethod
    async def _fetch(self, approval_id: str) -> ApprovalRecord | None: ...

    @abstractmethod
    async def _all(self) -> list[ApprovalRecord]: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryApprovalStore(ApprovalStore):
    """Process-local approval queue. Tests/dev only — invisible to other replicas."""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = asyncio.Lock()

    async def _put(self, record: ApprovalRecord) -> None:
        async with self._lock:
            self._records[record.approval_id] = record

    async def _fetch(self, approval_id: str) -> ApprovalRecord | None:
        async with self._lock:
            rec = self._records.get(approval_id)
            return _clone(rec) if rec is not None else None

    async def _all(self) -> list[ApprovalRecord]:
        async with self._lock:
            return [_clone(r) for r in self._records.values()]


def _clone(record: ApprovalRecord) -> ApprovalRecord:
    return ApprovalRecord(**asdict(record))


def _record_to_json(record: ApprovalRecord) -> str:
    return json.dumps(asdict(record), default=str)


def _record_from_mapping(data: dict[str, object]) -> ApprovalRecord:
    def _opt(key: str):  # type: ignore[no-untyped-def]
        v = data.get(key)
        return None if v in (None, "None") else v

    decided_at = _opt("decided_at")
    decided_by = _opt("decided_by")
    reason = _opt("reason")
    return ApprovalRecord(
        approval_id=str(data["approval_id"]),
        tenant_id=str(data["tenant_id"]),
        session_id=str(data["session_id"]),
        canonical_name=str(data["canonical_name"]),
        args_summary=str(data.get("args_summary", "")),
        requested_at=float(data["requested_at"]),  # type: ignore[arg-type]
        ttl_s=float(data["ttl_s"]),  # type: ignore[arg-type]
        status=ApprovalStatus(str(data["status"])),
        decided_at=float(decided_at) if decided_at is not None else None,  # type: ignore[arg-type]
        decided_by=str(decided_by) if decided_by is not None else None,
        reason=str(reason) if reason is not None else None,
    )


class RedisApprovalStore(ApprovalStore):
    """Redis-backed approval queue. One key per record (``approval:v1:{id}``).

    Each record is stored as a JSON blob with a Redis TTL set to the approval's
    own ``ttl_s`` (plus a small grace) so abandoned records self-prune. The
    base-class read path still flips lapsed PENDING records to EXPIRED so a call
    that out-waited the grace window observes a deterministic terminal state.
    """

    KEY_SCHEMA_VERSION = "v1"

    def __init__(self, redis_url: str, *, key_prefix: str = "approval") -> None:
        if aioredis is None:
            raise RuntimeError("redis[asyncio] is required for RedisApprovalStore")
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._prefix = f"{key_prefix}:{self.KEY_SCHEMA_VERSION}:"

    def _key(self, approval_id: str) -> str:
        return f"{self._prefix}{approval_id}"

    async def _put(self, record: ApprovalRecord) -> None:
        # Keep decided records around for a short window so a racing waiter / poll
        # still sees the verdict, but never longer than ttl + grace.
        grace = 60
        ttl = max(1, int(record.ttl_s) + grace)
        await self._redis.set(self._key(record.approval_id), _record_to_json(record), ex=ttl)

    async def _fetch(self, approval_id: str) -> ApprovalRecord | None:
        raw = await self._redis.get(self._key(approval_id))
        if raw is None:
            return None
        return _record_from_mapping(json.loads(raw))

    async def _all(self) -> list[ApprovalRecord]:
        out: list[ApprovalRecord] = []
        async for key in self._redis.scan_iter(match=f"{self._prefix}*"):
            raw = await self._redis.get(key)
            if raw:
                out.append(_record_from_mapping(json.loads(raw)))
        return out

    async def aclose(self) -> None:
        await self._redis.aclose()


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id    TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    args_summary   TEXT NOT NULL DEFAULT '',
    requested_at   DOUBLE PRECISION NOT NULL,
    ttl_s          DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL,
    decided_at     DOUBLE PRECISION,
    decided_by     TEXT,
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_tenant_status
    ON approvals(tenant_id, status);
"""


class PostgresApprovalStore(ApprovalStore):
    """Durable approval queue in Postgres (asyncpg).

    The preferred prod backend: a granted/denied record is a permanent audit
    artefact (who decided, when, why) that survives a Redis flush.
    """

    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgresApprovalStore")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(_PG_SCHEMA)
        return self._pool

    async def _put(self, record: ApprovalRecord) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            INSERT INTO approvals (
                approval_id, tenant_id, session_id, canonical_name, args_summary,
                requested_at, ttl_s, status, decided_at, decided_by, reason
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (approval_id) DO UPDATE SET
                status     = EXCLUDED.status,
                decided_at = EXCLUDED.decided_at,
                decided_by = EXCLUDED.decided_by,
                reason     = EXCLUDED.reason
            """,
            record.approval_id,
            record.tenant_id,
            record.session_id,
            record.canonical_name,
            record.args_summary,
            record.requested_at,
            record.ttl_s,
            record.status.value,
            record.decided_at,
            record.decided_by,
            record.reason,
        )

    @staticmethod
    def _row_to_record(row: Any) -> ApprovalRecord:
        r = dict(row)
        return ApprovalRecord(
            approval_id=r["approval_id"],
            tenant_id=r["tenant_id"],
            session_id=r["session_id"],
            canonical_name=r["canonical_name"],
            args_summary=r["args_summary"],
            requested_at=float(r["requested_at"]),
            ttl_s=float(r["ttl_s"]),
            status=ApprovalStatus(r["status"]),
            decided_at=(float(r["decided_at"]) if r["decided_at"] is not None else None),
            decided_by=r["decided_by"],
            reason=r["reason"],
        )

    async def _fetch(self, approval_id: str) -> ApprovalRecord | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
        return None if row is None else self._row_to_record(row)

    async def _all(self) -> list[ApprovalRecord]:
        pool = await self._ensure_pool()
        rows = await pool.fetch("SELECT * FROM approvals")
        return [self._row_to_record(r) for r in rows]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = [
    "ApprovalRecord",
    "ApprovalStatus",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "PostgresApprovalStore",
    "RedisApprovalStore",
    "new_approval_id",
]
