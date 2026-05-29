"""Phase 3 — discovery heuristics (profile scoping/listing) + list_changed."""
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.manager import AdapterManager
from concierge.core.catalog import Catalog
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.core.types import AdapterHealth, PrimitiveType, TransportType
from concierge.gateway.profiles import Profile, ProfileRegistry, ProfileSelector
from concierge.gateway.service import GatewayService
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import TokenBucketRateLimiter
from concierge.util.audit import AuditLogger


class MultiToolAdapter(UpstreamAdapter):
    """One upstream exposing a 'read' tool and a 'write' tool (different tags)."""
    transport = TransportType.STDIO

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self._connected = True

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self):
        return [
            {"name": "alpha", "description": "Read alpha.", "inputSchema": {"type": "object"}},
            {"name": "beta", "description": "Write beta.", "inputSchema": {"type": "object"}},
        ]

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name, arguments):
        return {"content": []}

    async def read_resource(self, uri):
        return {}

    async def get_prompt(self, name, arguments=None):
        return {}

    def health(self):
        return AdapterHealth(server_id=self.server_id, transport=self.transport, connected=True)


async def _svc(*, auto_apply_readonly: bool = False) -> GatewayService:
    catalog = Catalog()
    bus = NotificationBus(coalesce_window_s=0)
    sessions = SessionManager()
    publishing = PublishingService(catalog, bus)
    adapters = AdapterManager(catalog)
    adapters.register(MultiToolAdapter("demo"), default_tags=["read"])
    await adapters.refresh_server("demo")
    # Manually tag beta as a "write" tool so a profile can select a subset.
    beta = catalog.get("demo.beta")
    beta.tags = ["write"]
    catalog.upsert(beta)

    audit = AuditLogger()
    policy = PolicyEngine(TokenBucketRateLimiter(), DenyByDefaultApprovalBroker(), audit)
    profiles = ProfileRegistry()
    profiles.register(Profile(
        name="readonly", description="Read tools only.",
        selectors=[ProfileSelector(tags=["read"])],
        auto_apply=auto_apply_readonly,
    ))
    return GatewayService(catalog, publishing, sessions, adapters, policy, audit, bus, profiles)


@pytest.mark.asyncio
async def test_discover_scoped_to_profile():
    svc = await _svc()
    s = await svc.sessions.create()
    res = await svc.tools_call(s, {
        "name": "gateway_discover_catalog",
        "arguments": {"profile": "readonly"},
    })
    names = [e["name"] for e in res["structuredContent"]["entries"]]
    assert names == ["demo.alpha"]          # beta (write) excluded by profile scope


@pytest.mark.asyncio
async def test_discover_unknown_profile_is_invalid_params():
    from concierge.errors import InvalidParams
    svc = await _svc()
    s = await svc.sessions.create()
    with pytest.raises(InvalidParams):
        await svc.tools_call(s, {
            "name": "gateway_discover_catalog",
            "arguments": {"profile": "nope"},
        })


@pytest.mark.asyncio
async def test_list_profiles_reports_counts_and_sample():
    svc = await _svc()
    s = await svc.sessions.create()
    res = await svc.tools_call(s, {"name": "gateway_list_profiles", "arguments": {}})
    profiles = res["structuredContent"]["profiles"]
    ro = next(p for p in profiles if p["name"] == "readonly")
    assert ro["enables_count"] == 1
    assert ro["sample"] == ["demo.alpha"]
    assert ro["description"] == "Read tools only."


@pytest.mark.asyncio
async def test_list_profiles_is_a_native_primitive():
    svc = await _svc()
    s = await svc.sessions.create()
    res = await svc.tools_list(s)
    names = [t["name"] for t in res["tools"]]
    assert "gateway_list_profiles" in names


# --------------------------------------------------------------------------- #
# notifications/list_changed coalescing + correctness
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_bus_coalesces_same_method_in_window():
    bus = NotificationBus(coalesce_window_s=10)
    bus.tools_list_changed("s")
    bus.tools_list_changed("s")        # within window -> folded
    assert bus.queue_for("s").qsize() == 1


@pytest.mark.asyncio
async def test_bus_keeps_distinct_methods():
    bus = NotificationBus(coalesce_window_s=10)
    bus.tools_list_changed("s")
    bus.resources_list_changed("s")    # different method -> not coalesced
    assert bus.queue_for("s").qsize() == 2


@pytest.mark.asyncio
async def test_rapid_enable_disable_coalesces_to_one():
    svc = await _svc()
    # Swap in a coalescing bus shared by publishing.
    bus = NotificationBus(coalesce_window_s=10)
    publishing = PublishingService(svc.catalog, bus)
    s = await svc.sessions.create()

    publishing.enable(s, ["demo.alpha"])
    publishing.disable(s, ["demo.alpha"])   # rapid tools mutation
    assert bus.queue_for(s.session_id).qsize() == 1


# --------------------------------------------------------------------------- #
# auto_apply profiles — published at session init
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_auto_apply_profile_published_in_first_tools_list():
    svc = await _svc(auto_apply_readonly=True)
    s = await svc.sessions.create()
    await svc.initialize(s, {})

    names = [t["name"] for t in (await svc.tools_list(s))["tools"]]
    assert "demo.alpha" in names        # read tool auto-published at init
    assert "demo.beta" not in names     # write tool excluded by the profile
    assert "readonly" in s.active_profiles


@pytest.mark.asyncio
async def test_auto_applied_tool_is_callable_without_manual_enable():
    svc = await _svc(auto_apply_readonly=True)
    s = await svc.sessions.create()
    await svc.initialize(s, {})
    # No gateway_enable_tools call — the auto profile already published it.
    res = await svc.tools_call(s, {"name": "demo.alpha", "arguments": {}})
    assert res == {"content": []}


@pytest.mark.asyncio
async def test_non_auto_profile_does_not_publish_at_init():
    svc = await _svc(auto_apply_readonly=False)
    s = await svc.sessions.create()
    await svc.initialize(s, {})

    names = [t["name"] for t in (await svc.tools_list(s))["tools"]]
    assert "demo.alpha" not in names    # nothing auto-published
    assert s.active_profiles == []
