"""P1-6 observability endpoints, metrics, tracing hooks, and audit sink fan-out."""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from concierge.observability import HttpAuditSink, MetricAuditSink, MetricRegistry
from concierge.util.audit import AuditLogger

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "observability-test", "version": "0.0.1"},
    },
}


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))


def test_health_ready_and_metrics_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"ok": True, "upstreams": []}

    init = client.post("/mcp", json=_INIT)
    assert init.status_code == 200
    client.app.state.bus.tools_list_changed("session-with-pending-notification")

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    body = metrics.text
    assert "concierge_http_requests_total" in body
    assert "concierge_http_request_duration_seconds_count" in body
    assert "concierge_sessions_active 1" in body
    assert (
        'concierge_notification_queue_depth{session_id="session-with-pending-notification"} 1'
        in body
    )


def test_metric_audit_sink_exports_tool_call_metrics() -> None:
    metrics = MetricRegistry()
    audit = AuditLogger(sinks=[MetricAuditSink(metrics)])

    audit.tool_called("s1", "demo__echo", "demo", True, 12.5)

    body = metrics.render(upstream_health=[], active_sessions=0, queue_depths={})
    assert 'concierge_tool_calls_total{ok="true",server_id="demo"} 1' in body
    assert 'concierge_tool_call_duration_seconds_count{ok="true",server_id="demo"} 1' in body
    assert 'concierge_tool_call_duration_seconds_sum{ok="true",server_id="demo"} 0.0125' in body


def test_audit_logger_fans_out_redacted_events_to_pluggable_sink() -> None:
    sink = RecordingSink()
    audit = AuditLogger(sinks=[sink])

    audit.emit(
        "auth.failure",
        api_token="raw-secret-token",
        headers={"authorization": "Bearer raw-secret-token"},
    )

    assert sink.events
    event, payload = sink.events[0]
    assert event == "auth.failure"
    assert payload["api_token"] == "***"
    assert payload["headers"]["authorization"] == "***"


def test_metric_audit_sink_tool_call_without_latency() -> None:
    metrics = MetricRegistry()
    sink = MetricAuditSink(metrics)

    sink.emit("tool.call", {"server_id": "s2", "ok": False})

    body = metrics.render(upstream_health=[], active_sessions=0, queue_depths={})
    assert 'concierge_tool_calls_total{ok="false",server_id="s2"} 1' in body
    assert "concierge_tool_call_duration_seconds_count" not in body
    assert "concierge_tool_call_duration_seconds_sum" not in body


def test_http_audit_sink_posts_and_closes() -> None:
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    sink = HttpAuditSink("http://collector.test/audit", timeout_s=1.0)
    sink._client = httpx.Client(transport=httpx.MockTransport(_handler))

    sink.emit("tool.call", {"server_id": "s1", "ok": True})

    assert len(captured) == 1
    assert captured[0].url == "http://collector.test/audit"
    assert captured[0].method == "POST"
    assert json.loads(captured[0].content) == {
        "event": "tool.call",
        "server_id": "s1",
        "ok": True,
    }

    sink.close()
    assert sink._client.is_closed


def test_metric_registry_render_upstream_health_and_queue_depths() -> None:
    metrics = MetricRegistry()
    body = metrics.render(
        upstream_health=[
            {
                "server_id": "srv1",
                "transport": "stdio",
                "connected": True,
                "circuit_open": False,
            },
            {
                "server_id": "srv2",
                "transport": "sse",
                "connected": False,
                "circuit_open": True,
            },
        ],
        active_sessions=3,
        queue_depths={"sess-a": 2, "sess-b": 0},
    )
    assert "concierge_sessions_active 3" in body
    assert 'concierge_notification_queue_depth{session_id="sess-a"} 2' in body
    assert 'concierge_notification_queue_depth{session_id="sess-b"} 0' in body
    assert 'concierge_upstream_connected{server_id="srv1",transport="stdio"} 1' in body
    assert 'concierge_upstream_connected{server_id="srv2",transport="sse"} 0' in body
    assert 'concierge_upstream_circuit_open{server_id="srv2",transport="sse"} 1' in body
    assert 'concierge_upstream_circuit_open{server_id="srv1",transport="stdio"} 0' in body
