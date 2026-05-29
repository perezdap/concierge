"""Phase 4 — metrics/audit hooks: payload sizes + session/pool lifecycle."""
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.manager import AdapterManager
from concierge.core.catalog import Catalog
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.core.types import AdapterHealth, TransportType
from concierge.gateway.profiles import ProfileRegistry
from concierge.gateway.service import GatewayService
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import TokenBucketRateLimiter
from concierge.util.audit import AuditLogger


class RecordingAudit(AuditLogger):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def of(self, event: str) -> list[dict[str, Any]]:
        return [f for e, f in self.events if e == event]


class OneToolAdapter(UpstreamAdapter):
    transport = TransportType.STREAMABLE_HTTP

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self._connected = True

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self):
        return [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object"}}]

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": "x" * 50}]}

    async def read_resource(self, uri):
        return {}

    async def get_prompt(self, name, arguments=None):
        return {}

    def health(self):
        return AdapterHealth(server_id=self.server_id, transport=self.transport, connected=True)


@pytest.mark.asyncio
async def test_tool_call_records_payload_sizes():
    catalog = Catalog()
    audit = RecordingAudit()
    adapters = AdapterManager(catalog, audit=audit)
    adapters.register(OneToolAdapter("demo"))
    await adapters.refresh_server("demo")
    publishing = PublishingService(catalog, NotificationBus(coalesce_window_s=0))
    svc = GatewayService(
        catalog, publishing, SessionManager(), adapters,
        PolicyEngine(TokenBucketRateLimiter(), DenyByDefaultApprovalBroker(), audit),
        audit, publishing.bus, ProfileRegistry(),
    )
    s = await svc.sessions.create()
    await svc.tools_call(s, {"name": "gateway_enable_tools", "arguments": {"names": ["demo.echo"]}})
    await svc.tools_call(s, {"name": "demo.echo", "arguments": {"text": "hello"}})

    calls = audit.of("tool.call")
    assert len(calls) == 1
    assert isinstance(calls[0]["request_bytes"], int) and calls[0]["request_bytes"] > 0
    assert isinstance(calls[0]["response_bytes"], int) and calls[0]["response_bytes"] > 50
    assert calls[0]["ok"] is True


def _per_session_manager(audit, **kw):
    catalog = Catalog()
    adapters = AdapterManager(catalog, audit=audit, **kw)
    adapters.register(
        OneToolAdapter("demo"),
        isolation="per_session",
        factory=lambda: OneToolAdapter("demo"),
        connect_backoff_base_s=0.001,
    )
    return adapters


@pytest.mark.asyncio
async def test_upstream_session_created_and_evicted_events():
    audit = RecordingAudit()
    adapters = _per_session_manager(audit)

    await adapters.call_tool("demo", "echo", {}, router_session_id="s1")
    created = audit.of("upstream_session.created")
    assert created and created[0]["router_session_id"] == "s1"

    await adapters.evict_router_session("s1")
    evicted = audit.of("upstream_session.evicted")
    assert evicted and evicted[0]["server_id"] == "demo"


@pytest.mark.asyncio
async def test_upstream_session_lru_eviction_event():
    audit = RecordingAudit()
    adapters = _per_session_manager(audit, max_upstream_sessions=1)

    await adapters.call_tool("demo", "echo", {}, router_session_id="s1")
    await adapters.call_tool("demo", "echo", {}, router_session_id="s2")  # evicts s1

    lru = audit.of("upstream_session.lru_evicted")
    assert lru and lru[0]["router_session_id"] == "s1"


@pytest.mark.asyncio
async def test_session_evicted_event_via_on_evict_composition():
    """Mirrors app.py wiring: on_evict tears down pool + audits the eviction."""
    audit = RecordingAudit()
    adapters = _per_session_manager(audit)

    async def on_evict(sid: str) -> None:
        freed = await adapters.evict_router_session(sid)
        audit.session_evicted(sid, upstream_sessions_freed=freed)

    sessions = SessionManager(idle_ttl_seconds=-1, on_evict=on_evict)
    s = await sessions.create()
    await adapters.call_tool("demo", "echo", {}, router_session_id=s.session_id)

    await sessions.gc()
    ev = audit.of("session.evicted")
    assert ev and ev[0]["session_id"] == s.session_id
    assert ev[0]["upstream_sessions_freed"] == 1
