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
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..util.audit import AuditLogger

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
from ..observability import trace_span
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


@dataclass
class _PooledSession:
    """One lazily-created, isolated upstream session for a router session."""
    adapter: UpstreamAdapter
    last_used: datetime


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
        # "Data plane" pool for per_session upstreams, LRU-ordered.
        self._pool: OrderedDict[tuple[str, str], _PooledSession] = OrderedDict()
        self._pool_pending: dict[tuple[str, str], asyncio.Task[UpstreamAdapter]] = {}
        self._pool_lock = asyncio.Lock()

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
        async with self._pool_lock:
            pending = list(self._pool_pending.values())
            self._pool_pending.clear()
            pooled = [entry.adapter for entry in self._pool.values()]
            self._pool.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for adapter in pooled:
            await self._safe_close(adapter)
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
                entry = self._normalize(server_id, adapter.transport, PrimitiveType.TOOL, t, meta)
                if entry is not None:
                    entries.append(entry)
        except GatewayError as e:
            _log.warning("list_tools(%s) failed: %s", server_id, e)

        try:
            resources = await adapter.list_resources()
            for r in resources:
                entry = self._normalize(
                    server_id, adapter.transport, PrimitiveType.RESOURCE, r, meta
                )
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

        await self.catalog.replace_server(server_id, entries)
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
    # Session registry — resolve the right upstream session for a call.
    # ------------------------------------------------------------------
    async def _resolve_session_adapter(
        self, server_id: str, router_session_id: str | None
    ) -> UpstreamAdapter:
        """Return the upstream adapter to use for a data-plane call.

        For ``shared`` upstreams (or when no router session id is given) this is
        the control-plane adapter. For ``per_session`` upstreams it is a pooled,
        lazily-created, per-router-session adapter — reused across calls in the
        same router session and LRU-evicted when the pool is full.
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

        key = (server_id, router_session_id)
        async with self._pool_lock:
            existing = self._pool.get(key)
            if existing is not None:
                existing.last_used = datetime.now(UTC)
                self._pool.move_to_end(key)
                return existing.adapter

            pending = self._pool_pending.get(key)
            if pending is None:
                adapter = factory()
                pending = asyncio.create_task(
                    self._create_pooled_session_adapter(key, adapter, meta),
                    name=f"connect-{server_id}-{router_session_id}",
                )
                self._pool_pending[key] = pending
                def forget_pending(
                    task: asyncio.Task[UpstreamAdapter],
                    pending_key: tuple[str, str] = key,
                ) -> None:
                    asyncio.create_task(self._forget_pending_session(pending_key, task))

                pending.add_done_callback(forget_pending)

        return await asyncio.shield(pending)

    async def _forget_pending_session(
        self,
        key: tuple[str, str],
        task: asyncio.Task[UpstreamAdapter],
    ) -> None:
        async with self._pool_lock:
            if self._pool_pending.get(key) is task:
                self._pool_pending.pop(key, None)

    async def _create_pooled_session_adapter(
        self,
        key: tuple[str, str],
        adapter: UpstreamAdapter,
        meta: dict[str, Any],
    ) -> UpstreamAdapter:
        """Connect a pooled adapter without holding the global pool lock."""
        server_id, router_session_id = key
        adapter_transferred = False
        adapter_to_close: UpstreamAdapter | None = None
        victims: list[tuple[str, str, UpstreamAdapter]] = []
        try:
            await self._connect_with_backoff(
                adapter,
                max_retries=meta["connect_max_retries"],
                base_s=meta["connect_backoff_base_s"],
                max_s=meta["connect_backoff_max_s"],
            )

            result = adapter
            async with self._pool_lock:
                existing = self._pool.get(key)
                if existing is not None:
                    existing.last_used = datetime.now(UTC)
                    self._pool.move_to_end(key)
                    adapter_to_close = adapter
                    result = existing.adapter
                else:
                    self._pool[key] = _PooledSession(adapter=adapter, last_used=datetime.now(UTC))
                    adapter_transferred = True
                    self._pool.move_to_end(key)
                    if self._audit is not None:
                        self._audit.upstream_session("created", server_id, router_session_id)
                    victims = self._pop_lru_over_cap_locked()

            if adapter_to_close is not None:
                await self._safe_close(adapter_to_close)
                adapter_to_close = None
            await self._close_lru_victims(victims)
            victims = []
            return result
        except asyncio.CancelledError:
            if adapter_to_close is not None:
                await self._safe_close(adapter_to_close)
            if not adapter_transferred:
                await self._safe_close(adapter)
            await self._close_lru_victims(victims)
            raise
        except Exception:
            _log.exception(
                "pooled adapter connect for %s/%s failed",
                server_id,
                router_session_id,
            )
            if adapter_to_close is not None:
                await self._safe_close(adapter_to_close)
            if not adapter_transferred:
                await self._safe_close(adapter)
            await self._close_lru_victims(victims)
            raise

    def _pop_lru_over_cap_locked(self) -> list[tuple[str, str, UpstreamAdapter]]:
        """Remove LRU pooled sessions past the cap. Caller must hold the pool lock."""
        victims: list[tuple[str, str, UpstreamAdapter]] = []
        while len(self._pool) > self.max_upstream_sessions:
            (sid, rsid), victim = self._pool.popitem(last=False)
            victims.append((sid, rsid, victim.adapter))
        return victims

    async def _close_lru_victims(self, victims: list[tuple[str, str, UpstreamAdapter]]) -> None:
        for sid, rsid, adapter in victims:
            await self._safe_close(adapter)
            _log.info("evicted LRU upstream session %s/%s", sid, rsid)
            if self._audit is not None:
                self._audit.upstream_session("lru_evicted", sid, rsid)

    async def _enforce_pool_cap(self) -> None:
        """Evict least-recently-used pooled sessions past the cap."""
        async with self._pool_lock:
            victims = self._pop_lru_over_cap_locked()
        await self._close_lru_victims(victims)

    async def evict_router_session(self, router_session_id: str) -> int:
        """Tear down all pooled upstream sessions owned by a router session."""
        async with self._pool_lock:
            victim_keys = [k for k in self._pool if k[1] == router_session_id]
            victims: list[tuple[str, str, UpstreamAdapter]] = []
            for k in victim_keys:
                entry = self._pool.pop(k, None)
                if entry is not None:
                    victims.append((k[0], k[1], entry.adapter))
            pending_keys = [k for k in self._pool_pending if k[1] == router_session_id]
            pending = [self._pool_pending.pop(k) for k in pending_keys]

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        for sid, rsid, adapter in victims:
            await self._safe_close(adapter)
            if self._audit is not None:
                self._audit.upstream_session("evicted", sid, rsid)
        return len(victims)

    def pool_size(self) -> int:
        return len(self._pool)

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
