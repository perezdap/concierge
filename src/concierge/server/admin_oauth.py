"""Admin upstream OAuth router factory (P2-ADMIN-7).

Mounted by ``build_app`` / ``build_admin_router`` in a later wave; this module
only exposes ``build_admin_oauth_router`` so Builder 1 can include it without
duplicating endpoint logic.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..admin.credential_store import UpstreamCredentialStore
from ..admin.oauth import OAuthDiscoveryDocument, UpstreamOAuthService
from ..admin.oauth_providers import get_provider, public_catalog
from ..util.audit import AuditLogger
from .auth import AuthProvider, AuthResult


class DiscoverRequest(BaseModel):
    issuer: str = Field(min_length=1)


class ProbeRequest(BaseModel):
    resource_url: str = Field(min_length=1)


class SignInStartRequest(BaseModel):
    # Resolution priority:
    # 1. ``resource_url`` set -> zero-config: probe the MCP server (RFC 9728),
    #    discover its authorization server (RFC 8414/OIDC), and dynamically
    #    register a client (RFC 7591). No provider, secret, or operator setup.
    # 2. ``provider`` set -> built-in preset endpoints/scopes/creds.
    # 3. explicit ``issuer`` + ``client_id`` -> custom OIDC provider.
    resource_url: str | None = None
    provider: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    scopes: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None


class ClientCredentialsRequest(BaseModel):
    token_endpoint: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    scopes: str = ""
    issuer: str | None = None


@dataclass
class _ResolvedSignIn:
    discovery: OAuthDiscoveryDocument
    client_id: str
    client_secret: str | None
    scopes: str
    extra_authorize_params: dict[str, str]


@dataclass
class AdminOAuthDeps:
    auth: AuthProvider
    oauth: UpstreamOAuthService
    credentials: UpstreamCredentialStore
    audit: AuditLogger
    public_base_url: str = "http://127.0.0.1:8765"


async def _resolve_sign_in(
    deps: AdminOAuthDeps, body: SignInStartRequest, *, upstream_id: str
) -> _ResolvedSignIn:
    """Resolve a sign-in request into concrete endpoints/scopes/credentials.

    Three modes (see :class:`SignInStartRequest` for priority):
    * ``resource_url`` -> zero-config DCR (probe + discover + register).
    * ``provider`` -> built-in preset endpoints/scopes/quirks + operator creds.
    * ``issuer`` + ``client_id`` -> custom OIDC.
    """
    redirect_uri = body.redirect_uri or (
        f"{deps.public_base_url.rstrip('/')}/admin/oauth/callback"
    )
    if body.resource_url:
        return await _resolve_dcr(deps, body, upstream_id=upstream_id, redirect_uri=redirect_uri)
    if body.provider:
        preset = get_provider(body.provider)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"unknown provider '{body.provider}'")
        client_id = body.client_id or preset.client_id()
        client_secret = body.client_secret or preset.client_secret()
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"provider '{preset.id}' is not configured: set {preset.env_client_id} "
                    f"and {preset.env_client_secret}, or supply client_id/client_secret"
                ),
            )
        if preset.supports_discovery and preset.issuer:
            discovery = await deps.oauth.discover(preset.issuer)
        else:
            discovery = OAuthDiscoveryDocument(
                issuer=preset.issuer or preset.id,
                authorization_endpoint=preset.authorization_endpoint,
                token_endpoint=preset.token_endpoint,
                revocation_endpoint=preset.revocation_endpoint,
            )
        return _ResolvedSignIn(
            discovery=discovery,
            client_id=client_id,
            client_secret=client_secret,
            scopes=body.scopes or preset.default_scopes,
            extra_authorize_params=dict(preset.extra_authorize_params),
        )

    # Custom OIDC provider path.
    if not body.issuer or not body.client_id:
        raise HTTPException(
            status_code=400,
            detail="issuer and client_id are required when no provider is given",
        )
    discovery = await deps.oauth.discover(body.issuer)
    return _ResolvedSignIn(
        discovery=discovery,
        client_id=body.client_id,
        client_secret=body.client_secret,
        scopes=body.scopes or "",
        extra_authorize_params={},
    )


async def _resolve_dcr(
    deps: AdminOAuthDeps, body: SignInStartRequest, *, upstream_id: str, redirect_uri: str
) -> _ResolvedSignIn:
    """Zero-config resolution for a spec-compliant remote MCP server.

    Probes the resource for its authorization server (RFC 9728), discovers the
    AS metadata (RFC 8414/OIDC), and dynamically registers a public client
    (RFC 7591) so no client_id/secret or operator setup is needed.
    """
    resource_url = body.resource_url or ""
    auth_servers = await deps.oauth.probe_resource_metadata(resource_url)
    if not auth_servers:
        raise HTTPException(
            status_code=422,
            detail=(
                "upstream did not advertise an OAuth authorization server "
                "(no 401 Protected Resource Metadata). Use a provider preset or "
                "supply issuer/client_id instead."
            ),
        )
    discovery = await deps.oauth.discover_authorization_server(auth_servers[0])
    if not discovery.registration_endpoint:
        raise HTTPException(
            status_code=422,
            detail=(
                "authorization server does not support Dynamic Client "
                "Registration. Use a provider preset or supply client_id."
            ),
        )
    # Reuse a previously-registered client for this (upstream, issuer) rather
    # than registering a fresh one on every Connect — repeated sign-ins would
    # otherwise accumulate orphaned client registrations on the auth server.
    existing = await deps.credentials.load_client_registration(upstream_id, discovery.issuer)
    if existing is not None:
        client_id, client_secret = existing
    else:
        client_id, client_secret = await deps.oauth.register_client(
            registration_endpoint=discovery.registration_endpoint,
            redirect_uri=redirect_uri,
        )
        await deps.credentials.save_client_registration(
            upstream_id,
            discovery.issuer,
            client_id=client_id,
            client_secret=client_secret,
        )
    return _ResolvedSignIn(
        discovery=discovery,
        client_id=client_id,
        client_secret=client_secret,
        scopes=body.scopes or "",
        extra_authorize_params={},
    )


def _callback_page(*, ok: bool, message: str, status_code: int = 200) -> HTMLResponse:
    """Tiny self-contained page shown in the OAuth popup after the redirect.

    Posts a message to the opener (the admin console) and auto-closes, so the
    user sees a friendly result instead of raw JSON. ``message`` is escaped to
    prevent HTML injection from provider-supplied error strings.
    """
    safe = html.escape(message)
    status_word = "Connected" if ok else "Sign-in failed"
    color = "#137333" if ok else "#b3261e"
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{status_word}</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
         height: 100vh; margin: 0; background: #f6f8fa; }}
  .card {{ background: #fff; padding: 2rem 2.5rem; border-radius: 12px;
           box-shadow: 0 1px 6px rgba(0,0,0,.12); text-align: center; max-width: 24rem; }}
  h1 {{ color: {color}; font-size: 1.25rem; margin: 0 0 .5rem; }}
  p {{ color: #444; margin: 0; }}
</style></head>
<body><div class="card"><h1>{status_word}</h1><p>{safe}</p></div>
<script>
  try {{ window.opener && window.opener.postMessage(
    {{ type: "concierge:oauth", ok: {str(ok).lower()} }}, "*"); }} catch (e) {{}}
  setTimeout(function () {{ window.close(); }}, {1500 if ok else 4000});
</script></body></html>"""
    return HTMLResponse(content=body, status_code=status_code)


