"""P1-3 end-to-end approval workflow through the FastAPI facade.

Acceptance: a dangerous/requires-approval tool call PARKS a pending record and
the request blocks; an operator grants out-of-band over the HTTP admin surface;
the parked call then resumes and returns the upstream result. Companion tests
cover the deny path, the TTL-expiry denial, the MCP-native pending list, and a
signature-verifiable webhook fired on the decision.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.custom import register_custom_adapter
from concierge.config import (
    ApprovalConfig,
    GatewayConfig,
    PolicyConfig,
    UpstreamServerConfig,
    WebhookConfig,
)
from concierge.core.types import AdapterHealth, TransportType
from concierge.policy.webhook import SIGNATURE_HEADER, verify_signature
from concierge.server.app import build_app

_INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "p1-3-e2e", "version": "0.0.1"},
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


class _DangerAdapter(UpstreamAdapter):
    """One tool, "danger", whose upstream result proves the call actually ran."""

    transport = TransportType.CUSTOM

    def __init__(self, *, server_id: str) -> None:
        self.server_id = server_id
        self.connected = False
        self.calls = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def initialize(self) -> dict[str, Any]:
        return {}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "danger",
                "description": "a tool requiring operator approval",
                "inputSchema": {"type": "object"},
            }
        ]

    async def list_resources(self) -> list[dict[str, Any]]:
        return []

    async def list_prompts(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"content": [{"type": "text", "text": "DANGER EXECUTED"}]}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return {}

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            server_id=self.server_id, transport=self.transport, connected=self.connected
        )


def _build(*, approval: ApprovalConfig | None = None) -> GatewayConfig:
    register_custom_adapter("p1-3-danger", _DangerAdapter)
    return GatewayConfig(
        auth={"type": "none"},
        upstream_servers=[
            UpstreamServerConfig(
                id="srv",
                transport="custom",
                custom_kind="p1-3-danger",
                requires_approval_for=["danger"],
            )
        ],
        policy=PolicyConfig(
            approval_mode="queue",
            approval=approval or ApprovalConfig(backend="memory"),
        ),
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )


def _session(client: TestClient) -> str:
    init = _rpc(client, "initialize", _INIT_PARAMS)
    sid = init.headers["MCP-Session-Id"]
    enable = _rpc(
        client,
        "tools/call",
        {"name": "gateway_enable_tools", "arguments": {"names": ["srv__danger"]}},
        session_id=sid,
    )
    assert enable.status_code == 200
    return sid


def test_grant_resumes_parked_call_and_executes_upstream() -> None:
    cfg = _build(approval=ApprovalConfig(backend="memory", poll_interval_s=0.05))
    app = build_app(cfg)
    with TestClient(app) as client:
        sid = _session(client)
        result: dict[str, Any] = {}

        def _call() -> None:
            # This blocks inside the policy engine until the approval is decided.
            resp = _rpc(
                client,
                "tools/call",
                {"name": "srv__danger", "arguments": {"x": 1}},
                session_id=sid,
                rpc_id=99,
            )
            result["resp"] = resp

        caller = threading.Thread(target=_call)
        caller.start()
        try:
            # Poll the admin queue until the call has parked, then grant it.
            approval_id = None
            for _ in range(200):
                listed = client.get("/admin/approvals").json()["approvals"]
                if listed:
                    approval_id = listed[0]["approval_id"]
                    break
                time.sleep(0.02)
            assert approval_id is not None, "call never parked a pending approval"

            grant = client.post("/admin/approvals/grant", json={"approval_id": approval_id})
            assert grant.status_code == 200
            assert grant.json()["approval"]["status"] == "granted"
        finally:
            caller.join(timeout=10)

        assert "resp" in result, "parked call never returned"
        body = result["resp"].json()
        assert "error" not in body, body
        assert body["result"]["content"][0]["text"] == "DANGER EXECUTED"


def test_deny_returns_structured_denial() -> None:
    cfg = _build(approval=ApprovalConfig(backend="memory", poll_interval_s=0.05))
    app = build_app(cfg)
    with TestClient(app) as client:
        sid = _session(client)
        result: dict[str, Any] = {}

        def _call() -> None:
            result["resp"] = _rpc(
                client,
                "tools/call",
                {"name": "srv__danger", "arguments": {}},
                session_id=sid,
                rpc_id=42,
            )

        caller = threading.Thread(target=_call)
        caller.start()
        try:
            approval_id = None
            for _ in range(200):
                listed = client.get("/admin/approvals").json()["approvals"]
                if listed:
                    approval_id = listed[0]["approval_id"]
                    break
                time.sleep(0.02)
            assert approval_id is not None
            deny = client.post(
                "/admin/approvals/deny",
                json={"approval_id": approval_id, "reason": "nope"},
            )
            assert deny.status_code == 200
            assert deny.json()["approval"]["status"] == "denied"
        finally:
            caller.join(timeout=10)

        body = result["resp"].json()
        assert "result" not in body
        assert body["error"]["code"] == -32004  # GW_APPROVAL_REQUIRED
        assert body["error"]["data"]["status"] == "denied"


def test_ttl_expiry_returns_denial_without_decision() -> None:
    # Tiny TTL so the bounded await elapses with no operator decision.
    cfg = _build(
        approval=ApprovalConfig(
            backend="memory", ttl_s=0.3, wait_timeout_s=0.3, poll_interval_s=0.05
        )
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        sid = _session(client)
        start = time.time()
        resp = _rpc(
            client,
            "tools/call",
            {"name": "srv__danger", "arguments": {}},
            session_id=sid,
            rpc_id=7,
        )
        elapsed = time.time() - start
        body = resp.json()
        assert body["error"]["code"] == -32004
        assert body["error"]["data"]["status"] == "expired"
        assert elapsed < 5.0  # bounded; did not hang


def test_list_pending_approvals_via_mcp_primitive() -> None:
    cfg = _build(approval=ApprovalConfig(backend="memory", poll_interval_s=0.05))
    app = build_app(cfg)
    with TestClient(app) as client:
        sid = _session(client)

        def _call() -> None:
            _rpc(
                client,
                "tools/call",
                {"name": "srv__danger", "arguments": {}},
                session_id=sid,
                rpc_id=5,
            )

        caller = threading.Thread(target=_call)
        caller.start()
        try:
            seen = []
            for _ in range(200):
                resp = _rpc(
                    client,
                    "tools/call",
                    {"name": "gateway_list_pending_approvals", "arguments": {}},
                    session_id=sid,
                    rpc_id=6,
                )
                approvals = resp.json()["result"]["structuredContent"]["approvals"]
                if approvals:
                    seen = approvals
                    break
                time.sleep(0.02)
            assert seen, "MCP primitive never saw the parked approval"
            assert seen[0]["tool"] == "srv__danger"
            # Decide so the parked caller thread unblocks.
            client.post(
                "/admin/approvals/deny", json={"approval_id": seen[0]["approval_id"]}
            )
        finally:
            caller.join(timeout=10)


def test_operator_allow_list_refuses_non_operator() -> None:
    # With an allow-list configured, an authenticated-but-not-listed principal
    # (here the type="none" provider yields subject=None) is refused 403.
    cfg = _build(
        approval=ApprovalConfig(
            backend="memory", poll_interval_s=0.05, operator_subjects=["ops@trusted"]
        )
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        resp = client.get("/admin/approvals")
        assert resp.status_code == 403
        decide = client.post(
            "/admin/approvals/grant", json={"approval_id": "ap_does_not_exist"}
        )
        assert decide.status_code == 403


def test_grant_unknown_id_is_404() -> None:
    cfg = _build(approval=ApprovalConfig(backend="memory", poll_interval_s=0.05))
    app = build_app(cfg)
    with TestClient(app) as client:
        resp = client.post(
            "/admin/approvals/grant", json={"approval_id": "ap_nope"}
        )
        assert resp.status_code == 404


def test_admin_approvals_501_when_queue_disabled() -> None:
    # approval_mode != "queue": the decision endpoints report not-enabled.
    register_custom_adapter("p1-3-danger", _DangerAdapter)
    cfg = GatewayConfig(
        auth={"type": "none"},
        upstream_servers=[],
        policy=PolicyConfig(approval_mode="deny"),
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        resp = client.post("/admin/approvals/grant", json={"approval_id": "ap_x"})
        assert resp.status_code == 501


class _Sink:
    """Captures POSTed webhook deliveries (URL, body, headers)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        self.calls.append((url, content, headers))

        class _Resp:
            status_code = 200

        return _Resp()


