"""App-level wiring tests for P1-8 output filtering and caching."""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.custom import register_custom_adapter
from concierge.config import CacheConfig, GatewayConfig, OutputConfig, UpstreamServerConfig
from concierge.core.types import AdapterHealth, TransportType
from concierge.server.app import build_app

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "p1-8-test", "version": "0.0.1"},
    },
}


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    rpc_id: int = 1,
):
    headers: dict[str, str] = {}
    if session_id:
        headers["MCP-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
    return client.post("/mcp", json=payload, headers=headers)


class P18FakeAdapter(UpstreamAdapter):
    transport = TransportType.CUSTOM

    def __init__(self, *, server_id: str) -> None:
        self.server_id = server_id
        self.connected = False
        self.read_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "leak_secret",
                "description": "returns a fake secret for output-filter wiring tests",
                "inputSchema": {"type": "object"},
            }
        ]

    async def list_resources(self) -> list[dict[str, Any]]:
        return [{"uri": "resource://counter", "name": "counter", "description": "counter"}]

    async def list_prompts(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "secret is sk-1234567890abcdef"}]}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        self.read_count += 1
        return {"contents": [{"uri": uri, "text": f"read #{self.read_count}"}]}

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            server_id=self.server_id,
            transport=self.transport,
            connected=self.connected,
        )


def _build_test_app(*, output: OutputConfig | None = None, cache: CacheConfig | None = None):
    register_custom_adapter("p1-8-fake", P18FakeAdapter)
    cfg = GatewayConfig(
        auth={"type": "none"},
        upstream_servers=[
            UpstreamServerConfig(
                id="p18",
                transport="custom",
                custom_kind="p1-8-fake",
            )
        ],
        output=output or OutputConfig(),
        cache=cache or CacheConfig(),
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )
    return build_app(cfg)


def test_build_app_wires_enabled_output_filter_into_tool_calls() -> None:
    app = _build_test_app(output=OutputConfig(enable_output_filter=True))
    with TestClient(app) as client:
        init = _rpc(client, "initialize", _INIT["params"])
        session_id = init.headers["MCP-Session-Id"]
        enable = _rpc(
            client,
            "tools/call",
            {"name": "gateway_enable_tools", "arguments": {"names": ["p18__leak_secret"]}},
            session_id=session_id,
        )
        assert enable.status_code == 200

        call = _rpc(
            client,
            "tools/call",
            {"name": "p18__leak_secret", "arguments": {}},
            session_id=session_id,
        )

    text = call.json()["result"]["content"][0]["text"]
    assert "sk-1234567890abcdef" not in text
    assert "[REDACTED" in text


def test_build_app_wraps_adapters_with_enabled_cache() -> None:
    app = _build_test_app(cache=CacheConfig(enable_cache=True, default_ttl_s=60))
    with TestClient(app) as client:
        init = _rpc(client, "initialize", _INIT["params"])
        session_id = init.headers["MCP-Session-Id"]
        enable = _rpc(
            client,
            "tools/call",
            {"name": "gateway_enable_tools", "arguments": {"names": ["p18__counter"]}},
            session_id=session_id,
        )
        assert enable.status_code == 200

        first = _rpc(
            client,
            "resources/read",
            {"name": "p18__counter"},
            session_id=session_id,
        )
        second = _rpc(
            client,
            "resources/read",
            {"name": "p18__counter"},
            session_id=session_id,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["result"] == second.json()["result"]
    assert first.json()["result"]["contents"][0]["text"] == "read #1"
