"""FastAPI facade integration tests (auth, origin, batch, errors)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from concierge.errors import JSONRPC_INVALID_REQUEST, JSONRPC_PARSE_ERROR
from concierge.gateway.service import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.1"},
    },
}


def _post_mcp(
    client: TestClient,
    payload: dict | list,
    *,
    session_id: str | None = None,
    origin: str | None = None,
    authorization: str | None = None,
):
    headers: dict[str, str] = {}
    if session_id:
        headers["MCP-Session-Id"] = session_id
    if origin is not None:
        headers["Origin"] = origin
    if authorization:
        headers["Authorization"] = authorization
    return client.post("/mcp", json=payload, headers=headers)


def test_initialize_returns_session_and_capabilities(client: TestClient) -> None:
    resp = _post_mcp(client, _INIT)
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "concierge"
    assert "MCP-Session-Id" in resp.headers
    assert resp.headers["MCP-Session-Id"]


def test_tools_list_requires_session(client: TestClient) -> None:
    resp = _post_mcp(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert resp.status_code == 200
    err = resp.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["reason"] == "session_missing_header"
    assert err["data"]["recoverable"] is False
    assert err["data"]["operator_hint"]


def test_stale_session_returns_structured_error(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    del_resp = client.delete("/mcp", headers={"MCP-Session-Id": sid})
    assert del_resp.status_code == 204
    resp = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        session_id=sid,
    )
    assert resp.status_code == 200
    err = resp.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["reason"] == "session_not_found"
    assert err["data"]["recoverable"] is True
    assert "initialize" in err["data"]["operator_hint"].lower()


def test_bearer_rejects_unauthenticated_initialize(bearer_client: TestClient) -> None:
    resp = _post_mcp(bearer_client, _INIT)
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32001


def test_bearer_accepts_valid_token(bearer_client: TestClient) -> None:
    resp = _post_mcp(
        bearer_client,
        _INIT,
        authorization="Bearer test-secret-token",
    )
    assert resp.status_code == 200
    assert "MCP-Session-Id" in resp.headers


def test_bearer_http_401_when_session_header_and_bad_token(bearer_client: TestClient) -> None:
    init = _post_mcp(
        bearer_client,
        _INIT,
        authorization="Bearer test-secret-token",
    )
    sid = init.headers["MCP-Session-Id"]
    resp = _post_mcp(
        bearer_client,
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        session_id=sid,
        authorization="Bearer wrong-token",
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == -32001


def test_unknown_method_returns_jsonrpc_error(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 8, "method": "nope/method", "params": {}},
        session_id=sid,
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601


def test_origin_rejects_evil_localhost_prefix(client: TestClient) -> None:
    resp = _post_mcp(client, _INIT, origin="http://localhost.evil.com")
    assert resp.status_code == 403


def test_origin_rejects_null_unless_allowlisted(client: TestClient) -> None:
    resp = _post_mcp(client, _INIT, origin="null")
    assert resp.status_code == 403


def test_origin_allows_listed_localhost(client: TestClient) -> None:
    resp = _post_mcp(client, _INIT, origin="http://localhost")
    assert resp.status_code == 200


def test_parse_error_envelope(client: TestClient) -> None:
    resp = client.post("/mcp", content=b"not-json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == JSONRPC_PARSE_ERROR


def test_batch_initialize_returns_array(client: TestClient) -> None:
    batch = [_INIT, {**_INIT, "id": 2}]
    resp = _post_mcp(client, batch)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert all(item.get("result") for item in body)


def test_notification_without_id_returns_202_accepted(client: TestClient) -> None:
    """P0-4: Pure notification POSTs must return 202 Accepted with no body."""
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    resp = _post_mcp(client, note, session_id=sid)
    assert resp.status_code == 202
    # No body (or empty) for 202 notification responses
    assert resp.content in (b"", b"null") or resp.text in ("", "null")


def test_ping_after_initialize(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}},
        session_id=sid,
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {}


def test_delete_session(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    del_resp = client.delete("/mcp", headers={"MCP-Session-Id": sid})
    assert del_resp.status_code == 204
    again = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        session_id=sid,
    )
    assert again.status_code == 200
    err = again.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["reason"] == "session_not_found"
    assert err["data"]["recoverable"] is True


def test_batch_invalid_item_returns_parse_error(client: TestClient) -> None:
    resp = _post_mcp(client, [{"jsonrpc": "2.0", "method": 123, "id": 1}])
    assert resp.status_code == 200
    err = resp.json()[0]["error"]
    assert err["code"] == JSONRPC_PARSE_ERROR


def test_get_mcp_requires_session(client: TestClient) -> None:
    resp = client.get("/mcp")
    assert resp.status_code == 400


# --- P0-3/P0-4 protocol version negotiation -------------------------------


def test_initialize_echoes_supported_protocol_version(client: TestClient) -> None:
    """initialize replies with a version we actually support."""
    resp = _post_mcp(client, _INIT)
    assert resp.status_code == 200
    agreed = resp.json()["result"]["protocolVersion"]
    assert agreed == PROTOCOL_VERSION
    assert agreed in SUPPORTED_PROTOCOL_VERSIONS


def test_initialize_negotiates_down_for_unsupported_client_version(client: TestClient) -> None:
    """An unsupported client protocolVersion negotiates to a supported one (no hard fail)."""
    init = {**_INIT, "params": {**_INIT["params"], "protocolVersion": "1999-01-01"}}
    resp = _post_mcp(client, init)
    assert resp.status_code == 200
    assert resp.json()["result"]["protocolVersion"] in SUPPORTED_PROTOCOL_VERSIONS


def test_unsupported_protocol_version_header_returns_400(client: TestClient) -> None:
    """Post-initialize requests with an unsupported MCP-Protocol-Version header are rejected clearly."""
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 11, "method": "ping", "params": {}},
        headers={"MCP-Session-Id": sid, "MCP-Protocol-Version": "1999-01-01"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == JSONRPC_INVALID_REQUEST
    assert "1999-01-01" in err["message"]


def test_supported_protocol_version_header_accepted(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 12, "method": "ping", "params": {}},
        headers={"MCP-Session-Id": sid, "MCP-Protocol-Version": PROTOCOL_VERSION},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == {}


def test_missing_protocol_version_header_allowed_for_backcompat(client: TestClient) -> None:
    """Absent header stays permissive (older clients / non-browser SDKs)."""
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 13, "method": "ping", "params": {}},
        session_id=sid,
    )
    assert resp.status_code == 200


def test_get_stream_rejects_unsupported_protocol_version(client: TestClient) -> None:
    init = _post_mcp(client, _INIT)
    sid = init.headers["MCP-Session-Id"]
    resp = client.get(
        "/mcp",
        headers={"MCP-Session-Id": sid, "MCP-Protocol-Version": "1999-01-01"},
    )
    assert resp.status_code == 400

