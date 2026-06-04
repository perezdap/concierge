"""Policy engine authorization paths."""
from __future__ import annotations

import pytest

from concierge.core.session import SessionManager
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType
from concierge.errors import ApprovalRequired, Forbidden, RateLimited
from concierge.policy.approval import AllowListApprovalBroker, DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import TokenBucketRateLimiter
from concierge.util.audit import AuditLogger


def _entry(**kwargs) -> CatalogEntry:
    defaults = dict(
        server_id="demo",
        upstream_name="alpha",
        canonical_name="demo__alpha",
        primitive_type=PrimitiveType.TOOL,
        transport=TransportType.STDIO,
        display_label="Alpha",
        short_description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        requires_auth=False,
    )
    defaults.update(kwargs)
    return CatalogEntry(**defaults)


@pytest.mark.asyncio
async def test_rate_limit_denies_when_exhausted():
    audit = AuditLogger()
    limiter = TokenBucketRateLimiter(default_capacity=1, default_refill=0.0)
    engine = PolicyEngine(limiter, DenyByDefaultApprovalBroker(), audit)
    session = await SessionManager().create()
    entry = _entry()
    await engine.authorize_call(session, entry, {})
    with pytest.raises(RateLimited):
        await engine.authorize_call(session, entry, {})


@pytest.mark.asyncio
async def test_dangerous_tool_requires_approval():
    audit = AuditLogger()
    engine = PolicyEngine(
        TokenBucketRateLimiter(),
        DenyByDefaultApprovalBroker(),
        audit,
        block_dangerous_without_approval=True,
    )
    session = await SessionManager().create()
    entry = _entry(risk_level=RiskLevel.DANGEROUS)
    with pytest.raises(ApprovalRequired):
        await engine.authorize_call(session, entry, {})


@pytest.mark.asyncio
async def test_requires_auth_subject():
    audit = AuditLogger()
    engine = PolicyEngine(TokenBucketRateLimiter(), DenyByDefaultApprovalBroker(), audit)
    session = await SessionManager().create()
    entry = _entry(requires_auth=True)
    with pytest.raises(Forbidden):
        await engine.authorize_call(session, entry, {})


@pytest.mark.asyncio
async def test_allow_list_permits_named_dangerous_tool():
    audit = AuditLogger()
    entry = _entry(risk_level=RiskLevel.DANGEROUS)
    engine = PolicyEngine(
        TokenBucketRateLimiter(),
        AllowListApprovalBroker([entry.canonical_name]),
        audit,
        block_dangerous_without_approval=True,
    )
    session = await SessionManager().create()
    # On the allow-list → approved, no raise.
    await engine.authorize_call(session, entry, {})


@pytest.mark.asyncio
async def test_allow_list_still_denies_unlisted_tool():
    audit = AuditLogger()
    engine = PolicyEngine(
        TokenBucketRateLimiter(),
        AllowListApprovalBroker(["some__other_tool"]),
        audit,
        block_dangerous_without_approval=True,
    )
    session = await SessionManager().create()
    entry = _entry(risk_level=RiskLevel.DANGEROUS)
    with pytest.raises(ApprovalRequired):
        await engine.authorize_call(session, entry, {})
