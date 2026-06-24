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
    _resource_indicator_for,
    _same_origin,
    _select_dcr_token_endpoint_auth_method,
)
from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key
from concierge.server.admin_oauth import AdminOAuthDeps, build_admin_oauth_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger

_AS = "https://as.mcp.test"
_RESOURCE = "https://mcp.test/mcp"


def _fake_mcp_stack(
    *,
    token_endpoint_auth_methods_supported: list[str] | None = None,
) -> Starlette:
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
        payload: dict[str, object] = {
            "issuer": _AS,
            "authorization_endpoint": f"{_AS}/authorize",
            "token_endpoint": f"{_AS}/token",
            "registration_endpoint": f"{_AS}/register",
        }
        if token_endpoint_auth_methods_supported is not None:
            payload["token_endpoint_auth_methods_supported"] = (
                token_endpoint_auth_methods_supported
            )
        return JSONResponse(payload)

    async def register(req: Request) -> JSONResponse:
        payload = await req.json()
        registered.append(payload)
        response: dict[str, object] = {
            "client_id": "dcr-generated-client",
            "redirect_uris": payload["redirect_uris"],
        }
        if payload.get("token_endpoint_auth_method") != "none":
            response["client_secret"] = "dcr-generated-secret"
        return JSONResponse(response, status_code=201)

    async def authorize(_req: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def token(req: Request) -> JSONResponse:
        body = await req.body()
        form = dict(parse_qs(body.decode(), keep_blank_values=True))
        app.state.token_requests.append(form)
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
    app.state.token_requests = []
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


def test_same_origin():
    assert _same_origin("https://mcp.test/a", "https://mcp.test/b/c")
    assert _same_origin("https://mcp.test:443/a", "https://mcp.test/b")  # default port
    assert not _same_origin("https://mcp.test/a", "https://evil.test/a")  # host
    assert not _same_origin("http://mcp.test/a", "https://mcp.test/a")  # scheme
    assert not _same_origin("https://mcp.test:8443/a", "https://mcp.test/a")  # port


def test_resource_indicator_for_mcp_endpoint_is_origin():
    assert _resource_indicator_for("https://mcp.test/mcp/oauth") == "https://mcp.test"
    assert _resource_indicator_for("https://mcp.test:8443/mcp?x=1") == "https://mcp.test:8443"


def test_select_dcr_token_endpoint_auth_method():
    assert _select_dcr_token_endpoint_auth_method(None) == "none"
    assert _select_dcr_token_endpoint_auth_method(["none"]) == "none"
    assert (
        _select_dcr_token_endpoint_auth_method(["client_secret_post", "client_secret_basic"])
        == "client_secret_post"
    )
    assert _select_dcr_token_endpoint_auth_method(["client_secret_basic"]) == "client_secret_basic"
    with pytest.raises(ValueError, match="compatible token endpoint auth method"):
        _select_dcr_token_endpoint_auth_method(["private_key_jwt"])


@pytest.mark.asyncio
async def test_probe_ignores_cross_origin_resource_metadata_header():
    """SSRF guard: a cross-origin resource_metadata pointer must not be fetched.

    RFC 9728 puts the PRM document on the resource's own origin, so a header
    pointing elsewhere is illegitimate — we fall back to the well-known path on
    the resource origin instead of fetching the attacker-supplied URL.
    """
    evil_hits: list[str] = []

    async def resource(_req: Request) -> Response:
        # 401 header points the PRM at a *different* origin (attacker-controlled).
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer resource_metadata="https://evil.test/prm"'},
        )

    async def evil_prm(_req: Request) -> JSONResponse:
        evil_hits.append("hit")
        return JSONResponse({"authorization_servers": ["https://evil.test/as"]})

    async def prm(_req: Request) -> JSONResponse:
        return JSONResponse({"resource": _RESOURCE, "authorization_servers": [_AS]})

    app = Starlette(
        routes=[
            Route("/mcp", resource, methods=["GET"]),
            Route("/prm", evil_prm, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", prm, methods=["GET"]),
        ]
    )
    svc = _svc(httpx.ASGITransport(app=app))
    servers = await svc.probe_resource_metadata(_RESOURCE)
    # Fell back to the legit well-known PRM; never fetched the evil URL.
    assert servers == [_AS]
    assert evil_hits == []


def test_dcr_reuses_registered_client_across_sign_ins():
    """Repeated Connect clicks reuse one client_id instead of re-registering."""
    app = _fake_mcp_stack()
    client = _client(_svc(httpx.ASGITransport(app=app)))
    first = client.post("/admin/oauth/srv/sign-in/start", json={"resource_url": _RESOURCE})
    second = client.post("/admin/oauth/srv/sign-in/start", json={"resource_url": _RESOURCE})
    assert first.status_code == 200
    assert second.status_code == 200
    # Registration endpoint hit exactly once across both sign-ins.
    assert len(app.state.registered) == 1
    first_cid = parse_qs(urlparse(first.json()["authorization_url"]).query)["client_id"]
    second_cid = parse_qs(urlparse(second.json()["authorization_url"]).query)["client_id"]
    assert first_cid == second_cid == ["dcr-generated-client"]


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


def test_zero_config_sign_in_registers_client_for_resource_and_builds_auth_url():
    app = _fake_mcp_stack()
    transport = httpx.ASGITransport(app=app)
    client = _client(_svc(transport))
    r = client.post(
        "/admin/oauth/srv/sign-in/start",
        json={"resource_url": _RESOURCE},
    )
    assert r.status_code == 200
    url = urlparse(r.json()["authorization_url"])
    assert url.netloc == "as.mcp.test"
    qs = parse_qs(url.query)
    # Dynamically-registered client_id is used, with PKCE and the MCP resource
    # indicator so providers can mint an audience-bound access token.
    assert qs["client_id"] == ["dcr-generated-client"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["resource"] == [_RESOURCE_ORIGIN]
    assert app.state.registered[0]["resource"] == _RESOURCE_ORIGIN
    assert app.state.registered[0]["token_endpoint_auth_method"] == "none"


def test_zero_config_dcr_uses_supported_confidential_client_auth():
    app = _fake_mcp_stack(token_endpoint_auth_methods_supported=["client_secret_post"])
    transport = httpx.ASGITransport(app=app)
    client = _client(_svc(transport))
    r = client.post(
        "/admin/oauth/srv/sign-in/start",
        json={"resource_url": _RESOURCE},
    )
    assert r.status_code == 200
    assert app.state.registered[0]["token_endpoint_auth_method"] == "client_secret_post"
    state = r.json()["state"]
    r = client.get("/admin/oauth/callback", params={"code": "auth-code-1", "state": state})
    assert r.status_code == 200
    assert app.state.token_requests[0]["client_secret"] == ["dcr-generated-secret"]


def test_zero_config_callback_sends_resource_to_token_endpoint():
    app = _fake_mcp_stack()
    transport = httpx.ASGITransport(app=app)
    client = _client(_svc(transport))
    r = client.post(
        "/admin/oauth/srv/sign-in/start",
        json={"resource_url": _RESOURCE},
    )
    assert r.status_code == 200
    state = r.json()["state"]
    r = client.get("/admin/oauth/callback", params={"code": "auth-code-1", "state": state})
    assert r.status_code == 200
    assert app.state.token_requests[0]["resource"] == [_RESOURCE_ORIGIN]


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
