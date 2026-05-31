"""Observability helpers for metrics, tracing, health, and audit sinks."""
from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

try:  # Optional: operators can install OpenTelemetry and configure exporters via env.
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover - depends on optional runtime package
    _otel_trace = None  # type: ignore[assignment]


def _labels(values: dict[str, str]) -> str:
    if not values:
        return ""
    parts = []
    for key, value in sorted(values.items()):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{key}="{escaped}"')
    return "{" + ",".join(parts) + "}"


def _sample_name(name: str, suffix: str) -> str:
    return f"{name}_{suffix}"


@contextmanager
def trace_span(name: str, **attributes: Any):
    """Start an OpenTelemetry span when the optional API is installed.

    The gateway does not own exporter configuration. Production deployments can
    install/configure the OTel SDK or auto-instrumentation; without it this is a
    no-op, keeping the default dependency footprint small.
    """
    if _otel_trace is None:
        with nullcontext() as span:
            yield span
        return

    tracer = _otel_trace.get_tracer("concierge")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


@dataclass(frozen=True)
class Histogram:
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> Histogram:
        return Histogram(count=self.count + 1, total=self.total + value)


class MetricRegistry:
    """Small Prometheus text-format registry with app-specific metrics."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], Histogram] = {}

    @staticmethod
    def _key(
        name: str,
        labels: dict[str, str] | None = None,
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def inc(
        self,
        name: str,
        *,
        amount: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._counters[self._key(name, labels)] += amount

    def observe(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        self._histograms[key] = self._histograms.get(key, Histogram()).observe(value)

    def record_http_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_s: float,
    ) -> None:
        labels = {"method": method, "path": path, "status": str(status_code)}
        self.inc("concierge_http_requests_total", labels=labels)
        self.observe("concierge_http_request_duration_seconds", duration_s, labels=labels)

    def record_tool_call(
        self,
        *,
        server_id: str,
        ok: bool,
        latency_ms: float | int | None = None,
    ) -> None:
        labels = {"server_id": server_id, "ok": str(ok).lower()}
        self.inc("concierge_tool_calls_total", labels=labels)
        if latency_ms is not None:
            self.observe(
                "concierge_tool_call_duration_seconds",
                float(latency_ms) / 1000.0,
                labels=labels,
            )

    def render(
        self,
        *,
        upstream_health: list[dict[str, Any]],
        active_sessions: int,
        queue_depths: dict[str, int],
    ) -> str:
        lines = [
            "# HELP concierge_http_requests_total HTTP requests handled by Concierge.",
            "# TYPE concierge_http_requests_total counter",
        ]
        for (name, label_items), value in sorted(self._counters.items()):
            if name == "concierge_http_requests_total":
                lines.append(f"{name}{_labels(dict(label_items))} {value:g}")

        lines.extend([
            "# HELP concierge_http_request_duration_seconds HTTP request duration.",
            "# TYPE concierge_http_request_duration_seconds summary",
        ])
        for (name, label_items), hist in sorted(self._histograms.items()):
            if name == "concierge_http_request_duration_seconds":
                label_dict = dict(label_items)
                lines.append(f"{_sample_name(name, 'count')}{_labels(label_dict)} {hist.count}")
                lines.append(f"{_sample_name(name, 'sum')}{_labels(label_dict)} {hist.total:g}")

        lines.extend([
            "# HELP concierge_tool_calls_total Upstream tool calls routed by Concierge.",
            "# TYPE concierge_tool_calls_total counter",
        ])
        for (name, label_items), value in sorted(self._counters.items()):
            if name == "concierge_tool_calls_total":
                lines.append(f"{name}{_labels(dict(label_items))} {value:g}")

        lines.extend([
            "# HELP concierge_tool_call_duration_seconds Upstream tool call duration.",
            "# TYPE concierge_tool_call_duration_seconds summary",
        ])
        for (name, label_items), hist in sorted(self._histograms.items()):
            if name == "concierge_tool_call_duration_seconds":
                label_dict = dict(label_items)
                lines.append(f"{_sample_name(name, 'count')}{_labels(label_dict)} {hist.count}")
                lines.append(f"{_sample_name(name, 'sum')}{_labels(label_dict)} {hist.total:g}")

        lines.extend([
            "# HELP concierge_sessions_active Active downstream MCP sessions.",
            "# TYPE concierge_sessions_active gauge",
            f"concierge_sessions_active {active_sessions}",
            "# HELP concierge_notification_queue_depth "
            "Pending downstream notifications per session.",
            "# TYPE concierge_notification_queue_depth gauge",
        ])
        for session_id, depth in sorted(queue_depths.items()):
            queue_labels = _labels({"session_id": session_id})
            lines.append(f"concierge_notification_queue_depth{queue_labels} {depth}")

        lines.extend([
            "# HELP concierge_upstream_connected Whether an upstream adapter reports connected.",
            "# TYPE concierge_upstream_connected gauge",
            "# HELP concierge_upstream_circuit_open Whether the upstream circuit breaker is open.",
            "# TYPE concierge_upstream_circuit_open gauge",
        ])
        for health in sorted(upstream_health, key=lambda h: h["server_id"]):
            upstream_labels = {
                "server_id": str(health["server_id"]),
                "transport": str(health["transport"]),
            }
            connected = 1 if health["connected"] else 0
            circuit_open = 1 if health["circuit_open"] else 0
            lines.append(f"concierge_upstream_connected{_labels(upstream_labels)} {connected}")
            lines.append(
                f"concierge_upstream_circuit_open{_labels(upstream_labels)} {circuit_open}"
            )
        return "\n".join(lines) + "\n"


class MetricAuditSink:
    """Audit sink that turns redacted audit events into Prometheus counters."""

    def __init__(self, metrics: MetricRegistry) -> None:
        self.metrics = metrics

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event == "tool.call":
            self.metrics.record_tool_call(
                server_id=str(payload.get("server_id", "unknown")),
                ok=bool(payload.get("ok")),
                latency_ms=payload.get("latency_ms"),
            )


class HttpAuditSink:
    """Synchronous HTTP JSON audit sink for external collectors/webhooks."""

    def __init__(self, url: str, *, timeout_s: float = 2.0) -> None:
        import httpx

        self._client = httpx.Client(timeout=timeout_s)
        self._url = url

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        body = {"event": event, **payload}
        self._client.post(self._url, json=body)

    def close(self) -> None:
        self._client.close()


def build_observability_router(
    *,
    metrics: MetricRegistry,
    adapters: Any,
    sessions: Any,
    bus: Any,
    metrics_path: str = "/metrics",
    health_path: str = "/healthz",
    ready_path: str = "/readyz",
    drain: Any = None,
) -> APIRouter:
    router = APIRouter()

    @router.get(health_path, include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @router.get(ready_path, include_in_schema=False)
    async def readyz() -> JSONResponse:
        # During a graceful drain (SIGTERM, P1-7) report not-ready so the load
        # balancer / k8s endpoints controller stops sending new connections here
        # while in-flight calls finish on the old pod.
        if drain is not None and drain.draining:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "draining": True},
            )
        upstream = adapters.health_snapshot()
        ok = all(h["connected"] for h in upstream)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"ok": ok, "upstreams": upstream},
        )

    @router.get(metrics_path, include_in_schema=False)
    async def prometheus_metrics() -> PlainTextResponse:
        body = metrics.render(
            upstream_health=adapters.health_snapshot(),
            active_sessions=len(await sessions.all()),
            queue_depths=bus.queue_depths(),
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    return router


async def record_request_metrics(
    request: Request,
    call_next: Any,
    metrics: MetricRegistry,
):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        metrics.record_http_request(
            request.method,
            request.url.path,
            status_code,
            time.perf_counter() - started,
        )
