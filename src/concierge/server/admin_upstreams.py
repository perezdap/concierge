"""Admin upstream server management API (P2-ADMIN-5).

Mutates ``upstream_servers`` on the runtime config draft; test-connection uses an
ephemeral adapter without registering on the live AdapterManager.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters.factory import build_adapter
from ..adapters.manager import AdapterManager
from ..admin.config_draft import (
    ApplyDraftError,
    apply_draft_live,
    config_for_reads,
    ensure_draft,
    validate_upstream_dict,
    validation_error_detail,
)
from ..admin.config_store import ConfigStore
from ..admin.redaction import redact_config_for_api
from ..admin.reload import ReloadCoordinator
from ..admin.secrets import merge_write_only_secrets
from ..config import UpstreamServerConfig
from .auth import AuthProvider, AuthResult


class UpstreamBody(BaseModel):
    upstream: dict[str, Any]


class UpstreamValidateResponse(BaseModel):
    ok: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)


class TestConnectionResponse(BaseModel):
    ok: bool
    server_id: str
    transport: str
    connected: bool = False
    tools_discovered: int = 0
    resources_discovered: int = 0
    prompts_discovered: int = 0
    latency_ms: int | None = None
    error: str | None = None


@dataclass
class AdminUpstreamsDeps:
    auth: AuthProvider
    config_store: ConfigStore
    adapters: AdapterManager
    reload_coordinator: ReloadCoordinator | None = None


def _redact_upstream(entry: dict[str, Any]) -> dict[str, Any]:
    wrapped = redact_config_for_api({"upstream_servers": [entry]})
    servers = wrapped.get("upstream_servers") or []
    return servers[0] if servers else entry


def _find_upstream_index(servers: list[dict[str, Any]], upstream_id: str) -> int:
    for i, entry in enumerate(servers):
        if entry.get("id") == upstream_id:
            return i
    raise HTTPException(status_code=404, detail=f"unknown upstream: {upstream_id}")


def build_admin_upstreams_router(deps: AdminUpstreamsDeps) -> APIRouter:
    router = APIRouter(prefix="/admin/upstreams", tags=["admin-upstreams"])

    async def _auth(request: Request) -> AuthResult:
        return await deps.auth.authenticate(request)

    @router.get("")
    async def list_upstreams(_auth: AuthResult = Depends(_auth)) -> dict:
        cfg = await config_for_reads(deps.config_store)
        servers = cfg.get("upstream_servers") or []
        return {
            "upstreams": [_redact_upstream(s) for s in servers],
            "source": "draft" if (await deps.config_store.get_draft()) else "active",
        }

    @router.get("/{upstream_id}")
    async def get_upstream(
        upstream_id: str,
        _auth: AuthResult = Depends(_auth),
    ) -> dict:
        cfg = await config_for_reads(deps.config_store)
        servers = cfg.get("upstream_servers") or []
        idx = _find_upstream_index(servers, upstream_id)
        return {"upstream": _redact_upstream(servers[idx])}

    @router.post("")
    async def create_upstream(
        body: UpstreamBody,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        upstream_id = body.upstream.get("id")
        if not upstream_id or not isinstance(upstream_id, str):
            raise HTTPException(status_code=400, detail="upstream.id is required")
        cfg = await ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        servers = list(cfg.get("upstream_servers") or [])
        if any(s.get("id") == upstream_id for s in servers):
            raise HTTPException(
                status_code=409,
                detail=f"upstream already exists: {upstream_id}",
            )
        validation = validate_upstream_dict(body.upstream)
        if not validation.ok:
            raise HTTPException(status_code=400, detail=validation_error_detail(validation))
        servers.append(body.upstream)
        cfg["upstream_servers"] = servers
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {"upstream": _redact_upstream(body.upstream), "version_id": draft.id}

    @router.put("/{upstream_id}")
    async def update_upstream(
        upstream_id: str,
        body: UpstreamBody,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        if body.upstream.get("id") not in (None, upstream_id):
            raise HTTPException(status_code=400, detail="upstream.id must match path")
        cfg = await ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        servers = list(cfg.get("upstream_servers") or [])
        idx = _find_upstream_index(servers, upstream_id)
        merged = merge_write_only_secrets(body.upstream, servers[idx])
        merged["id"] = upstream_id
        validation = validate_upstream_dict(merged)
        if not validation.ok:
            raise HTTPException(status_code=400, detail=validation_error_detail(validation))
        servers[idx] = merged
        cfg["upstream_servers"] = servers
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {"upstream": _redact_upstream(merged), "version_id": draft.id}

    @router.delete("/{upstream_id}")
    async def delete_upstream(
        upstream_id: str,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        cfg = await ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        servers = list(cfg.get("upstream_servers") or [])
        idx = _find_upstream_index(servers, upstream_id)
        removed = servers.pop(idx)
        cfg["upstream_servers"] = servers
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {
            "removed_id": upstream_id,
            "upstream": _redact_upstream(removed),
            "version_id": draft.id,
        }

    @router.post("/{upstream_id}/validate")
    async def validate_upstream(
        upstream_id: str,
        body: UpstreamBody | None = None,
        _auth: AuthResult = Depends(_auth),
    ) -> UpstreamValidateResponse:
        if body is not None and body.upstream.get("id") not in (None, upstream_id):
            raise HTTPException(status_code=400, detail="upstream.id must match path")
        if body is not None:
            data = body.upstream
            data["id"] = upstream_id
        else:
            cfg = await config_for_reads(deps.config_store)
            servers = cfg.get("upstream_servers") or []
            idx = _find_upstream_index(servers, upstream_id)
            data = servers[idx]
        result = validate_upstream_dict(data)
        return UpstreamValidateResponse(
            ok=result.ok,
            issues=[i.model_dump() for i in result.issues],
        )

    @router.post("/{upstream_id}/test-connection")
    async def test_connection(
        upstream_id: str,
        body: UpstreamBody | None = None,
        _auth: AuthResult = Depends(_auth),
    ) -> TestConnectionResponse:
        if body is not None and body.upstream.get("id") not in (None, upstream_id):
            raise HTTPException(status_code=400, detail="upstream.id must match path")
        if body is not None:
            data = body.upstream
            data["id"] = upstream_id
        else:
            cfg = await config_for_reads(deps.config_store)
            servers = cfg.get("upstream_servers") or []
            idx = _find_upstream_index(servers, upstream_id)
            data = servers[idx]
        validation = validate_upstream_dict(data)
        if not validation.ok:
            return TestConnectionResponse(
                ok=False,
                server_id=upstream_id,
                transport=str(data.get("transport", "")),
                error=(
                    validation.issues[0].message
                    if validation.issues
                    else "invalid upstream"
                ),
            )
        cfg = UpstreamServerConfig.model_validate(data)
        adapter = build_adapter(cfg)
        started = time.perf_counter()
        try:
            await adapter.connect()
            await adapter.initialize()
            tools = await adapter.list_tools()
            resources = await adapter.list_resources()
            prompts = await adapter.list_prompts()
            latency_ms = int((time.perf_counter() - started) * 1000)
            return TestConnectionResponse(
                ok=True,
                server_id=upstream_id,
                transport=cfg.transport,
                connected=True,
                tools_discovered=len(tools),
                resources_discovered=len(resources),
                prompts_discovered=len(prompts),
                latency_ms=latency_ms,
            )
        except Exception as e:  # noqa: BLE001
            return TestConnectionResponse(
                ok=False,
                server_id=upstream_id,
                transport=cfg.transport,
                error=str(e),
            )
        finally:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001
                pass

    @router.post("/{upstream_id}/refresh")
    async def refresh_upstream_catalog(
        upstream_id: str,
        _auth: AuthResult = Depends(_auth),
    ) -> dict:
        try:
            entries = await deps.adapters.refresh_server(upstream_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"server": upstream_id, "entries": entries}

    @router.post("/apply")
    async def apply_upstream_draft(
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        try:
            active, result = await apply_draft_live(
                deps.config_store,
                reload_coordinator=deps.reload_coordinator,
                subject=auth_result.subject,
            )
        except ApplyDraftError as e:
            if e.code == "apply_reload_failed":
                raise HTTPException(
                    status_code=e.status_code,
                    detail={"code": e.code, "message": e.message},
                ) from e
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        return {
            "version_id": active.id,
            "applied": True,
            "reloaded": result.reload_coordinator_configured,
        }

    return router