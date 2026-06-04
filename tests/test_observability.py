"""P1-6 observability endpoints, metrics, tracing hooks, and audit sink fan-out."""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from concierge.observability import MetricAuditSink, MetricRegistry
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
