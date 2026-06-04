"""Approval brokers.

Three operator-selectable modes, all behind one :class:`ApprovalBroker` ABC:

* :class:`DenyByDefaultApprovalBroker` — every gated tool is uninvokable. The safe
  MVP default (P0-3); still the right choice when no operator console exists.
* :class:`AllowListApprovalBroker` — a curated, static set of canonical tool names
  is pre-approved; everything else is denied (P0-3, interim).
* :class:`QueuedApprovalBroker` — the real out-of-band workflow (P1-3). A gated
  call *parks* a pending record in shared state and **waits** (bounded) for an
  operator to grant or deny it; on grant the call resumes and executes upstream,
  on deny / TTL it returns a structured denial.

Resumption strategy (bounded await — see ``docs/APPROVALS.md`` for the rationale):
the gateway facade dispatches ``tools/call`` synchronously, so the broker simply
``await``\\s the record's terminal state inside ``PolicyEngine.authorize_call``,
up to ``wait_timeout_s`` (default 5 min). The wait wakes immediately when the
decision is made in *this* process (an in-memory event), and otherwise polls the
shared store on a short interval so a grant issued on a *different* replica is
still observed. If the wait elapses with no decision, the call is denied with a
``ttl`` reason — the client never holds the connection open indefinitely.
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

from ..core.types import CatalogEntry, Session
from ..util.redact import redact
from .approval_store import ApprovalRecord, ApprovalStatus, ApprovalStore
from .webhook import WebhookDispatcher


class ApprovalDecision:
    """Outcome the broker hands back to the policy engine.

    ``approved`` gates the call; ``record`` (when present) carries the parked
    approval so the engine can put its id + reason into the structured denial a
    client receives on a deny / TTL outcome.
    """

    __slots__ = ("approved", "record", "reason")

    def __init__(
        self,
        approved: bool,
        *,
        record: ApprovalRecord | None = None,
        reason: str | None = None,
    ) -> None:
        self.approved = approved
        self.record = record
        self.reason = reason


class ApprovalBroker(ABC):
    @abstractmethod
    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool: ...

    async def evaluate(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> ApprovalDecision:
        """Richer entry point used by the policy engine. Defaults to ``is_approved``."""
        ok = await self.is_approved(session, entry, arguments)
        return ApprovalDecision(ok)


class DenyByDefaultApprovalBroker(ApprovalBroker):
    """Always denies — safe default for MVP."""

    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool:
        return False


class AllowListApprovalBroker(ApprovalBroker):
    """Pre-approve an explicit, operator-curated set of canonical tool names.

    Interim (P0-3) broker that makes deny-by-default configurable: tools the
    operator has vetted ahead of time can be invoked, everything else is still
    denied. The real out-of-band approval queue is :class:`QueuedApprovalBroker`.
    """

    def __init__(self, allowed: list[str]) -> None:
        self._allowed = set(allowed)

    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool:
        return entry.canonical_name in self._allowed


def summarize_arguments(arguments: dict[str, Any], *, max_chars: int = 500) -> str:
    """Redacted, length-capped JSON summary of call args — never raw secrets.

    Reuses the audit-layer :func:`redact` so token/secret/password-shaped keys are
    masked before anything is persisted to the (potentially durable) queue.
    """
    try:
        text = json.dumps(redact(arguments), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        text = "<unserializable arguments>"
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


class QueuedApprovalBroker(ApprovalBroker):
    """Out-of-band approval queue with bounded-await resumption (P1-3).

    A gated call parks a pending record and waits for a terminal decision. The
    decision can arrive from any replica (it lives in the shared store); a
    ``decided`` event lets a same-process operator path wake the waiter instantly,
    while a polling fallback covers cross-replica grants.
    """

    def __init__(
        self,
        store: ApprovalStore,
        *,
        ttl_s: float = 300.0,
        wait_timeout_s: float = 300.0,
        poll_interval_s: float = 1.0,
        webhooks: WebhookDispatcher | None = None,
        audit: Any | None = None,
    ) -> None:
        self.store = store
        self.ttl_s = ttl_s
        # The waiter never blocks longer than the record's own TTL — past that the
        # record is EXPIRED, so capping the wait there avoids a pointless extra wait.
        self.wait_timeout_s = min(wait_timeout_s, ttl_s)
        self.poll_interval_s = poll_interval_s
        self.webhooks = webhooks
        self.audit = audit
        # approval_id -> event, for instant same-process wakeups.
        self._waiters: dict[str, asyncio.Event] = {}

    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool:
        decision = await self.evaluate(session, entry, arguments)
        return decision.approved

    async def evaluate(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> ApprovalDecision:
        record = await self.store.create(
            tenant_id=session.tenant_id,
            session_id=session.session_id,
            canonical_name=entry.canonical_name,
            args_summary=summarize_arguments(arguments),
            ttl_s=self.ttl_s,
        )
        self._emit("approval.requested", record)
        event = asyncio.Event()
        self._waiters[record.approval_id] = event
        try:
            final = await self._await_decision(record.approval_id, event)
        finally:
            self._waiters.pop(record.approval_id, None)

        if final.status is ApprovalStatus.GRANTED:
            return ApprovalDecision(True, record=final)
        reason = "expired" if final.status is ApprovalStatus.EXPIRED else "denied"
        return ApprovalDecision(False, record=final, reason=reason)

    async def _await_decision(
        self, approval_id: str, event: asyncio.Event
    ) -> ApprovalRecord:
        """Block until the record reaches a terminal state or the wait elapses.

        Polls the shared store so a decision made on another replica is observed;
        the event short-circuits the poll when the decision is local.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.wait_timeout_s
        while True:
            current = await self.store.get(approval_id)
            if current is not None and current.is_terminal():
                return current
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Force the record to a terminal EXPIRED state and return it.
                return await self._force_expire(approval_id, current)
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=min(self.poll_interval_s, remaining)
                )
            except TimeoutError:
                pass
            event.clear()

    async def _force_expire(
        self, approval_id: str, current: ApprovalRecord | None
    ) -> ApprovalRecord:
        # The store flips a lapsed PENDING to EXPIRED on read; if the wait deadline
        # arrived before the TTL (wait_timeout_s == ttl_s so this is rare), record
        # the timeout deterministically as a denial-equivalent EXPIRED.
        decided = await self.store.decide(
            approval_id, granted=False, decided_by="system:timeout", reason="wait_timeout"
        )
        if decided is not None and decided.status is ApprovalStatus.DENIED:
            decided.status = ApprovalStatus.EXPIRED
            return decided
        refreshed = await self.store.get(approval_id)
        if refreshed is not None:
            return refreshed
        if current is not None:
            current.status = ApprovalStatus.EXPIRED
            return current
        raise RuntimeError(f"approval {approval_id} vanished during wait")

    # -- operator decision surface (called by admin endpoint + CLI) ----------

    async def decide(
        self,
        approval_id: str,
        *,
        granted: bool,
        decided_by: str,
        tenant_id: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRecord | None:
        """Resolve a pending approval, wake any local waiter, and fire webhooks."""
        record = await self.store.decide(
            approval_id,
            granted=granted,
            decided_by=decided_by,
            tenant_id=tenant_id,
            reason=reason,
        )
        if record is None:
            return None
        # Wake a same-process waiter immediately (cross-replica waiters poll).
        waiter = self._waiters.get(approval_id)
        if waiter is not None:
            waiter.set()
        event_name = "approval.granted" if record.status is ApprovalStatus.GRANTED else (
            "approval.denied"
        )
        self._emit(event_name, record)
        if self.webhooks is not None and record.is_terminal():
            await self.webhooks.dispatch(event=event_name, record_public=record.to_public())
        return record

    def _emit(self, event: str, record: ApprovalRecord) -> None:
        emit = getattr(self.audit, "emit", None)
        if emit is None:
            return
        emit(
            event,
            approval_id=record.approval_id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            tool=record.canonical_name,
            status=record.status.value,
            decided_by=record.decided_by,
        )


__all__ = [
    "AllowListApprovalBroker",
    "ApprovalBroker",
    "ApprovalDecision",
    "DenyByDefaultApprovalBroker",
    "QueuedApprovalBroker",
    "summarize_arguments",
]
