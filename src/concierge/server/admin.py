"""Debug/admin endpoints. Same auth as /mcp."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..adapters.manager import AdapterManager
from ..core.catalog import Catalog
from ..core.session import SessionManager
from ..gateway.service import GatewayService
from .auth import AuthProvider


def build_admin_router(
    *,
    auth: AuthProvider,
    catalog: Catalog,
    adapters: AdapterManager,
    sessions: SessionManager,
    service: GatewayService,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    async def _auth_dep(request: Request) -> None:
        await auth.authenticate(request)

    @router.get("/health", dependencies=[Depends(_auth_dep)])
    async def health() -> dict:
        return {
            "ok": True,
            "catalog_count": catalog.count(),
            "session_count": len(sessions.all()),
            "servers": [a.health().model_dump() for a in adapters.all()],
        }

    @router.get("/catalog", dependencies=[Depends(_auth_dep)])
    async def list_catalog() -> dict:
        return {"entries": [e.model_dump() for e in catalog.list(limit=10_000)]}

    @router.get("/sessions", dependencies=[Depends(_auth_dep)])
    async def list_sessions() -> dict:
        return {"sessions": [s.model_dump(mode="json") for s in sessions.all()]}

    @router.post("/refresh/{server_id}", dependencies=[Depends(_auth_dep)])
    async def refresh(server_id: str) -> dict:
        n = await adapters.refresh_server(server_id)
        return {"server": server_id, "entries": n}

    return router
