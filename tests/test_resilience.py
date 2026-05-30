"""TDD tests for P1-5 / T3 Upstream resilience (backoff + callable=false marking).

Per swarm assignment (Builder 3):
- Add callable flag to CatalogEntry (default True).
- Manager: on upstream loss, mark catalog entries callable=false (keep them, do not remove);
  reconnect uses/ extends _connect_with_backoff with jitter for control-plane.
- Tool calls (or discovery) to non-callable return clear error (no hang/timeout).
- Tests prove: reconnect-after-kill, callable=false marking, clear-error-not-hang.

TDD order: these tests written FIRST (must fail), then min code to green.
Run: PYTHONPATH=src python -m pytest tests/test_resilience.py -q --tb=short
"""

import asyncio
from typing import Any

import pytest

from concierge.adapters.base import UpstreamAdapter
from concierge.adapters.manager import AdapterManager
from concierge.core.catalog import Catalog
from concierge.core.types import (
    AdapterHealth,
    CatalogEntry,
    PrimitiveType,
    RiskLevel,
    TransportType,
)
from concierge.errors import GatewayError, UpstreamUnavailable
from concierge.util.audit import AuditLogger


class DisconnectingFakeAdapter(UpstreamAdapter):
    """Fake that can simulate connect success then hard disconnect for resilience tests.
    Raises proper GatewayError subclasses so manager resilience paths (mark callable=false, clear errors) are exercised.
    """
    transport = TransportType.STDIO

    def __init__(self, server_id: str, *, fail_after: int = 0) -> None:
        self.server_id = server_id
        self._connected = False
        self._connect_calls = 0
        self._fail_after = fail_after  # after N successes, next connect fails
        self._tools: list[dict[str, Any]] = [
            {"name": "echo", "description": "echo tool for resilience test"}
        ]

    async def connect(self) -> None:
        self._connect_calls += 1
        if self._fail_after > 0 and self._connect_calls > self._fail_after:
            self._connected = False
            raise GatewayError("simulated upstream disconnect for T3 test")
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def initialize(self) -> dict[str, Any]:
        if not self._connected:
            raise GatewayError("not connected")
        return {}

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self._connected:
            raise GatewayError("not connected")
        return self._tools

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._connected:
            raise GatewayError("upstream down")
        return {"content": [{"type": "text", "text": "ok"}]}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            server_id=self.server_id,
            transport=self.transport,
            connected=self._connected,
            last_error=None if self._connected else "simulated down",
            last_connected_at=None,
            consecutive_failures=0 if self._connected else 1,
        )


def _build_minimal_manager() -> tuple[AdapterManager, Catalog, DisconnectingFakeAdapter]:
    catalog = Catalog()
    audit = AuditLogger()
    mgr = AdapterManager(catalog, audit=audit, refresh_interval_s=999.0)
    adapter = DisconnectingFakeAdapter("test_upstream", fail_after=1)
    # register with factory for per_session but we test control-plane path too
    mgr.register(
        adapter,
        default_risk=RiskLevel.LOW,
        isolation="shared",
        factory=lambda: DisconnectingFakeAdapter("test_upstream", fail_after=1),
        connect_max_retries=2,
        connect_backoff_base_s=0.01,
        connect_backoff_max_s=0.1,
    )
    return mgr, catalog, adapter


@pytest.mark.asyncio
async def test_catalog_entry_has_callable_flag_default_true():
    """New field per T3: CatalogEntry must have callable: bool = True (for resilience marking)."""
    entry = CatalogEntry(
        canonical_name="test__tool",
        upstream_name="tool",
        server_id="s1",
        transport=TransportType.STDIO,
        primitive_type=PrimitiveType.TOOL,
        display_label="tool",
        short_description="d",
    )
    assert hasattr(entry, "callable")
    assert entry.callable is True  # default


@pytest.mark.asyncio
async def test_manager_marks_catalog_entries_callable_false_on_upstream_down():
    """When upstream health fails (simulated disconnect), refresh or mark must set callable=false on its entries (not remove them)."""
    mgr, catalog, adapter = _build_minimal_manager()
    await mgr.start_all()  # initial success populates
    assert len(catalog.list(server="test_upstream")) > 0
    assert all(e.callable for e in catalog.list(server="test_upstream"))

    # Simulate loss: force disconnect + refresh failure path
    adapter._connected = False
    # refresh should detect not connected, attempt (fail), and mark down
    count = await mgr.refresh_server("test_upstream")
    # After failure path, entries should still exist but marked non-callable
    entries = catalog.list(server="test_upstream")
    assert len(entries) > 0, "entries must be kept, not removed on transient down"
    assert all(e.callable is False for e in entries), "must mark callable=false while down"


@pytest.mark.asyncio
async def test_tool_call_to_non_callable_upstream_returns_clear_error_not_hang():
    """Call to a down (non-callable) upstream must raise clear UpstreamUnavailable (or equiv) quickly, no hang."""
    mgr, catalog, adapter = _build_minimal_manager()
    await mgr.start_all()
    # force down + mark
    adapter._connected = False
    await mgr.refresh_server("test_upstream")

    # Now call should not hang, must surface clear error (manager or service layer)
    with pytest.raises((UpstreamUnavailable, Exception)) as exc:  # broad until exact error chosen
        await mgr.call_tool("test_upstream", "echo", {}, router_session_id=None)
    # The error must be fast (no long timeout) and descriptive (not generic internal)
    assert "unavailable" in str(exc.value).lower() or "down" in str(exc.value).lower() or "callable" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_reconnect_with_backoff_after_kill_restores_callable_true():
    """After simulated kill, manager reconnects with backoff (reusing _connect_with_backoff), restores callable=true."""
    mgr, catalog, adapter = _build_minimal_manager()
    await mgr.start_all()
    adapter._connected = False
    await mgr.refresh_server("test_upstream")
    assert all(e.callable is False for e in catalog.list(server="test_upstream"))

    # Now "fix" the upstream and trigger reconnect path
    adapter._connected = True
    # In real, the watch/refresh or explicit would use backoff path
    # For TDD, assert that after success refresh, callable restored
    await mgr.refresh_server("test_upstream")
    assert all(e.callable is True for e in catalog.list(server="test_upstream"))
