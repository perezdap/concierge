"""Debug/admin endpoints. Same auth as /mcp.

Adds the P1-1 token-administration surface: mint / rotate / revoke per-tenant
tokens and revoke any token id (including OIDC ``jti``). The minted secret is
returned exactly once, at creation — it is never persisted in the clear and never
re-readable. These endpoints sit behind the same ``AuthProvider`` as the rest of
the gateway; in production, restrict them further at the ingress.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters.manager import AdapterManager
from ..core.catalog import Catalog
from ..core.session import SessionManager
from ..gateway.service import GatewayService
from .auth import AuthProvider
from .revocation import RevocationStore
from .tenant_tokens import TenantTokenStore


class MintTokenRequest(BaseModel):
    tenant_id: str = Field(min_length=1)


class RotateTokenRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    old_token_id: str = Field(min_length=1)
    # Revoke the old id too (default), so a cached copy of the old secret dies.
    revoke_old: bool = True


class RevokeRequest(BaseModel):
    token_id: str = Field(min_length=1)
    # Optional TTL so a short-lived JWT's revocation self-prunes when it expires.
    ttl_s: int | None = None


def build_admin_router(
    *,
    auth: AuthProvider,
    catalog: Catalog,
    adapters: AdapterManager,
    sessions: SessionManager,
    service: GatewayService,
    revocation: RevocationStore | None = None,
    tenant_tokens: TenantTokenStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    async def _auth_dep(request: Request) -> None:
        await auth.authenticate(request)

    @router.get("/health", dependencies=[Depends(_auth_dep)])
    async def health() -> dict:
        return {
            "ok": True,
            "catalog_count": await catalog.count(),
            "session_count": len(await sessions.all()),
            "servers": [a.health().model_dump() for a in adapters.all()],
        }

    @router.get("/catalog", dependencies=[Depends(_auth_dep)])
    async def list_catalog() -> dict:
        return {"entries": [e.model_dump() for e in await catalog.list(limit=10_000)]}

    @router.get("/sessions", dependencies=[Depends(_auth_dep)])
    async def list_sessions() -> dict:
        return {"sessions": [s.model_dump(mode="json") for s in await sessions.all()]}

    @router.post("/refresh/{server_id}", dependencies=[Depends(_auth_dep)])
    async def refresh(server_id: str) -> dict:
        n = await adapters.refresh_server(server_id)
        return {"server": server_id, "entries": n}

    # -- P1-1 token administration -----------------------------------------

    @router.post("/tokens", dependencies=[Depends(_auth_dep)])
    async def mint_token(body: MintTokenRequest) -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        minted = await tenant_tokens.mint(body.tenant_id)
        # The raw token is returned ONCE here; it is never stored or logged.
        return {
            "token": minted.token,
            "token_id": minted.token_id,
            "tenant_id": minted.tenant_id,
        }

    @router.post("/tokens/rotate", dependencies=[Depends(_auth_dep)])
    async def rotate_token(body: RotateTokenRequest) -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        minted = await tenant_tokens.rotate(body.tenant_id, body.old_token_id)
        if body.revoke_old and revocation is not None:
            await revocation.revoke(body.old_token_id)
        return {
            "token": minted.token,
            "token_id": minted.token_id,
            "tenant_id": minted.tenant_id,
            "revoked_old": bool(body.revoke_old and revocation is not None),
        }

    @router.get("/tokens", dependencies=[Depends(_auth_dep)])
    async def list_tokens() -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        # Never returns secrets — only the opaque ids + tenant bindings.
        return {
            "tokens": [
                {"token_id": r.token_id, "tenant_id": r.tenant_id}
                for r in await tenant_tokens.list_records()
            ]
        }

    @router.post("/revocations", dependencies=[Depends(_auth_dep)])
    async def revoke(body: RevokeRequest) -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        await revocation.revoke(body.token_id, ttl_s=body.ttl_s)
        return {"revoked": body.token_id}

    @router.delete("/revocations/{token_id}", dependencies=[Depends(_auth_dep)])
    async def unrevoke(token_id: str) -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        await revocation.unrevoke(token_id)
        return {"unrevoked": token_id}

    @router.get("/revocations", dependencies=[Depends(_auth_dep)])
    async def list_revocations() -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        return {"revoked": await revocation.all()}

    return router
