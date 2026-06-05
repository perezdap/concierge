"""Tests for upstream OAuth admin subsystem (P2-ADMIN-7)."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from concierge.admin.credential_store import UpstreamCredentialStore
from concierge.admin.oauth import (
    OAuthPendingStore,
    UpstreamOAuthService,
    validate_id_token_nonce,
)
from concierge.admin.redaction import assert_no_leaked_secrets
from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key
from concierge.server.admin_oauth import AdminOAuthDeps, build_admin_oauth_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger

_ISSUER = "https://idp.oauth.test"
_ACCESS = "access_upstream_secret_xyz"
_REFRESH = "refresh_upstream_secret_abc"
_CLIENT_ID = "concierge-upstream-client"
_CLIENT_SECRET = "client-secret-value"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fake_id_token(*, nonce: str, sub: str = "test-subject") -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"nonce": nonce, "sub": sub}).encode())
    return f"{header}.{payload}.test-signature"


def _fake_idp_app() -> Starlette:
    last_nonce: list[str] = []
    revoke_calls: list[str] = []

    async def discovery(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": _ISSUER,
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "revocation_endpoint": f"{_ISSUER}/revoke",
            }
        )

    async def authorize(req: Request) -> PlainTextResponse:
        nonce = req.query_params.get("nonce")
        if nonce:
            last_nonce.clear()
            last_nonce.append(nonce)
        return PlainTextResponse("ok")

    async def token(req: Request) -> JSONResponse:
        body = await req.body()
        form = dict(parse_qsl(body.decode(), keep_blank_values=True))
        grant = form.get("grant_type")
        if grant == "authorization_code":
            if form.get("code_verifier") is None:
                return JSONResponse({"error": "missing_verifier"}, status_code=400)
            token_body: dict[str, object] = {
                "access_token": _ACCESS,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": _REFRESH,
            }
            if last_nonce:
                token_body["id_token"] = _fake_id_token(nonce=last_nonce[0])
            return JSONResponse(token_body)
        if grant == "refresh_token":
            return JSONResponse(
                {"access_token": "access_refreshed_new", "token_type": "Bearer", "expires_in": 3600}
            )
        if grant == "client_credentials":
            return JSONResponse(
                {"access_token": "access_cc_flow", "token_type": "Bearer", "expires_in": 600}
            )
        return JSONResponse({"error": "unsupported"}, status_code=400)

    async def revoke(req: Request) -> PlainTextResponse:
        body = await req.body()
        form = dict(parse_qsl(body.decode(), keep_blank_values=True))
        token = form.get("token")
        if token:
            revoke_calls.append(token)
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery, methods=["GET"]),
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
            Route("/revoke", revoke, methods=["POST"]),
        ]
    )
    app.state.last_nonce = last_nonce
    app.state.revoke_calls = revoke_calls
    return app


def _prime_idp_authorize(transport: httpx.ASGITransport, auth_url: str) -> None:
    """Hit the fake IdP authorize URL so PKCE nonce is stored for token exchange."""

    async def _go() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(auth_url)

    asyncio.run(_go())


async def _prime_idp_authorize_async(svc: UpstreamOAuthService, auth_url: str) -> None:
    http = await svc._client()
    await http.get(auth_url)


@pytest.fixture
def idp_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=_fake_idp_app())


@pytest.fixture
def cred_backend() -> InMemoryCredentialStore:
    return InMemoryCredentialStore(key=resolve_fernet_key("oauth-test-key"))


@pytest.fixture
def oauth_stack(
    idp_transport: httpx.ASGITransport,
    cred_backend: InMemoryCredentialStore,
) -> tuple[UpstreamOAuthService, UpstreamCredentialStore]:
    http = httpx.AsyncClient(transport=idp_transport, base_url=_ISSUER)
    store = UpstreamCredentialStore(backend=cred_backend)
    svc = UpstreamOAuthService(
        credential_store=store,
        pending=OAuthPendingStore(ttl_s=120.0),
        audit=AuditLogger(),
        http_client=http,
    )
    return svc, store


@pytest.mark.asyncio
async def test_pkce_flow_stores_encrypted_tokens(oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore]) -> None:
    svc, store = oauth_stack
    discovery = await svc.discover(_ISSUER)
    auth_url, state = svc.begin_authorization_code(
        upstream_id="github",
        discovery=discovery,
        client_id=_CLIENT_ID,
        redirect_uri="http://127.0.0.1:8765/admin/oauth/callback",
        scopes="openid",
    )
    assert "code_challenge=" in auth_url
    parsed = parse_qs(urlparse(auth_url).query)
    assert parsed["state"][0] == state

    await _prime_idp_authorize_async(svc, auth_url)
    await svc.complete_callback(code="auth-code-1", state=state)
    status = await store.status("github")
    assert status is not None
    assert status["access_present"] is True
    assert_no_leaked_secrets(status, needles=[_ACCESS, _REFRESH])


@pytest.mark.asyncio
async def test_refresh_before_upstream_call(oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore]) -> None:
    svc, store = oauth_stack
    from concierge.admin.credential_store import OAuthTokenSet

    await store.save_tokens(
        "srv",
        OAuthTokenSet(
            access_token="expired",
            refresh_token=_REFRESH,
            expires_at=time.time() - 10,
            issuer=_ISSUER,
            client_id=_CLIENT_ID,
            flow="authorization_code",
        ),
    )
    token = await svc.refresh_if_needed(
        "srv",
        token_endpoint=f"{_ISSUER}/token",
        client_id=_CLIENT_ID,
    )
    assert token == "access_refreshed_new"
    assert_no_leaked_secrets(await store.status("srv"), needles=["access_refreshed_new"])


@pytest.mark.asyncio
async def test_client_credentials_flow(oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore]) -> None:
    svc, store = oauth_stack
    await svc.client_credentials(
        upstream_id="svc",
        token_endpoint=f"{_ISSUER}/token",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        issuer=_ISSUER,
    )
    loaded = await store.load_tokens("svc")
    assert loaded is not None
    assert loaded.access_token == "access_cc_flow"


@pytest.mark.asyncio
async def test_disconnect_clears_credentials(
    oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore],
    idp_transport: httpx.ASGITransport,
) -> None:
    svc, store = oauth_stack
    discovery = await svc.discover(_ISSUER)
    auth_url, state = svc.begin_authorization_code(
        upstream_id="x",
        discovery=discovery,
        client_id=_CLIENT_ID,
        redirect_uri="http://localhost/cb",
        scopes="openid",
        client_secret=_CLIENT_SECRET,
    )
    await _prime_idp_authorize_async(svc, auth_url)
    await svc.complete_callback(code="auth-code-1", state=state)
    idp_app = idp_transport.app
    await svc.disconnect("x")
    assert idp_app.state.revoke_calls == [_REFRESH]
    assert await store.status("x") is None


@pytest.mark.asyncio
async def test_openid_nonce_mismatch_rejected(
    oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore],
) -> None:
    svc, _ = oauth_stack
    discovery = await svc.discover(_ISSUER)
    _, state = svc.begin_authorization_code(
        upstream_id="bad-nonce",
        discovery=discovery,
        client_id=_CLIENT_ID,
        redirect_uri="http://localhost/cb",
        scopes="openid",
    )
    pending = svc.pending.pop(state)
    assert pending is not None
    svc.pending.put(pending)
    bad_id_token = _fake_id_token(nonce="wrong-nonce-value")
    with pytest.raises(ValueError, match="nonce mismatch"):
        validate_id_token_nonce(bad_id_token, pending.nonce)


@pytest.mark.asyncio
async def test_complete_callback_rejects_bad_id_token_nonce(
    oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore],
    idp_transport: httpx.ASGITransport,
) -> None:
    svc, _ = oauth_stack
    discovery = await svc.discover(_ISSUER)
    _, state = svc.begin_authorization_code(
        upstream_id="nonce-check",
        discovery=discovery,
        client_id=_CLIENT_ID,
        redirect_uri="http://localhost/cb",
        scopes="openid",
    )
    idp_transport.app.state.last_nonce.clear()
    idp_transport.app.state.last_nonce.append("stale-wrong-nonce")
    with pytest.raises(ValueError, match="nonce mismatch"):
        await svc.complete_callback(code="auth-code-1", state=state)


@pytest.mark.asyncio
async def test_invalid_state_rejected(oauth_stack: tuple[UpstreamOAuthService, UpstreamCredentialStore]) -> None:
    svc, _ = oauth_stack
    with pytest.raises(ValueError, match="invalid or expired"):
        await svc.complete_callback(code="c", state="missing-state")


def test_admin_oauth_router_pkce_via_http(
    idp_transport: httpx.ASGITransport,
    cred_backend: InMemoryCredentialStore,
) -> None:
    http = httpx.AsyncClient(transport=idp_transport, base_url=_ISSUER)
    store = UpstreamCredentialStore(backend=cred_backend)
    svc = UpstreamOAuthService(credential_store=store, http_client=http)
    app = FastAPI()
    deps = AdminOAuthDeps(
        auth=NoAuth(),
        oauth=svc,
        credentials=store,
        audit=AuditLogger(),
        public_base_url="http://testserver",
    )
    app.include_router(build_admin_oauth_router(deps))
    client = TestClient(app)

    r = client.post("/admin/oauth/gh/discover", json={"issuer": _ISSUER})
    assert r.status_code == 200
    assert r.json()["token_endpoint"].endswith("/token")

    r = client.post(
        "/admin/oauth/gh/sign-in/start",
        json={
            "issuer": _ISSUER,
            "client_id": _CLIENT_ID,
            "scopes": "openid",
            "redirect_uri": "http://testserver/admin/oauth/callback",
        },
    )
    assert r.status_code == 200
    body = r.json()
    state = body["state"]
    _prime_idp_authorize(idp_transport, body["authorization_url"])

    r = client.get("/admin/oauth/callback", params={"code": "auth-code-1", "state": state})
    assert r.status_code == 200

    r = client.get("/admin/oauth/gh/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert_no_leaked_secrets(body, needles=[_ACCESS, _REFRESH, _CLIENT_SECRET])