def build_admin_oauth_router(deps: AdminOAuthDeps) -> APIRouter:
    router = APIRouter(prefix="/admin/oauth", tags=["admin-oauth"])

    async def _auth(request: Request) -> AuthResult:
        return await deps.auth.authenticate(request)

    @router.get("/providers")
    async def list_oauth_providers(
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, Any]:
        """Browser-safe catalog of built-in providers (never returns secrets)."""
        return {"providers": public_catalog()}

    @router.post("/{upstream_id}/probe")
    async def probe(
        upstream_id: str,
        body: ProbeRequest,
        _auth: AuthResult = Depends(_auth),
    ) -> dict[str, Any]:
        """Check whether an MCP upstream supports zero-config OAuth (DCR).

        Returns ``{supports_dcr, authorization_server}`` so the UI can offer a
        no-secret one-click Connect when the upstream is spec-compliant.
        """
        auth_servers = await deps.oauth.probe_resource_metadata(body.resource_url)
        if not auth_servers:
            return {"supports_dcr": False, "authorization_server": None, "protected": False}
        try:
            discovery = await deps.oauth.discover_authorization_server(auth_servers[0])
        except Exception:  # noqa: BLE001
            return {
                "supports_dcr": False,
                "authorization_server": auth_servers[0],
                "protected": True,
            }
        return {
            "supports_dcr": bool(discovery.registration_endpoint),
            "authorization_server": discovery.issuer,
            "protected": True,
        }

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
            params = await _resolve_sign_in(deps, body, upstream_id=upstream_id)
            auth_url, state = deps.oauth.begin_authorization_code(
                upstream_id=upstream_id,
                discovery=params.discovery,
                client_id=params.client_id,
                redirect_uri=redirect,
                scopes=params.scopes,
                client_secret=params.client_secret,
                extra_authorize_params=params.extra_authorize_params,
            )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"authorization_url": auth_url, "state": state, "redirect_uri": redirect}

    @router.get("/callback")
    async def oauth_callback(
        request: Request,
        code: str,
        state: str,
    ) -> HTMLResponse:
        try:
            await deps.oauth.complete_callback(code=code, state=state)
        except ValueError as e:
            return _callback_page(ok=False, message=str(e), status_code=400)
        except Exception as e:  # noqa: BLE001
            return _callback_page(ok=False, message=str(e), status_code=502)
        return _callback_page(ok=True, message="Connected. You can close this window.")

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