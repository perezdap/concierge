"""End-to-end test of gateway-native primitives through GatewayService."""
import asyncio
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.manager import AdapterManager
from concierge.core.catalog import Catalog
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.core.types import AdapterHealth, TransportType
from concierge.gateway.profiles import Profile, ProfileRegistry, ProfileSelector
from concierge.gateway.service import GatewayService
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import TokenBucketRateLimiter
from concierge.util.audit import AuditLogger


class FakeAdapter(UpstreamAdapter):
    transport = TransportType.STDIO

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self._connected = True

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "ping",
                "title": "Ping",
                "description": "Replies with pong.",
                "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}},
            }
        ]

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": f"pong:{arguments.get('msg', '')}"}]}

    async def read_resource(self, uri: str):
        return {}

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None):
        return {}

    def health(self) -> AdapterHealth:
        return AdapterHealth(server_id=self.server_id, transport=self.transport, connected=self._connected)


@pytest.fixture
async def svc():
    catalog = Catalog()
    bus = NotificationBus(coalesce_window_s=0)
    sessions = SessionManager()
    publishing = PublishingService(catalog, bus)
    adapters = AdapterManager(catalog)
    adapters.register(FakeAdapter("demo"))
    await adapters.refresh_server("demo")
    audit = AuditLogger()
    policy = PolicyEngine(TokenBucketRateLimiter(), DenyByDefaultApprovalBroker(), audit)
    profiles = ProfileRegistry()
    profiles.register(Profile(name="demo-all", selectors=[ProfileSelector(server="demo")]))
    return GatewayService(catalog, publishing, sessions, adapters, policy, audit, bus, profiles)


@pytest.mark.asyncio
async def test_initial_tools_list_is_small(svc):
    s = await svc.sessions.create()
    res = await svc.tools_list(s)
    names = [t["name"] for t in res["tools"]]
    # Only the gateway-native primitives — no demo.ping yet.
    assert "demo.ping" not in names
    assert "gateway_discover_catalog" in names
    assert "gateway_enable_tools" in names


@pytest.mark.asyncio
async def test_discover_then_enable_then_call(svc):
    s = await svc.sessions.create()
    # discover
    res = await svc.tools_call(s, {"name": "gateway_discover_catalog", "arguments": {}})
    entries = res["structuredContent"]["entries"]
    names = [e["name"] for e in entries]
    assert "demo.ping" in names

    # enable
    res = await svc.tools_call(s, {"name": "gateway_enable_tools", "arguments": {"names": ["demo.ping"]}})
    assert res["structuredContent"]["enabled"] == ["demo.ping"]

    # tools/list now includes demo.ping
    res = await svc.tools_list(s)
    names = [t["name"] for t in res["tools"]]
    assert "demo.ping" in names

    # call_tool routes to the fake adapter
    res = await svc.tools_call(s, {"name": "demo.ping", "arguments": {"msg": "hi"}})
    assert res["content"][0]["text"] == "pong:hi"


@pytest.mark.asyncio
async def test_call_not_published_raises(svc):
    from concierge.errors import NotPublished
    s = await svc.sessions.create()
    with pytest.raises(NotPublished):
        await svc.tools_call(s, {"name": "demo.ping", "arguments": {}})


@pytest.mark.asyncio
async def test_profile_enables_bundle(svc):
    s = await svc.sessions.create()
    res = await svc.tools_call(s, {"name": "gateway_use_profile", "arguments": {"profile": "demo-all"}})
    assert "demo.ping" in res["structuredContent"]["enabled"]
    assert "demo-all" in s.active_profiles


@pytest.mark.asyncio
async def test_list_changed_notification_fired_on_enable(svc):
    s = await svc.sessions.create()
    await svc.tools_call(s, {"name": "gateway_enable_tools", "arguments": {"names": ["demo.ping"]}})
    msg = await asyncio.wait_for(svc.bus.queue_for(s.session_id).get(), timeout=1)
    assert msg["method"] == "notifications/tools/list_changed"
