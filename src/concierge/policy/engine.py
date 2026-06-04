"""
Policy engine.

Stays small in the MVP but is the single chokepoint every call_tool flows
through. Adding multi-tenant rules, ABAC, or external policy services means
plugging more checks in here.
"""
from __future__ import annotations

from typing import Any

from ..core.types import CatalogEntry, RiskLevel, Session
from ..errors import ApprovalRequired, Forbidden, RateLimited
from ..util.audit import AuditLogger
from .approval import ApprovalBroker
from .ratelimit import RateLimiter


class PolicyEngine:
    def __init__(
        self,
        rate_limiter: RateLimiter,
        approval: ApprovalBroker,
        audit: AuditLogger,
        *,
        block_dangerous_without_approval: bool = True,
        metrics: Any | None = None,
    ) -> None:
        self.rate_limiter = rate_limiter
        self.approval = approval
        self.audit = audit
        self.block_dangerous = block_dangerous_without_approval
        # Optional MetricRegistry (P1-6). When present we emit ratelimit
        # allowed/denied counters labelled by tenant + tool.
        self.metrics = metrics

    async def authorize_call(
        self,
        session: Session,
        entry: CatalogEntry,
        arguments: dict[str, Any],
    ) -> None:
        # 1. Rate limit — multi-dimensional bucket keyed by (tenant, session, tool).
        decision = await self.rate_limiter.acquire(
            session.tenant_id, session.session_id, entry.canonical_name
        )
        labels = {"tenant": session.tenant_id, "tool": entry.canonical_name}
        if not decision.allowed:
            if self.metrics is not None:
                self.metrics.inc("concierge_ratelimit_denied_total", labels=labels)
            self.audit.policy_decision(
                session.session_id, entry.canonical_name, "deny", "rate_limited"
            )
            raise RateLimited.with_retry_after(
                f"rate limit exceeded for {entry.canonical_name}",
                decision.retry_after,
            )
        if self.metrics is not None:
            self.metrics.inc("concierge_ratelimit_allowed_total", labels=labels)

        # 2. Approval gating
        needs_approval = entry.requires_approval or (
            self.block_dangerous and entry.risk_level == RiskLevel.DANGEROUS
        )
        if needs_approval:
            approval_decision = await self.approval.evaluate(session, entry, arguments)
            if not approval_decision.approved:
                reason = approval_decision.reason or "approval_required"
                self.audit.policy_decision(
                    session.session_id, entry.canonical_name, "deny", reason
                )
                # Surface the parked approval id + outcome so a client can map the
                # denial back to the request it queued (and poll/retry on grant).
                data: dict[str, Any] | None = None
                if approval_decision.record is not None:
                    data = {
                        "approval_id": approval_decision.record.approval_id,
                        "status": approval_decision.record.status.value,
                    }
                raise ApprovalRequired(
                    f"{entry.canonical_name} requires approval to invoke",
                    data=data,
                )

        # 3. Auth subject required?
        if entry.requires_auth and not session.auth_subject:
            self.audit.policy_decision(
                session.session_id, entry.canonical_name, "deny", "no_auth_subject"
            )
            raise Forbidden(f"{entry.canonical_name} requires an authenticated subject")

        self.audit.policy_decision(
            session.session_id, entry.canonical_name, "allow"
        )
