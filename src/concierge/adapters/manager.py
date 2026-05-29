"""
AdapterManager.

Owns the lifecycle of every UpstreamAdapter, refreshes the catalog from each
upstream, applies circuit-breaker logic around tool calls, and bridges adapter
`list_changed` events into a catalog refresh.

The manager itself is *not* aware of sessions — it deals only with the upstream
side of the world. Per-session publishing happens elsewhere.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.catalog import Catalog
from ..core.types import (
    ArgumentSummary,
    CatalogEntry,
    PrimitiveType,
    RiskLevel,
    TransportType,
)
from ..errors import (
    GatewayError,
    UpstreamCircuitOpen,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from ..util.log import get_logger
from ..util.sanitize import (
    make_canonical_name,
    sanitize_description,
    sanitize_label,
    schema_hash,
    summarize_arguments,
    validate_input_schema,
)
from .base import UpstreamAdapter

_log = get_logger("concierge.adapter.manager")


class _CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = timedelta(seconds=cooldown_s)
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, datetime] = {}

    def is_open(self, server_id: str) -> bool:
        until = self._open_until.get(server_id)
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            self._open_until.pop(server_id, None)
            self._failures[server_id] = 0
            return False
        return True

    def record_success(self, server_id: str) -> None:
        self._failures[server_id] = 0
        self._open_until.pop(server_id, None)

    def record_failure(self, server_id: str) -> None:
        n = self._failures.get(server_id, 0) + 1
        self._failures[server_id] = n
        if n >= self.failure_threshold:
            self._open_until[server_id] = datetime.now(timezone.utc) + self.cooldown


class AdapterManager:
    def __init__(
        self,
        catalog: Catalog,
        *,
        refresh_interval_s: float = 300.0,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_s: float = 30.0,
    ) -> None:
        self.catalog = catalog
        self.refresh_interval_s = refresh_interval_s
        self._adapters: dict[str, UpstreamAdapter] = {}
        self._meta: dict[str, dict[str, Any]] = {}   # server_id -> {risk_level, tags, ...}
        self._tasks: list[asyncio.Task[Any]] = []
        self._breaker = _CircuitBreaker(circuit_failure_threshold, circuit_cooldown_s)

    # ------------------------------------------------------------------
    def register(
        self,
        adapter: UpstreamAdapter,
        *,
        default_risk: RiskLevel = RiskLevel.MEDIUM,
        default_tags: list[str] | None = None,
        default_categories: list[str] | None = None,
        requires_auth: bool = False,
        requires_approval_for: list[str] | None = None,
    ) -> None:
        self._adapters[adapter.server_id] = adapter
        self._meta[adapter.server_id] = {
            "default_risk": default_risk,
            "default_tags": default_tags or [],
            "default_categories": default_categories or [],
            "requires_auth": requires_auth,
            "requires_approval_for": set(requires_approval_for or []),
        }

    def get(self, server_id: str) -> UpstreamAdapter | None:
        return self._adapters.get(server_id)

    def all(self) -> list[UpstreamAdapter]:
        return list(self._adapters.values())

    # ------------------------------------------------------------------
    async def start_all(self) -> None:
        for a in self._adapters.values():
            try:
                await a.connect()
                await a.initialize()
                await self.refresh_server(a.server_id)
            except GatewayError as e:
                _log.warning("adapter %s failed initial connect: %s", a.server_id, e)
            self._tasks.append(asyncio.create_task(self._watch_changes(a), name=f"watch-{a.server_id}"))
        self._tasks.append(asyncio.create_task(self._refresh_loop(), name="catalog-refresh"))

    async def stop_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        for a in self._adapters.values():
            try:
                await a.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    async def _watch_changes(self, adapter: UpstreamAdapter) -> None:
        try:
            async for kind in adapter.list_changed_events():
                _log.info("upstream %s notified list change (%s)", adapter.server_id, kind)
                try:
                    await self.refresh_server(adapter.server_id)
                except Exception as e:  # noqa: BLE001
                    _log.warning("refresh after list_changed failed: %s", e)
        except asyncio.CancelledError:
            return

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.refresh_interval_s)
                for sid in list(self._adapters.keys()):
                    try:
                        await self.refresh_server(sid)
                    except Exception as e:  # noqa: BLE001
                        _log.warning("periodic refresh %s failed: %s", sid, e)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    async def refresh_server(self, server_id: str) -> int:
        adapter = self._adapters.get(server_id)
        if adapter is None:
            return 0
        if not adapter.health().connected:
            # Try to reconnect lazily; if it still fails, leave catalog as-is.
            try:
                await adapter.connect()
                await adapter.initialize()
            except GatewayError:
                return 0

        entries: list[CatalogEntry] = []
        meta = self._meta.get(server_id, {})

        try:
            tools = await adapter.list_tools()
            for t in tools:
                entry = self._normalize(server_id, adapter.transport, PrimitiveType.TOOL, t, meta)
                if entry is not None:
                    entries.append(entry)
        except GatewayError as e:
            _log.warning("list_tools(%s) failed: %s", server_id, e)

        try:
            resources = await adapter.list_resources()
            for r in resources:
                entry = self._normalize(server_id, adapter.transport, PrimitiveType.RESOURCE, r, meta)
                if entry is not None:
                    entries.append(entry)
        except GatewayError:
            pass

        try:
            prompts = await adapter.list_prompts()
            for p in prompts:
                entry = self._normalize(server_id, adapter.transport, PrimitiveType.PROMPT, p, meta)
                if entry is not None:
                    entries.append(entry)
        except GatewayError:
            pass

        self.catalog.replace_server(server_id, entries)
        return len(entries)

    def _normalize(
        self,
        server_id: str,
        transport: TransportType,
        ptype: PrimitiveType,
        raw: dict[str, Any],
        meta: dict[str, Any],
    ) -> CatalogEntry | None:
        try:
            upstream_name = raw.get("name") or raw.get("uri") or ""
            canonical = make_canonical_name(server_id, upstream_name)
        except ValueError as e:
            _log.warning("dropping malformed primitive from %s: %s", server_id, e)
            return None

        title = sanitize_label(raw.get("title") or raw.get("name") or upstream_name)
        # IMPORTANT: do not use upstream description verbatim — sanitize.
        desc = sanitize_description(raw.get("description"))
        if not desc:
            desc = f"{ptype.value} provided by upstream {server_id}"

        input_schema = None
        arg_summary: list[ArgumentSummary] = []
        if ptype == PrimitiveType.TOOL:
            input_schema = validate_input_schema(raw.get("inputSchema") or raw.get("input_schema"))
            arg_summary = [ArgumentSummary(**a) for a in summarize_arguments(input_schema)]

        risk = meta.get("default_risk", RiskLevel.MEDIUM)
        requires_approval = upstream_name in meta.get("requires_approval_for", set())

        return CatalogEntry(
            canonical_name=canonical,
            upstream_name=upstream_name,
            server_id=server_id,
            transport=transport,
            primitive_type=ptype,
            display_label=title,
            title=title,
            short_description=desc,
            usage_guidance=None,
            input_schema=input_schema,
            argument_summary=arg_summary,
            tags=list(meta.get("default_tags", [])),
            categories=list(meta.get("default_categories", [])),
            risk_level=risk,
            requires_approval=requires_approval,
            requires_auth=meta.get("requires_auth", False),
            schema_hash=schema_hash(input_schema) if input_schema else None,
        )

    # ------------------------------------------------------------------
    async def call_tool(self, server_id: str, upstream_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        adapter = self._adapters.get(server_id)
        if adapter is None:
            raise UpstreamUnavailable(f"no adapter for {server_id}")
        if self._breaker.is_open(server_id):
            raise UpstreamCircuitOpen(server_id)
        try:
            result = await adapter.call_tool(upstream_name, arguments)
            self._breaker.record_success(server_id)
            return result
        except (UpstreamUnavailable, UpstreamTimeout):
            self._breaker.record_failure(server_id)
            raise
        except GatewayError:
            # protocol errors don't open the breaker
            raise

    async def read_resource(self, server_id: str, uri: str) -> dict[str, Any]:
        adapter = self._adapters.get(server_id)
        if adapter is None:
            raise UpstreamUnavailable(server_id)
        return await adapter.read_resource(uri)

    async def get_prompt(self, server_id: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        adapter = self._adapters.get(server_id)
        if adapter is None:
            raise UpstreamUnavailable(server_id)
        return await adapter.get_prompt(name, arguments)
