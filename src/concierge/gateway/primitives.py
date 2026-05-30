"""
Gateway-native tool primitives.

These are the *only* tools downstream clients see on a fresh connection.
Everything else lives in the catalog and is published per-session by these
primitives.

Each primitive is registered with:
  - canonical name        (e.g. "gateway_discover_catalog")
  - JSON Schema for args
  - sync handler that takes (session, args, gateway_service) -> result dict
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..core.types import PrimitiveType, Session


GatewayHandler = Callable[[Session, dict[str, Any], "GatewayService"], Awaitable[dict[str, Any]]]  # noqa: F821


@dataclass
class GatewayPrimitive:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: GatewayHandler

    def as_tool_descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# --------------------------------------------------------------------------- #
#  Handler implementations                                                    #
# --------------------------------------------------------------------------- #

async def _h_discover_catalog(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    # Optional workflow-scoped discovery: restrict to what a named profile would
    # enable, so a model can browse just the tools relevant to a workflow.
    names: set[str] | None = None
    profile_name = args.get("profile")
    if profile_name:
        profile = svc.profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"unknown profile: {profile_name}")
        names = set(profile.resolve(svc.catalog))

    entries = svc.catalog.list(
        query=args.get("query"),
        server=args.get("server"),
        category=args.get("category"),
        tags=args.get("tags"),
        primitive_type=(
            PrimitiveType(args["primitive_type"]) if args.get("primitive_type") else None
        ),
        max_risk=args.get("max_risk"),
        names=names,
        limit=int(args.get("limit", 25)),
        offset=int(args.get("offset", 0)),
    )

    return {
        "content": [{"type": "text", "text": _summarize_count(entries)}],
        "structuredContent": {
            "entries": [_compact_entry(e) for e in entries],
            "total_in_page": len(entries),
            "offset": int(args.get("offset", 0)),
        },
    }


def _compact_entry(e) -> dict[str, Any]:
    # Always-present, high-signal fields a model needs to pick a tool.
    out: dict[str, Any] = {
        "name": e.canonical_name,
        "label": e.display_label,
        "server": e.server_id,
        "type": e.primitive_type.value,
        "purpose": e.short_description,
        "risk": e.risk_level.value,
    }
    # Token-saving: omit empty/null/default-false fields rather than emitting
    # them on every entry. Each arg drops null descriptions too.
    if e.usage_guidance:
        out["when_to_use"] = e.usage_guidance
    if e.argument_summary:
        out["args"] = [a.model_dump(exclude_none=True) for a in e.argument_summary]
    if e.tags:
        out["tags"] = e.tags
    if e.categories:
        out["categories"] = e.categories
    if e.requires_approval:
        out["requires_approval"] = True
    if e.requires_auth:
        out["requires_auth"] = True
    return out


def _summarize_count(entries: list) -> str:
    if not entries:
        return "No catalog entries matched."
    sample = ", ".join(e.canonical_name for e in entries[:5])
    return f"{len(entries)} matching catalog entries. e.g. {sample}"


async def _h_enable_tools(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    names = args.get("names") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("names must be a list of strings")
    enabled, skipped = svc.publishing.enable(session, names, by="client")
    svc.audit.tool_enabled(session.session_id, enabled, by="client")
    return {
        "content": [{"type": "text", "text": f"Enabled {len(enabled)} tool(s); skipped {len(skipped)}."}],
        "structuredContent": {"enabled": enabled, "skipped": skipped},
    }


async def _h_disable_tools(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    names = args.get("names") or []
    # "all": true is the schema-declared way to clear everything; `names` is an
    # array of canonical names only (no "*" sentinel — that contradicted the schema).
    if args.get("all") is True:
        n = svc.publishing.disable_all(session)
        svc.audit.tool_disabled(session.session_id, ["*"])
        return {
            "content": [{"type": "text", "text": f"Disabled all {n} published primitives."}],
            "structuredContent": {"disabled_count": n},
        }
    removed = svc.publishing.disable(session, names)
    svc.audit.tool_disabled(session.session_id, removed)
    return {
        "content": [{"type": "text", "text": f"Disabled {len(removed)} tool(s)."}],
        "structuredContent": {"disabled": removed},
    }


async def _h_list_active_tools(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    out: dict[str, list[dict[str, Any]]] = {"tools": [], "resources": [], "prompts": []}
    for ptype, key in (
        (PrimitiveType.TOOL, "tools"),
        (PrimitiveType.RESOURCE, "resources"),
        (PrimitiveType.PROMPT, "prompts"),
    ):
        for e in svc.publishing.list_published(session, ptype):
            out[key].append({"name": e.canonical_name, "server": e.server_id, "label": e.display_label})
    return {
        "content": [{"type": "text", "text": f"Active: {len(out['tools'])} tools, {len(out['resources'])} resources, {len(out['prompts'])} prompts."}],
        "structuredContent": out,
    }


async def _h_list_servers(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    servers = []
    for adapter in svc.adapters.all():
        h = adapter.health()
        servers.append({
            "server_id": h.server_id,
            "transport": h.transport.value,
            "connected": h.connected,
            "last_error": h.last_error,
            "consecutive_failures": h.consecutive_failures,
        })
    return {
        "content": [{"type": "text", "text": f"{len(servers)} upstream server(s) configured."}],
        "structuredContent": {"servers": servers},
    }


async def _h_list_profiles(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    profiles = []
    for p in svc.profiles.all():
        names = p.resolve(svc.catalog)
        profiles.append({
            "name": p.name,
            "description": p.description,
            "enables_count": len(names),
            "sample": names[:5],
        })
    return {
        "content": [{"type": "text", "text": f"{len(profiles)} profile(s) available."}],
        "structuredContent": {"profiles": profiles},
    }


async def _h_use_profile(session: Session, args: dict[str, Any], svc: "GatewayService") -> dict[str, Any]:  # noqa: F821
    name = args.get("profile")
    if not isinstance(name, str):
        raise ValueError("profile must be a string")
    profile = svc.profiles.get(name)
    if profile is None:
        raise ValueError(f"unknown profile: {name}")
    names = profile.resolve(svc.catalog)
    enabled, skipped = svc.publishing.enable(session, names, by=f"profile:{name}")
    if name not in session.active_profiles:
        session.active_profiles.append(name)
    svc.audit.tool_enabled(session.session_id, enabled, by=f"profile:{name}")
    return {
        "content": [{"type": "text", "text": f"Profile {name!r} enabled {len(enabled)} primitive(s)."}],
        "structuredContent": {"profile": name, "enabled": enabled, "skipped": skipped},
    }


# --------------------------------------------------------------------------- #
#  Registry                                                                   #
# --------------------------------------------------------------------------- #

def builtin_primitives() -> dict[str, GatewayPrimitive]:
    primitives: list[GatewayPrimitive] = [
        GatewayPrimitive(
            name="gateway_discover_catalog",
            title="Discover catalog",
            description=(
                "Browse the gateway's catalog of upstream tools, resources, and prompts. "
                "Returns compact entries (name, server, purpose, arguments summary). "
                "Use this before enabling tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text substring filter."},
                    "server": {"type": "string", "description": "Restrict to one upstream server id."},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "primitive_type": {"type": "string", "enum": ["tool", "resource", "prompt"]},
                    "max_risk": {"type": "string", "enum": ["low", "medium", "high", "dangerous"]},
                    "profile": {"type": "string", "description": "Restrict results to what this profile would enable."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                },
            },
            handler=_h_discover_catalog,
        ),
        GatewayPrimitive(
            name="gateway_enable_tools",
            title="Enable tools",
            description=(
                "Publish catalog entries into this session so they appear in tools/list "
                "(or resources/list / prompts/list). Triggers a list_changed notification."
            ),
            input_schema={
                "type": "object",
                "required": ["names"],
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Canonical names (e.g. 'github.search_repos').",
                    }
                },
            },
            handler=_h_enable_tools,
        ),
        GatewayPrimitive(
            name="gateway_disable_tools",
            title="Disable tools",
            description="Remove previously enabled primitives from this session's active set.",
            input_schema={
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}},
                    "all": {"type": "boolean", "default": False},
                },
            },
            handler=_h_disable_tools,
        ),
        GatewayPrimitive(
            name="gateway_list_active_tools",
            title="List active tools",
            description="Show currently published tools/resources/prompts for this session.",
            input_schema={"type": "object", "properties": {}},
            handler=_h_list_active_tools,
        ),
        GatewayPrimitive(
            name="gateway_list_servers",
            title="List upstream servers",
            description="Show each upstream MCP server's id, transport, and health.",
            input_schema={"type": "object", "properties": {}},
            handler=_h_list_servers,
        ),
        GatewayPrimitive(
            name="gateway_list_profiles",
            title="List profiles",
            description=(
                "List the curated capability bundles (profiles) this gateway offers, "
                "with how many primitives each would enable and a small sample. Use "
                "this to pick a workflow, then apply it with gateway_use_profile."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_h_list_profiles,
        ),
        GatewayPrimitive(
            name="gateway_use_profile",
            title="Apply a profile",
            description=(
                "Enable a curated bundle of capabilities. Profile names come from the "
                "gateway's configuration (e.g. 'github-review', 'network-admin')."
            ),
            input_schema={
                "type": "object",
                "required": ["profile"],
                "properties": {"profile": {"type": "string"}},
            },
            handler=_h_use_profile,
        ),
    ]
    return {p.name: p for p in primitives}
