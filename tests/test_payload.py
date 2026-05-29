"""Phase 2 — payload & schema trimming."""
import json
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
from concierge.util.payload import PayloadOptions, cap_result_text, slim_schema


# --------------------------------------------------------------------------- #
# Unit: slim_schema
# --------------------------------------------------------------------------- #

VERBOSE_SCHEMA = {
    "type": "object",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "properties": {
        "query": {
            "type": "string",
            "description": "x" * 500,
            "examples": ["foo", "bar", "a very long example string here"],
        },
        "limit": {"type": "integer", "default": 25, "$comment": "internal note"},
    },
    "required": ["query"],
}


def test_slim_schema_drops_examples_and_caps_descriptions():
    slim = slim_schema(VERBOSE_SCHEMA, max_desc=160, drop_examples=True)
    # verbose keys gone
    assert "$schema" not in slim
    assert "examples" not in slim["properties"]["query"]
    assert "$comment" not in slim["properties"]["limit"]
    # description capped
    assert len(slim["properties"]["query"]["description"]) <= 161  # 160 + ellipsis
    assert slim["properties"]["query"]["description"].endswith("…")
    # structure a model needs to CALL the tool is preserved
    assert slim["type"] == "object"
    assert slim["required"] == ["query"]
    assert slim["properties"]["query"]["type"] == "string"
    assert slim["properties"]["limit"]["default"] == 25


def test_slim_schema_is_smaller():
    slim = slim_schema(VERBOSE_SCHEMA)
    assert len(json.dumps(slim)) < len(json.dumps(VERBOSE_SCHEMA))


def test_slim_schema_passthrough_non_dict():
    assert slim_schema(None) is None
    assert slim_schema("x") == "x"


# --------------------------------------------------------------------------- #
# Unit: cap_result_text
# --------------------------------------------------------------------------- #

def test_cap_result_text_disabled_by_default():
    result = {"content": [{"type": "text", "text": "x" * 1000}]}
    assert cap_result_text(result, 0) is result


def test_cap_result_text_truncates_and_marks():
    result = {"content": [{"type": "text", "text": "x" * 1000}]}
    out = cap_result_text(result, 100)
    assert out is not result
    assert out["_meta"]["gateway_truncated"] is True
    assert "[truncated by gateway]" in out["content"][0]["text"]
    assert len(out["content"][0]["text"].encode("utf-8")) < 1000


def test_cap_result_text_leaves_small_results_untouched():
    result = {"content": [{"type": "text", "text": "small"}]}
    assert cap_result_text(result, 100) is result


# --------------------------------------------------------------------------- #
# Integration: tools/list slimming + discovery compaction
# --------------------------------------------------------------------------- #

class VerboseAdapter(UpstreamAdapter):
    transport = TransportType.STDIO

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self._connected = True

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self):
        return [{
            "name": "search",
            "title": "Search",
            "description": "Search things.",
            "inputSchema": VERBOSE_SCHEMA,
        }]

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}

    async def read_resource(self, uri):
        return {}

    async def get_prompt(self, name, arguments=None):
        return {}

    def health(self):
        return AdapterHealth(server_id=self.server_id, transport=self.transport, connected=True)


async def _build_service(payload: PayloadOptions | None = None) -> GatewayService:
    catalog = Catalog()
    bus = NotificationBus(coalesce_window_s=0)
    sessions = SessionManager()
    publishing = PublishingService(catalog, bus)
    adapters = AdapterManager(catalog)
    adapters.register(VerboseAdapter("demo"))
    await adapters.refresh_server("demo")
    audit = AuditLogger()
    policy = PolicyEngine(TokenBucketRateLimiter(), DenyByDefaultApprovalBroker(), audit)
    return GatewayService(
        catalog, publishing, sessions, adapters, policy, audit, bus,
        ProfileRegistry(), payload=payload,
    )


# NOTE: the catalog allowlist (validate_input_schema) already strips examples /
# $schema / $comment / default at catalog time and caps descriptions at 600.
# The gateway-side slim adds a *tighter* description cap on the model-facing
# tools/list surface — that tighter cap is the observable difference below.

@pytest.mark.asyncio
async def test_tools_list_preserves_published_schema_by_default():
    svc = await _build_service()
    s = await svc.sessions.create()
    await svc.tools_call(s, {"name": "gateway_enable_tools", "arguments": {"names": ["demo.search"]}})
    res = await svc.tools_list(s)
    tool = next(t for t in res["tools"] if t["name"] == "demo.search")
    # without opt-in gateway slimming the catalog-level description (500) is preserved
    assert len(tool["inputSchema"]["properties"]["query"]["description"]) == 500


@pytest.mark.asyncio
async def test_tools_list_slims_published_schema_when_enabled():
    svc = await _build_service(PayloadOptions(slim_tools_list=True))
    s = await svc.sessions.create()
    await svc.tools_call(s, {"name": "gateway_enable_tools", "arguments": {"names": ["demo.search"]}})
    res = await svc.tools_list(s)

    tool = next(t for t in res["tools"] if t["name"] == "demo.search")
    schema = tool["inputSchema"]
    desc = schema["properties"]["query"]["description"]
    assert len(desc) <= 161 and desc.endswith("…")     # tightened to 160
    # still callable: required + property types intact
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"


@pytest.mark.asyncio
async def test_discovery_omits_null_and_empty_fields():
    svc = await _build_service()
    s = await svc.sessions.create()
    res = await svc.tools_call(s, {"name": "gateway_discover_catalog", "arguments": {}})
    entry = next(e for e in res["structuredContent"]["entries"] if e["name"] == "demo.search")
    # usage_guidance is None upstream -> key omitted entirely
    assert "when_to_use" not in entry
    # default-false safety flags omitted
    assert "requires_approval" not in entry
    assert "requires_auth" not in entry
    # high-signal fields always present
    assert entry["risk"] and entry["purpose"] and entry["server"]
