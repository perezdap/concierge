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
from .ratelimit import TokenBucketRateLimiter


class PolicyEngine:
    def __init__(
        self,
        rate_limiter: TokenBucketRateLimiter,
        approval: ApprovalBroker,
        audit: AuditLogger,
        *,
        block_dangerous_without_approval: bool = True,
    ) -> None:
        self.rate_limiter = rate_limiter
        self.approval = approval
        self.audit = audit
        self.block_dangerous = block_dangerous_without_approval

    async def authorize_call(
        self,
        session: Session,
        entry: CatalogEntry,
        arguments: dict[str, Any],
    ) -> None:
        # 1. Rate limit
        if not self.rate_limiter.allow(session.session_id, entry.canonical_name):
            self.audit.policy_decision(
                session.session_id, entry.canonical_name, "deny", "rate_limited"
            )
            raise RateLimited(f"rate limit exceeded for {entry.canonical_name}")

        # 2. Approval gating
        needs_approval = entry.requires_approval or (
            self.block_dangerous and entry.risk_level == RiskLevel.DANGEROUS
        )
        if needs_approval:
            ok = await self.approval.is_approved(session, entry, arguments)
            if not ok:
                self.audit.policy_decision(
                    session.session_id, entry.canonical_name, "deny", "approval_required"
                )
                raise ApprovalRequired(
                    f"{entry.canonical_name} requires approval to invoke"
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
