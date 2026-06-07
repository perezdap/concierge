"""Zero-config OAuth via the MCP authorization spec (DCR).

Covers the no-secret "just click Connect" path: probing a protected MCP server
for its authorization server (RFC 9728), discovering AS metadata (RFC 8414/OIDC),
and dynamically registering a public client (RFC 7591) — no operator setup.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from concierge.admin.credential_store import UpstreamCredentialStore
from concierge.admin.oauth import (
    OAuthPendingStore,
    UpstreamOAuthService,
    _parse_resource_metadata_url,
)
from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key
from concierge.server.admin_oauth import AdminOAuthDeps, build_admin_oauth_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger

_AS = "https://as.mcp.test"
_RESOURCE = "https://mcp.test/mcp"


def _fake_mcp_stack() -> Starlette:
    """A protected MCP resource + its DCR-capable authorization server."""
    registered: list[dict] = []

    async def resource(_req: Request) -> Response:
        # Protected: 401 with PRM pointer per RFC 9728 Section 5.1.
        return Response(
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    'Bearer resource_metadata='
                    f'"{_RESOURCE_ORIGIN}/.well-known/oauth-protected-resource"'
                )
            },
        )

    async def prm(_req: Request) -> JSONResponse:
        return JSONResponse(
            {"resource": _RESOURCE, "authorization_servers": [_AS]}
        )

    async def as_meta(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": _AS,
                "authorization_endpoint": f"{_AS}/authorize",
                "token_endpoint": f"{_AS}/token",
                "registration_endpoint": f"{_AS}/register",
            }
        )

    async def register(req: Request) -> JSONResponse:
        payload = await req.json()
        registered.append(payload)
        # Public client: no secret returned.
        return JSONResponse(
            {"client_id": "dcr-generated-client", "redirect_uris": payload["redirect_uris"]},
            status_code=201,
        )

    async def authorize(_req: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def token(_req: Request) -> JSONResponse:
        return JSONResponse(
            {"access_token": "dcr-access", "token_type": "Bearer", "expires_in": 3600}
        )

    app = Starlette(
        routes=[
            Route("/mcp", resource, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", as_meta, methods=["GET"]),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
        ]
    )
    app.state.registered = registered
    return app


_RESOURCE_ORIGIN = "https://mcp.test"


def _svc(transport: httpx.ASGITransport) -> UpstreamOAuthService:
    http = httpx.AsyncClient(transport=transport)
    store = UpstreamCredentialStore(
        backend=InMemoryCredentialStore(key=resolve_fernet_key("dcr-key"))
    )
    return UpstreamOAuthService(
        credential_store=store, pending=OAuthPendingStore(), http_client=http
    )


def _client(svc: UpstreamOAuthService) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_admin_oauth_router(
            AdminOAuthDeps(
                auth=NoAuth(),
                oauth=svc,
                credentials=svc.credentials,
                audit=AuditLogger(),
                public_base_url="https://concierge.test",
            )
        )
    )
    return TestClient(app)


def test_parse_resource_metadata_url():
    hdr = 'Bearer realm="mcp", resource_metadata="https://x.test/.well-known/oauth-protected-resource"'
    assert (
        _parse_resource_metadata_url(hdr)
        == "https://x.test/.well-known/oauth-protected-resource"
    )
    assert _parse_resource_metadata_url("Bearer") is None


@pytest.mark.asyncio
async def test_probe_returns_authorization_server():
    transport = httpx.ASGITransport(app=_fake_mcp_stack())
    svc = _svc(transport)
    servers = await svc.probe_resource_metadata(_RESOURCE)
    assert servers == [_AS]


@pytest.mark.asyncio
async def test_discover_authorization_server_reads_registration_endpoint():
    transport = httpx.ASGITransport(app=_fake_mcp_stack())
    svc = _svc(transport)
    doc = await svc.discover_authorization_server(_AS)
    assert doc.registration_endpoint == f"{_AS}/register"
    assert doc.token_endpoint == f"{_AS}/token"


@pytest.mark.asyncio
async def test_register_client_is_public_no_secret():
    transport = httpx.ASGITransport(app=_fake_mcp_stack())
    svc = _svc(transport)
    client_id, client_secret = await svc.register_client(
        registration_endpoint=f"{_AS}/register",
        redirect_uri="https://concierge.test/admin/oauth/callback",
    )
    assert client_id == "dcr-generated-client"
    assert client_secret is None


def test_probe_endpoint_reports_dcr_support():
    transport = httpx.ASGITransport(app=_fake_mcp_stack())
    client = _client(_svc(transport))
    r = client.post("/admin/oauth/srv/probe", json={"resource_url": _RESOURCE})
    assert r.status_code == 200
    body = r.json()
    assert body["supports_dcr"] is True
    assert body["protected"] is True
    assert body["authorization_server"] == _AS


def test_zero_config_sign_in_registers_and_builds_auth_url():
    transport = httpx.ASGITransport(app=_fake_mcp_stack())
    client = _client(_svc(transport))
    r = client.post(
        "/admin/oauth/srv/sign-in/start",
        json={"resource_url": _RESOURCE},
    )
    assert r.status_code == 200
    url = urlparse(r.json()["authorization_url"])
    assert url.netloc == "as.mcp.test"
    qs = parse_qs(url.query)
    # Dynamically-registered client_id is used, with PKCE.
    assert qs["client_id"] == ["dcr-generated-client"]
    assert qs["code_challenge_method"] == ["S256"]


def test_unprotected_upstream_reports_no_dcr():
    async def open_resource(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True})  # 200, not protected

    app = Starlette(routes=[Route("/mcp", open_resource, methods=["GET"])])
    transport = httpx.ASGITransport(app=app)
    client = _client(_svc(transport))
    r = client.post("/admin/oauth/srv/probe", json={"resource_url": "https://open.test/mcp"})
    assert r.status_code == 200
    assert r.json()["supports_dcr"] is False


def test_zero_config_sign_in_rejects_unprotected_upstream():
    async def open_resource(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", open_resource, methods=["GET"])])
    transport = httpx.ASGITransport(app=app)
    client = _client(_svc(transport))
    r = client.post(
        "/admin/oauth/srv/sign-in/start",
        json={"resource_url": "https://open.test/mcp"},
    )
    assert r.status_code == 422
