"""App factory, admin routes, and optional stdio upstream integration."""
from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

from concierge.config import GatewayHttpConfig, load_config

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "integration", "version": "0.0.1"},
    },
}

# Conftest configures the gateway with these allowed origins.
_ALLOWED_ORIGIN = "http://localhost"
_DISALLOWED_ORIGIN = "http://evil.example"
# Every downstream-facing MCP route that must sit behind the security gates.
_MCP_METHODS = ("post", "get", "delete")


def test_build_app_exposes_state(client: TestClient) -> None:
    assert client.app.state.catalog is not None
    assert client.app.state.service is not None


def test_admin_health(client: TestClient) -> None:
    resp = client.get("/admin/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "catalog_count" in body
    assert "session_count" in body


def test_admin_catalog_and_sessions(client: TestClient) -> None:
    init = client.post("/mcp", json=_INIT)
    sid = init.headers["MCP-Session-Id"]
    assert client.get("/admin/catalog").status_code == 200
    sessions = client.get("/admin/sessions").json()["sessions"]
    assert any(s["session_id"] == sid for s in sessions)


def test_load_config_from_example_yaml(tmp_path) -> None:
    example = tmp_path / "gateway.yaml"
    example.write_text(
        "gateway:\n  port: 9999\nauth:\n  type: none\n",
        encoding="utf-8",
    )
    cfg = load_config(example)
    assert cfg.gateway.port == 9999
    assert cfg.auth.type == "none"


def test_load_config_with_gateway_token_fallback(tmp_path, monkeypatch) -> None:
    # Clear GATEWAY_TOKEN from env to test fallback
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    
    config_file = tmp_path / "gateway.yaml"
    config_file.write_text(
        "gateway:\n  port: 9999\nauth:\n  type: bearer\n  bearer_tokens: [\"${GATEWAY_TOKEN:-}\"]\n",
        encoding="utf-8",
    )
    
    token_file = tmp_path / ".gateway_token"
    token_file.write_text("my-secret-fallback-token", encoding="utf-8")
    
    cfg = load_config(config_file)
    assert os.environ["GATEWAY_TOKEN"] == "my-secret-fallback-token"
    assert cfg.auth.bearer_tokens == ["my-secret-fallback-token"]


def test_echo_stdio_upstream_catalog_refresh(echo_client: TestClient) -> None:
    init = echo_client.post("/mcp", json=_INIT)
    assert init.status_code == 200
    refresh = echo_client.post("/admin/refresh/echo")
    assert refresh.status_code == 200
    data = refresh.json()
    assert data["server"] == "echo"
    assert data["entries"] >= 1
    catalog = echo_client.get("/admin/catalog").json()["entries"]
    names = [e["canonical_name"] for e in catalog]
    assert any("echo" in n for n in names)


def test_echo_tool_call_round_trip(echo_client: TestClient) -> None:
    init = echo_client.post("/mcp", json=_INIT)
    sid = init.headers["MCP-Session-Id"]
    echo_client.post("/admin/refresh/echo")
    discover = echo_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "gateway_discover_catalog",
                "arguments": {},
            },
        },
        headers={"MCP-Session-Id": sid},
    )
    entries = discover.json()["result"]["structuredContent"]["entries"]
    tool_name = next(e["name"] for e in entries if e["name"].endswith("__echo"))
    enable = echo_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "gateway_enable_tools",
                "arguments": {"names": [tool_name]},
            },
        },
        headers={"MCP-Session-Id": sid},
    )
    assert enable.status_code == 200
    call = echo_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"text": "hello-ci"},
            },
        },
        headers={"MCP-Session-Id": sid},
    )
    body = call.json()
    assert body.get("result") is not None
    text_blocks = body["result"].get("content", [])
    assert any("hello-ci" in (b.get("text") or "") for b in text_blocks if b.get("type") == "text")


# --------------------------------------------------------------------------
# P0-1 security gates — every MCP route must pass Origin allow-list + auth
# before any handler (and certainly before GatewayService) is reached.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", _MCP_METHODS)
def test_disallowed_origin_is_rejected_on_every_route(client: TestClient, method: str) -> None:
    """Acceptance (2)+(4): a bad Origin → 403 on POST, GET *and* DELETE.

    DELETE historically skipped the Origin check; the global middleware closes
    that bypass so no route reaches a handler with an untrusted Origin.
    """
    headers = {"Origin": _DISALLOWED_ORIGIN, "MCP-Session-Id": "irrelevant"}
    resp = getattr(client, method)("/mcp", headers=headers)
    assert resp.status_code == 403
    assert "origin" in resp.text.lower()


@pytest.mark.parametrize("method", _MCP_METHODS)
def test_allowed_origin_passes_the_origin_gate(client: TestClient, method: str) -> None:
    """A listed Origin is not blocked by the origin gate (no 403)."""
    headers = {"Origin": _ALLOWED_ORIGIN, "MCP-Session-Id": "irrelevant"}
    resp = getattr(client, method)("/mcp", headers=headers)
    assert resp.status_code != 403


def test_unauthenticated_request_is_rejected_on_every_route(bearer_client: TestClient) -> None:
    """Acceptance (1)+(4): no Authorization → 401 on every MCP route.

    Each route is exercised on the path that proves an *established* session
    cannot be driven without auth (POST/DELETE carry a session id so they reach
    the auth check rather than the session-create branch). GatewayService is
    never reached.
    """
    # POST on an existing session → explicit 401 (not the initialize branch).
    post = bearer_client.post(
        "/mcp",
        headers={"Origin": _ALLOWED_ORIGIN, "MCP-Session-Id": "irrelevant"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert post.status_code == 401

    get = bearer_client.get("/mcp", headers={"Origin": _ALLOWED_ORIGIN})
    assert get.status_code == 401

    delete = bearer_client.delete(
        "/mcp", headers={"Origin": _ALLOWED_ORIGIN, "MCP-Session-Id": "irrelevant"}
    )
    assert delete.status_code == 401


def test_good_origin_and_auth_succeeds(bearer_client: TestClient) -> None:
    """Acceptance (3): allowed Origin + valid bearer token → 200 + session id."""
    resp = bearer_client.post(
        "/mcp",
        headers={"Origin": _ALLOWED_ORIGIN, "Authorization": "Bearer test-secret-token"},
        json=_INIT,
    )
    assert resp.status_code == 200
    assert resp.headers.get("MCP-Session-Id")


def test_good_origin_without_auth_required_succeeds(client: TestClient) -> None:
    """Allowed Origin + auth=none → 200 (the gate does not over-block)."""
    resp = client.post("/mcp", headers={"Origin": _ALLOWED_ORIGIN}, json=_INIT)
    assert resp.status_code == 200
    assert resp.headers.get("MCP-Session-Id")


def test_default_binding_is_loopback_only() -> None:
    """Acceptance (6): the gateway binds to localhost by default (not public)."""
    cfg = GatewayHttpConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.bind_public is False
