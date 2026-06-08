"""
GatewayService — the business-logic surface used by the transport facade.

The facade (server/facade.py) translates JSON-RPC messages into method calls on
this service. The service does not know about HTTP, SSE, sessions IDs from the
wire, or auth headers — only `Session` objects and typed args.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..adapters.manager import AdapterManager
from ..core.catalog import Catalog
from ..core.notifications import NotificationBus
from ..core.publishing import PublishingService
from ..core.session import SessionManager
from ..core.types import (
    PrimitiveType,
    Session,
)
from ..errors import (
    GatewayError,
    InvalidParams,
    MethodNotFound,
    UnknownPrimitive,
)
from ..observability import trace_span
from ..policy.engine import PolicyEngine
from ..util.audit import AuditLogger
from ..util.log import get_logger
from ..util.payload import PayloadOptions, cap_result_text, slim_schema
from .primitives import GatewayPrimitive, builtin_primitives
from .profiles import ProfileRegistry

# P1-8 additive (safe default None = pass-through)
try:
    from ..util.output_filter import OutputFilter
except Exception:  # noqa: BLE001
    OutputFilter = None  # type: ignore[misc,assignment]

_log = get_logger("concierge.service")


PROTOCOL_VERSION = "2025-06-18"

# Supported MCP protocol versions for negotiation (see P0-4).
# We currently support only the 2025-06-18 Streamable HTTP baseline.
SUPPORTED_PROTOCOL_VERSIONS: list[str] = ["2025-06-18"]


def negotiate_protocol_version(client_version: str | None) -> str:
    """Negotiate the protocol version for a session.

    Per MCP spec and P0-4:
    - Echo client's version if we support it.
    - Otherwise fall back to our best (current) version.
    - In the future we can return an error for truly incompatible versions.
    """
    if client_version and client_version in SUPPORTED_PROTOCOL_VERSIONS:
        return client_version
    return PROTOCOL_VERSION


def _safe_bytes(obj: Any) -> int | None:
    """Serialized UTF-8 byte size of a payload, for metrics. None if unmeasurable."""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


class GatewayService:
    def __init__(
        self,
        catalog: Catalog,
        publishing: PublishingService,
        sessions: SessionManager,
        adapters: AdapterManager,
        policy: PolicyEngine,
        audit: AuditLogger,
        bus: NotificationBus,
        profiles: ProfileRegistry,
        payload: PayloadOptions | None = None,
        output_filter: Any | None = None,  # P1-8 additive, None = disabled (conservative)
        approval_store: Any | None = None,  # P1-3 additive, None = no pending-approval listing
    ) -> None:
        self.catalog = catalog
        self.publishing = publishing
        self.sessions = sessions
        self.adapters = adapters
        self.policy = policy
        self.audit = audit
        self.bus = bus
        self.profiles = profiles
        self.payload = payload or PayloadOptions()
        self.output_filter = output_filter  # may be OutputFilter instance or None
        self.approval_store = approval_store  # P1-3 ApprovalStore or None
        self.primitives: dict[str, GatewayPrimitive] = builtin_primitives()

    # ------------------------------------------------------------------
    # MCP method handlers
    # ------------------------------------------------------------------
    async def initialize(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        client_version = params.get("protocolVersion") if isinstance(params, dict) else None
        agreed_version = negotiate_protocol_version(client_version)

        session.client_info = params.get("clientInfo", {}) if isinstance(params, dict) else {}
        session.protocol_version = agreed_version
        self.audit.session_created(
            session.session_id,
            client=session.client_info.get("name"),
            protocol=session.protocol_version,
        )
        await self._apply_auto_profiles(session)
        return {
            "protocolVersion": agreed_version,
            "serverInfo": {"name": "concierge", "version": "0.1.0"},
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
                "logging": {},
            },
        }

    async def _apply_auto_profiles(self, session: Session) -> None:
        """Publish auto_apply profiles' tools at session init.

        This makes proxied tools present in the very first tools/list, so clients
        that don't react to notifications/tools/list_changed can still reach them.
        """
        for profile in self.profiles.auto_apply_profiles():
            names = await profile.resolve(self.catalog)
            enabled, _ = await self.publishing.enable(session, names, by=f"profile:{profile.name}")
            if profile.name not in session.active_profiles:
                session.active_profiles.append(profile.name)
                await self.sessions.save(session)
            if enabled:
                self.audit.tool_enabled(session.session_id, enabled, by=f"profile:{profile.name}")

    async def tools_list(self, session: Session) -> dict[str, Any]:
        # Always include gateway-native primitives, then add per-session published tools.
        tools: list[dict[str, Any]] = []
        for p in self.primitives.values():
            tools.append(p.as_tool_descriptor())
        for entry in await self.publishing.list_published(session, PrimitiveType.TOOL):
            schema = entry.input_schema or {"type": "object"}
            if self.payload.slim_tools_list:
                schema = slim_schema(
                    schema,
                    max_desc=self.payload.max_schema_description_chars,
                    drop_examples=self.payload.drop_schema_examples,
                )
            tools.append({
                "name": entry.canonical_name,
                "title": entry.display_label,
                "description": entry.short_description,
                "inputSchema": schema,
            })
        return {"tools": tools}

    async def resources_list(self, session: Session) -> dict[str, Any]:
        resources = []
        for entry in await self.publishing.list_published(session, PrimitiveType.RESOURCE):
            resources.append({
                "uri": entry.upstream_name,                # upstream URI preserved
                "name": entry.canonical_name,
                "description": entry.short_description,
            })
        return {"resources": resources}

    async def prompts_list(self, session: Session) -> dict[str, Any]:
        prompts = []
        for entry in await self.publishing.list_published(session, PrimitiveType.PROMPT):
            prompts.append({
                "name": entry.canonical_name,
                "description": entry.short_description,
            })
        return {"prompts": prompts}

    # ------------------------------------------------------------------
    async def tools_call(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise InvalidParams("tools/call params must be object")
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            raise InvalidParams("tools/call: name is required")
        if not isinstance(args, dict):
            raise InvalidParams("tools/call: arguments must be object")

        # Gateway-native primitives.
        if name in self.primitives:
            handler = self.primitives[name].handler
            try:
                return await handler(session, args, self)
            except ValueError as e:
                raise InvalidParams(str(e))

        # Upstream-routed primitive — must be published and policy-cleared.
        entry = await self.publishing.require_published(session, name, PrimitiveType.TOOL)
        await self.policy.authorize_call(session, entry, args)

        t0 = time.perf_counter()
        try:
            with trace_span(
                "concierge.gateway.tools_call",
                session_id=session.session_id,
                tool_name=name,
                server_id=entry.server_id,
            ):
                result = await self.adapters.call_tool(
                    entry.server_id, entry.upstream_name, args,
                    router_session_id=session.session_id,
                )
            # P1-8 additive output filter hook (secret redaction etc on results)
            if self.output_filter is not None and hasattr(self.output_filter, "apply"):
                result = self.output_filter.apply(result)
            result = cap_result_text(result, self.payload.max_result_bytes)
            latency = (time.perf_counter() - t0) * 1000
            self.audit.tool_called(
                session.session_id, name, entry.server_id, True, latency,
                request_bytes=_safe_bytes(args), response_bytes=_safe_bytes(result),
            )
            return result
        except GatewayError as e:
            latency = (time.perf_counter() - t0) * 1000
            self.audit.tool_called(
                session.session_id, name, entry.server_id, False, latency, e.code
            )
            raise

    async def resources_read(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise InvalidParams("resources/read params must be object")
        canonical = params.get("name") or params.get("uri")
        if not isinstance(canonical, str):
            raise InvalidParams("resources/read: name or uri required")
        # Allow lookup by either canonical name or upstream URI.
        entry = await self.catalog.get(canonical)
        if entry is None:
            # Fall back: scan for matching upstream uri among published resources.
            for e in await self.publishing.list_published(session, PrimitiveType.RESOURCE):
                if e.upstream_name == canonical:
                    entry = e
                    break
        if entry is None:
            raise UnknownPrimitive(canonical)
        entry = await self.publishing.require_published(
            session, entry.canonical_name, PrimitiveType.RESOURCE
        )
        return await self.adapters.read_resource(
            entry.server_id, entry.upstream_name, router_session_id=session.session_id,
        )

    async def prompts_get(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise InvalidParams("prompts/get params must be object")
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidParams("prompts/get: name required")
        entry = await self.publishing.require_published(session, name, PrimitiveType.PROMPT)
        return await self.adapters.get_prompt(
            entry.server_id, entry.upstream_name, params.get("arguments") or {},
            router_session_id=session.session_id,
        )

    # ------------------------------------------------------------------
    async def dispatch(self, session: Session, method: str, params: dict[str, Any] | None) -> Any:
        params = params if isinstance(params, dict) else {}
        with trace_span(
            "concierge.gateway.dispatch",
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            method=method,
        ):
            if method == "initialize":
                return await self.initialize(session, params)
            if method == "tools/list":
                return await self.tools_list(session)
            if method == "tools/call":
                return await self.tools_call(session, params)
            if method == "resources/list":
                return await self.resources_list(session)
            if method == "resources/read":
                return await self.resources_read(session, params)
            if method == "prompts/list":
                return await self.prompts_list(session)
            if method == "prompts/get":
                return await self.prompts_get(session, params)
            if method == "ping":
                return {}
            raise MethodNotFound(f"unknown method: {method}")