def test_webhook_fires_with_verifiable_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    import concierge.policy.webhook as wh

    sink = _Sink()
    monkeypatch.setattr(wh.httpx, "AsyncClient", lambda *a, **k: sink)

    cfg = _build(
        approval=ApprovalConfig(
            backend="memory",
            poll_interval_s=0.05,
            webhooks=WebhookConfig(
                default_urls=["http://hook.local/approvals"],
                default_secret="webhook-secret",
            ),
        )
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        sid = _session(client)

        def _call() -> None:
            _rpc(
                client,
                "tools/call",
                {"name": "srv__danger", "arguments": {}},
                session_id=sid,
                rpc_id=11,
            )

        caller = threading.Thread(target=_call)
        caller.start()
        try:
            approval_id = None
            for _ in range(200):
                listed = client.get("/admin/approvals").json()["approvals"]
                if listed:
                    approval_id = listed[0]["approval_id"]
                    break
                time.sleep(0.02)
            assert approval_id is not None
            client.post("/admin/approvals/grant", json={"approval_id": approval_id})
        finally:
            caller.join(timeout=10)

        # Give the dispatch a beat to complete on the app loop.
        for _ in range(200):
            if sink.calls:
                break
            time.sleep(0.02)

    assert sink.calls, "no webhook delivered on grant"
    url, body, headers = sink.calls[0]
    assert url == "http://hook.local/approvals"
    assert verify_signature("webhook-secret", body, headers[SIGNATURE_HEADER])
    import json as _json

    payload = _json.loads(body)
    assert payload["event"] == "approval.granted"
    assert payload["approval"]["approval_id"] == approval_id
