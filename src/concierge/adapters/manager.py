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
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..util.audit import AuditLogger

from ..core.catalog import Catalog, normalize_catalog_entry
from ..core.types import (
    CatalogEntry,
    PrimitiveType,
    RiskLevel,
)
from ..errors import (
    GatewayError,
    UpstreamCircuitOpen,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from ..observability import trace_span
from ..util.log import get_logger
from .base import UpstreamAdapter
from .session_pool import SessionPool

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
        if datetime.now(UTC) >= until:
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
            self._open_until[server_id] = datetime.now(UTC) + self.cooldown

    def open_until(self, server_id: str) -> datetime | None:
        return self._open_until.get(server_id)


class AdapterManager:
    def __init__(
        self,
        catalog: Catalog,
        *,
        refresh_interval_s: float = 300.0,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_s: float = 30.0,
        max_upstream_sessions: int = 256,
        audit: AuditLogger | None = None,
    ) -> None:
        self.catalog = catalog
        self.refresh_interval_s = refresh_interval_s
        self.max_upstream_sessions = max_upstream_sessions
        self._audit = audit
        # The "control plane" adapter per server: used for catalog refresh,
        # list_changed watching, and (for shared upstreams) tool calls.
        self._adapters: dict[str, UpstreamAdapter] = {}
        self._meta: dict[str, dict[str, Any]] = {}   # server_id -> {risk_level, tags, ...}
        self._tasks: list[asyncio.Task[Any]] = []
        self._breaker = _CircuitBreaker(circuit_failure_threshold, circuit_cooldown_s)
        # Data plane delegates to SessionPool (LRU-bounded, race-safe, router-session teardown).
        self._pool = SessionPool(max_upstream_sessions=max_upstream_sessions, audit=audit)

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
        isolation: str = "shared",
        factory: Callable[[], UpstreamAdapter] | None = None,
        connect_max_retries: int = 3,
        connect_backoff_base_s: float = 0.5,
        connect_backoff_max_s: float = 10.0,
    ) -> None:
        self._adapters[adapter.server_id] = adapter
        self._meta[adapter.server_id] = {
            "default_risk": default_risk,
            "default_tags": default_tags or [],
            "default_categories": default_categories or [],
            "requires_auth": requires_auth,
            "requires_approval_for": set(requires_approval_for or []),
            "isolation": isolation,
            "factory": factory,
            "connect_max_retries": connect_max_retries,
            "connect_backoff_base_s": connect_backoff_base_s,
            "connect_backoff_max_s": connect_backoff_max_s,
        }

    def get(self, server_id: str) -> UpstreamAdapter | None:
        return self._adapters.get(server_id)

    def all(self) -> list[UpstreamAdapter]:
        return list(self._adapters.values())

    def health_snapshot(self) -> list[dict[str, Any]]:
        """Serializable upstream health for /readyz and /metrics."""
        rows: list[dict[str, Any]] = []
        for server_id, adapter in self._adapters.items():
            health = adapter.health().model_dump(mode="json")
            circuit_open_until = self._breaker.open_until(server_id)
            health["circuit_open"] = self._breaker.is_open(server_id)
            health["circuit_open_until"] = (
                circuit_open_until.isoformat() if circuit_open_until is not None else None
            )
            rows.append(health)
        return rows

    # ------------------------------------------------------------------
    async def start_all(self) -> None:
        for a in self._adapters.values():
            try:
                await a.connect()
                await a.initialize()
                await self.refresh_server(a.server_id)
            except GatewayError as e:
                _log.warning("adapter %s failed initial connect: %s", a.server_id, e)
            self._tasks.append(
                asyncio.create_task(self._watch_changes(a), name=f"watch-{a.server_id}")
            )
        self._tasks.append(asyncio.create_task(self._refresh_loop(), name="catalog-refresh"))

    async def stop_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await self._pool.drain()
        for a in self._adapters.values():
            try:
                await a.close()
            except Exception as e:  # noqa: BLE001
                _log.debug("ignored error closing adapter %s: %s", a.server_id, e)

    # ------------------------------------------------------------------
    @staticmethod
    async def _safe_close(adapter: UpstreamAdapter) -> None:
        try:
            await adapter.close()
        except Exception as e:  # noqa: BLE001
            _log.debug("ignored error closing adapter %s: %s", adapter.server_id, e)

    async def _connect_with_backoff(
        self,
        adapter: UpstreamAdapter,
        *,
        max_retries: int,
        base_s: float,
        max_s: float,
    ) -> None:
        """Connect + initialize an adapter, retrying transient failures with
        exponential backoff and jitter. Raises the last error if all attempts
        are exhausted."""
        attempt = 0
        while True:
            try:
                await adapter.connect()
                await adapter.initialize()
                return
            except GatewayError as e:
                attempt += 1
                if attempt > max_retries:
                    await self._safe_close(adapter)
                    raise
                backoff = min(max_s, base_s * (2 ** (attempt - 1)))
                # Full jitter in [0.5x, 1.0x] of the computed backoff.
                delay = backoff * (0.5 + secrets.randbelow(500_001) / 1_000_000)
                _log.warning(
                    "connect %s failed (attempt %d/%d), retrying in %.2fs: %s",
                    adapter.server_id, attempt, max_retries, delay, e,
                )
                await asyncio.sleep(delay)

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
            # Try to reconnect lazily; if it still fails, mark down
            # (keep entries, flag callable=false)
            # and leave catalog as-is (no remove). Resilience per P1-5/T3.
            try:
                await adapter.connect()
                await adapter.initialize()
            except GatewayError:
                await self.catalog.set_callable_for_server(server_id, False)
                return 0

        entries: list[CatalogEntry] = []
        meta = self._meta.get(server_id, {})

        try:
            tools = await adapter.list_tools()
            for t in tools:
                entry = normalize_catalog_entry(
                    server_id, adapter.transport, PrimitiveType.TOOL, t, meta
                )
                if entry is not None:
                    entries.append(entry)
        except GatewayError as e:
            _log.warning("list_tools(%s) failed: %s", server_id, e)

        try:
            resources = await adapter.list_resources()
            for r in resources:
                entry = normalize_catalog_entry(
                    server_id, adapter.transport, PrimitiveType.RESOURCE, r, meta
                )
                if entry is not None:
                    entries.append(entry)
        except GatewayError:
            pass

        try:
            prompts = await adapter.list_prompts()
            for p in prompts:
                entry = normalize_catalog_entry(
                    server_id, adapter.transport, PrimitiveType.PROMPT, p, meta
                )
                if entry is not None:
                    entries.append(entry)
        except GatewayError:
            pass

        await self.catalog.replace_server(server_id, entries)
        return len(entries)

    # ------------------------------------------------------------------
    # Session registry — resolve the right upstream for a data-plane call.
    # Per-session pool ownership moved to SessionPool (task #4, Slice 2).
    # AdapterManager keeps the control-plane adapters + backoff for them.
    # ------------------------------------------------------------------
    async def _resolve_session_adapter(
        self, server_id: str, router_session_id: str | None
    ) -> UpstreamAdapter:
        """Return the upstream adapter for a call.

        Shared upstreams: return the registered control-plane adapter.
        Per-session upstreams: delegate to SessionPool (LRU, lazy connect with
        races, router-session teardown).
        """
        meta = self._meta.get(server_id)
        if meta is None:
            raise UpstreamUnavailable(f"no adapter for {server_id}")

        factory = meta.get("factory")
        if meta.get("isolation") != "per_session" or router_session_id is None or factory is None:
            adapter = self._adapters.get(server_id)
            if adapter is None:
                raise UpstreamUnavailable(f"no adapter for {server_id}")
            return adapter

        return await self._pool.resolve(
            server_id=server_id,
            router_session_id=router_session_id,
            factory=factory,
            connect_max_retries=meta["connect_max_retries"],
            connect_backoff_base_s=meta["connect_backoff_base_s"],
            connect_backoff_max_s=meta["connect_backoff_max_s"],
        )

    # Thin delegation surface so callers/tests keep using the same names.
    async def evict_router_session(self, router_session_id: str) -> int:
        """Tear down all pooled upstream adapters for a given router session."""
        return await self._pool.evict_router_session(router_session_id)

    def pool_size(self) -> int:
        return self._pool.size()

    async def _enforce_pool_cap(self) -> None:
        """Force an LRU cap enforcement (mostly for tests). Pool also enforces on insert."""
        await self._pool._enforce_pool_cap()

    # ------------------------------------------------------------------
    # Temporary compatibility shims (Slice 2 extraction only).
    # Existing pool-oriented tests reach into manager's previous internal
    # pool structures. Route them to SessionPool's equivalents so the
    # white-box tests keep passing verbatim during the transition.
    # Once the pool-specific tests are ported to drive SessionPool
    # directly, these two properties can be removed.
    # ------------------------------------------------------------------
    @property
    def _pool_lock(self) -> asyncio.Lock:
        return self._pool._pool_lock  # type: ignore[attr-defined]

    @property
    def _pool_pending(self) -> dict[tuple[str, str], asyncio.Task[UpstreamAdapter]]:
        return self._pool._pool_pending  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    async def call_tool(
        self,
        server_id: str,
        upstream_name: str,
        arguments: dict[str, Any],
        *,
        router_session_id: str | None = None,
    ) -> dict[str, Any]:
        if self._breaker.is_open(server_id):
            raise UpstreamCircuitOpen(server_id)
        # Resilience (T3/P1-5): if catalog has marked this server non-callable (upstream down),
        # return clear error immediately (no hang, no attempt on dead adapter).
        entry = await self.catalog.get(
            f"{server_id}__{upstream_name}"
        )  # best-effort; real callers use canonical via publishing
        if entry is not None and not getattr(entry, "callable", True):
            raise UpstreamUnavailable(f"upstream {server_id} is down (callable=false)")
        try:
            adapter = await self._resolve_session_adapter(server_id, router_session_id)
            with trace_span(
                "concierge.upstream.call_tool",
                server_id=server_id,
                upstream_name=upstream_name,
            ):
                result = await adapter.call_tool(upstream_name, arguments)
            self._breaker.record_success(server_id)
            return result
        except (UpstreamUnavailable, UpstreamTimeout):
            self._breaker.record_failure(server_id)
            raise
        except GatewayError:
            # protocol errors don't open the breaker
            raise

    async def read_resource(
        self, server_id: str, uri: str, *, router_session_id: str | None = None
    ) -> dict[str, Any]:
        adapter = await self._resolve_session_adapter(server_id, router_session_id)
        return await adapter.read_resource(uri)

    async def get_prompt(
        self,
        server_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        router_session_id: str | None = None,
    ) -> dict[str, Any]:
        adapter = await self._resolve_session_adapter(server_id, router_session_id)
        return await adapter.get_prompt(name, arguments)
