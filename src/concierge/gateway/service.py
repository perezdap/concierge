"""
GatewayService — the business-logic surface used by the transport facade.

The facade (server/facade.py) translates JSON-RPC messages into method calls on
this service. The service does not know about HTTP, SSE, sessions IDs from the
wire, or auth headers — only `Session` objects and typed args.
"""
from __future__ import annotations

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
from ..policy.engine import PolicyEngine
from ..util.audit import AuditLogger
from ..util.log import get_logger
from .primitives import GatewayPrimitive, builtin_primitives
from .profiles import ProfileRegistry

_log = get_logger("concierge.service")


PROTOCOL_VERSION = "2025-06-18"


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
    ) -> None:
        self.catalog = catalog
        self.publishing = publishing
        self.sessions = sessions
        self.adapters = adapters
        self.policy = policy
        self.audit = audit
        self.bus = bus
        self.profiles = profiles
        self.primitives: dict[str, GatewayPrimitive] = builtin_primitives()

    # ------------------------------------------------------------------
    # MCP method handlers
    # ------------------------------------------------------------------
    async def initialize(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        session.client_info = params.get("clientInfo", {}) if isinstance(params, dict) else {}
        session.protocol_version = params.get("protocolVersion") if isinstance(params, dict) else None
        self.audit.session_created(
            session.session_id,
            client=session.client_info.get("name"),
            protocol=session.protocol_version,
        )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "concierge", "version": "0.1.0"},
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True},
                "prompts": {"listChanged": True},
                "logging": {},
            },
        }

    async def tools_list(self, session: Session) -> dict[str, Any]:
        # Always include gateway-native primitives, then add per-session published tools.
        tools: list[dict[str, Any]] = []
        for p in self.primitives.values():
            tools.append(p.as_tool_descriptor())
        for entry in self.publishing.list_published(session, PrimitiveType.TOOL):
            tools.append({
                "name": entry.canonical_name,
                "title": entry.display_label,
                "description": entry.short_description,
                "inputSchema": entry.input_schema or {"type": "object"},
            })
        return {"tools": tools}

    async def resources_list(self, session: Session) -> dict[str, Any]:
        resources = []
        for entry in self.publishing.list_published(session, PrimitiveType.RESOURCE):
            resources.append({
                "uri": entry.upstream_name,                # upstream URI preserved
                "name": entry.canonical_name,
                "description": entry.short_description,
            })
        return {"resources": resources}

    async def prompts_list(self, session: Session) -> dict[str, Any]:
        prompts = []
        for entry in self.publishing.list_published(session, PrimitiveType.PROMPT):
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
        entry = self.publishing.require_published(session, name, PrimitiveType.TOOL)
        await self.policy.authorize_call(session, entry, args)

        t0 = time.perf_counter()
        try:
            result = await self.adapters.call_tool(entry.server_id, entry.upstream_name, args)
            latency = (time.perf_counter() - t0) * 1000
            self.audit.tool_called(session.session_id, name, entry.server_id, True, latency)
            return result
        except GatewayError as e:
            latency = (time.perf_counter() - t0) * 1000
            self.audit.tool_called(session.session_id, name, entry.server_id, False, latency, e.code)
            raise

    async def resources_read(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise InvalidParams("resources/read params must be object")
        canonical = params.get("name") or params.get("uri")
        if not isinstance(canonical, str):
            raise InvalidParams("resources/read: name or uri required")
        # Allow lookup by either canonical name or upstream URI.
        entry = self.catalog.get(canonical)
        if entry is None:
            # Fall back: scan for matching upstream uri among published resources.
            for e in self.publishing.list_published(session, PrimitiveType.RESOURCE):
                if e.upstream_name == canonical:
                    entry = e
                    break
        if entry is None:
            raise UnknownPrimitive(canonical)
        entry = self.publishing.require_published(session, entry.canonical_name, PrimitiveType.RESOURCE)
        return await self.adapters.read_resource(entry.server_id, entry.upstream_name)

    async def prompts_get(self, session: Session, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise InvalidParams("prompts/get params must be object")
        name = params.get("name")
        if not isinstance(name, str):
            raise InvalidParams("prompts/get: name required")
        entry = self.publishing.require_published(session, name, PrimitiveType.PROMPT)
        return await self.adapters.get_prompt(entry.server_id, entry.upstream_name, params.get("arguments") or {})

    # ------------------------------------------------------------------
    async def dispatch(self, session: Session, method: str, params: dict[str, Any] | None) -> Any:
        params = params if isinstance(params, dict) else {}
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
