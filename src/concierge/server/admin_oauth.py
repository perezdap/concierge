"""Admin upstream OAuth router factory (P2-ADMIN-7).

Mounted by ``build_app`` / ``build_admin_router`` in a later wave; this module
only exposes ``build_admin_oauth_router`` so Builder 1 can include it without
duplicating endpoint logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..admin.credential_store import UpstreamCredentialStore
from ..admin.oauth import UpstreamOAuthService
from ..util.audit import AuditLogger
from .auth import AuthProvider, AuthResult


class DiscoverRequest(BaseModel):
    issuer: str = Field(min_length=1)


class SignInStartRequest(BaseModel):
    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    scopes: str = ""
    client_secret: str | None = None
    redirect_uri: str | None = None


class ClientCredentialsRequest(BaseModel):
    token_endpoint: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    scopes: str = ""
    issuer: str | None = None


@dataclass
class AdminOAuthDeps:
    auth: AuthProvider
    oauth: UpstreamOAuthService
    credentials: UpstreamCredentialStore
    audit: AuditLogger
    public_base_url: str = "http://127.0.0.1:8765"


def build_admin_oauth_router(deps: AdminOAuthDeps) -> APIRouter:
    router = APIRouter(prefix="/admin/oauth", tags=["admin-oauth"])

    async def _auth(request: Request) -> AuthResult:
        return await deps.auth.authenticate(request)

    @router.post("/{upstream_id}/discover")
    async def discover(
        upstream_id: str,
        body: DiscoverRequest,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, Any]:
        try:
            doc = await deps.oauth.discover(body.issuer)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "upstream_id": upstream_id,
            "issuer": doc.issuer,
            "authorization_endpoint": doc.authorization_endpoint,
            "token_endpoint": doc.token_endpoint,
            "revocation_endpoint": doc.revocation_endpoint,
        }

    @router.post("/{upstream_id}/sign-in/start")
    async def sign_in_start(
        upstream_id: str,
        body: SignInStartRequest,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, str]:
        redirect = body.redirect_uri or f"{deps.public_base_url.rstrip('/')}/admin/oauth/callback"
        try:
            discovery = await deps.oauth.discover(body.issuer)
            auth_url, state = deps.oauth.begin_authorization_code(
                upstream_id=upstream_id,
                discovery=discovery,
                client_id=body.client_id,
                redirect_uri=redirect,
                scopes=body.scopes,
                client_secret=body.client_secret,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"authorization_url": auth_url, "state": state, "redirect_uri": redirect}

    @router.get("/callback")
    async def oauth_callback(
        request: Request,
        code: str,
        state: str,
    ) -> dict[str, str]:
        try:
            await deps.oauth.complete_callback(code=code, state=state)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"status": "connected", "state": state}

    @router.post("/{upstream_id}/client-credentials")
    async def client_credentials(
        upstream_id: str,
        body: ClientCredentialsRequest,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, Any]:
        try:
            await deps.oauth.client_credentials(
                upstream_id=upstream_id,
                token_endpoint=body.token_endpoint,
                client_id=body.client_id,
                client_secret=body.client_secret,
                scopes=body.scopes,
                issuer=body.issuer,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e)) from e
        status = await deps.credentials.status(upstream_id)
        return {"status": "connected", "upstream_id": upstream_id, "credential": status}

    @router.delete("/{upstream_id}")
    async def disconnect(
        upstream_id: str,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, str]:
        await deps.oauth.disconnect(upstream_id)
        return {"status": "disconnected", "upstream_id": upstream_id}

    @router.get("/{upstream_id}/status")
    async def oauth_status(
        upstream_id: str,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, Any]:
        status = await deps.credentials.status(upstream_id)
        if status is None:
            return {"upstream_id": upstream_id, "connected": False}
        return {"upstream_id": upstream_id, "connected": True, "credential": status}

    return router