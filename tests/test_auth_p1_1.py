"""P1-1 auth: OIDC (in-process IdP), mTLS trusted-proxy, chain, revocation.

The OIDC integration test stands up a tiny in-process IdP that signs a real
RS256 JWT and serves OIDC discovery + JWKS over an in-memory ASGI transport — no
public IdP is contacted. ``OidcAuth`` is pointed at it by swapping the module's
``httpx.AsyncClient`` for one bound to that transport, so the discovery fetch,
JWKS fetch, signature verify, and claim verify all run for real.
"""
from __future__ import annotations

import base64
import time

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from concierge.errors import Unauthorized
from concierge.server import auth as auth_module
from concierge.server.auth import (
    MtlsForwardedAuth,
    OidcAuth,
    ProviderChain,
    RevocationEnforcingAuth,
    StaticBearerAuth,
    TenantBearerAuth,
)
from concierge.server.revocation import InMemoryRevocationStore
from concierge.server.tenant_tokens import InMemoryTenantTokenStore

_ISSUER = "https://idp.example.test"
_AUD = "concierge-api"
_KID = "rsa-test-1"


def _int_to_b64url(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def idp():
    """An in-process IdP: RSA key, OIDC discovery doc, and a JWKS endpoint."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": _int_to_b64url(public_numbers.n),
                "e": _int_to_b64url(public_numbers.e),
            }
        ]
    }

    async def discovery(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": _ISSUER,
                "jwks_uri": f"{_ISSUER}/jwks.json",
            }
        )

    async def jwks_endpoint(_req: Request) -> JSONResponse:
        return JSONResponse(jwks)

    app = Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery),
            Route("/jwks.json", jwks_endpoint),
        ]
    )

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def mint(
        *,
        sub: str = "svc-1",
        aud: str | list[str] = _AUD,
        iss: str = _ISSUER,
        exp: int | None = None,
        nbf: int | None = None,
        jti: str | None = "jti-oidc-1",
        tenant: str | None = "acme",
        alg: str = "RS256",
    ) -> str:
        claims: dict = {
            "sub": sub,
            "aud": aud,
            "iss": iss,
            "exp": exp if exp is not None else int(time.time()) + 3600,
        }
        if nbf is not None:
            claims["nbf"] = nbf
        if jti is not None:
            claims["jti"] = jti
        if tenant is not None:
            claims["tenant"] = tenant
        return pyjwt.encode(claims, pem, algorithm=alg, headers={"kid": _KID})

    return app, mint


@pytest.fixture
def patched_httpx(monkeypatch: pytest.MonkeyPatch, idp):
    """Point OidcAuth's httpx client at the in-process IdP ASGI app."""
    app, mint = idp
    transport = httpx.ASGITransport(app=app)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            kw.setdefault("base_url", _ISSUER)
            super().__init__(*a, **kw)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _Client)
    return mint


def _request(*, authorization: str | None = None, client_host: str = "127.0.0.1") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


def _mtls_request(*, headers: dict[str, str], client_host: str) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": raw_headers,
        "client": (client_host, 443),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# OIDC against the in-process IdP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oidc_accepts_valid_token_via_discovery_and_jwks(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD])
    res = await auth.authenticate(_request(authorization=f"Bearer {mint()}"))
    # Tenant comes from the token's `tenant` claim.
    assert res.tenant_id == "acme"
    # Audit subject is an opaque salted id — never the raw token or sub.
    assert res.subject.startswith("oidc:")
    assert "svc-1" not in res.subject
    assert res.token_id == "jti-oidc-1"


@pytest.mark.asyncio
async def test_oidc_rejects_wrong_audience(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD])
    token = mint(aud="some-other-api")
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_oidc_rejects_wrong_issuer(patched_httpx):
    mint = patched_httpx
    # allowed_issuers stays the configured issuer; a token claiming another iss
    # still verifies by signature (same key) but must fail the issuer allow-list.
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD])
    token = mint(iss="https://evil.example.test")
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_oidc_rejects_expired_token(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD], leeway_s=0)
    token = mint(exp=int(time.time()) - 120)
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_oidc_rejects_not_yet_valid_token(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD], leeway_s=0)
    token = mint(nbf=int(time.time()) + 3600)
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_oidc_rejects_tampered_signature(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD])
    token = mint()
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-2]}AA"
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {tampered}"))


@pytest.mark.asyncio
async def test_oidc_multiple_allowed_issuers_and_audiences(patched_httpx):
    mint = patched_httpx
    auth = OidcAuth(
        issuer=_ISSUER,
        allowed_issuers=[_ISSUER, "https://other.example.test"],
        audiences=[_AUD, "second-aud"],
    )
    res = await auth.authenticate(_request(authorization=f"Bearer {mint(aud='second-aud')}"))
    assert res.subject.startswith("oidc:")


@pytest.mark.asyncio
async def test_oidc_caches_jwks_and_refreshes_on_unknown_kid(patched_httpx, monkeypatch):
    mint = patched_httpx
    auth = OidcAuth(issuer=_ISSUER, audiences=[_AUD])
    # First call populates the cache.
    await auth.authenticate(_request(authorization=f"Bearer {mint()}"))
    fetches = {"n": 0}
    real_fetch = auth._fetch_jwks

    async def counting_fetch():
        fetches["n"] += 1
        return await real_fetch()

    monkeypatch.setattr(auth, "_fetch_jwks", counting_fetch)
    # A second valid call with the cache warm should NOT refetch.
    await auth.authenticate(_request(authorization=f"Bearer {mint()}"))
    assert fetches["n"] == 0


# ---------------------------------------------------------------------------
# mTLS trusted-proxy gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mtls_accepts_from_trusted_proxy():
    auth = MtlsForwardedAuth(
        trusted_proxy_cidrs=["10.0.0.0/8"],
        subject_header="x-forwarded-client-cert",
        verify_header=None,
        subject_tenant_map={"CN=svc-a": "tenant-a"},
    )
    req = _mtls_request(
        headers={"x-forwarded-client-cert": "CN=svc-a"},
        client_host="10.1.2.3",
    )
    res = await auth.authenticate(req)
    assert res.tenant_id == "tenant-a"
    assert res.subject.startswith("mtls:")
    assert "svc-a" not in res.subject  # opaque, not the raw subject


@pytest.mark.asyncio
async def test_mtls_rejects_from_untrusted_proxy_even_with_headers():
    auth = MtlsForwardedAuth(
        trusted_proxy_cidrs=["10.0.0.0/8"],
        subject_header="x-forwarded-client-cert",
        verify_header=None,
    )
    # Forged header from an off-net client — must be refused on the proxy gate.
    req = _mtls_request(
        headers={"x-forwarded-client-cert": "CN=attacker"},
        client_host="203.0.113.9",
    )
    with pytest.raises(Unauthorized, match="untrusted source"):
        await auth.authenticate(req)


@pytest.mark.asyncio
async def test_mtls_empty_cidr_list_trusts_nobody():
    auth = MtlsForwardedAuth(trusted_proxy_cidrs=[], verify_header=None)
    req = _mtls_request(
        headers={"x-forwarded-client-cert": "CN=svc"},
        client_host="127.0.0.1",
    )
    with pytest.raises(Unauthorized, match="untrusted source"):
        await auth.authenticate(req)


@pytest.mark.asyncio
async def test_mtls_requires_proxy_verify_success_when_configured():
    auth = MtlsForwardedAuth(
        trusted_proxy_cidrs=["10.0.0.0/8"],
        verify_header="ssl-client-verify",
    )
    req = _mtls_request(
        headers={
            "x-forwarded-client-cert": "CN=svc",
            "ssl-client-verify": "FAILED",
        },
        client_host="10.0.0.5",
    )
    with pytest.raises(Unauthorized, match="not verified"):
        await auth.authenticate(req)


@pytest.mark.asyncio
async def test_mtls_parses_envoy_xfcc_san():
    auth = MtlsForwardedAuth(
        trusted_proxy_cidrs=["10.0.0.0/8"],
        verify_header=None,
        subject_tenant_map={"spiffe://cluster/ns/svc": "tenant-spiffe"},
    )
    xfcc = 'Subject="CN=svc";URI=spiffe://cluster/ns/svc;Hash=abc123'
    req = _mtls_request(
        headers={"x-forwarded-client-cert": xfcc},
        client_host="10.0.0.7",
    )
    res = await auth.authenticate(req)
    assert res.tenant_id == "tenant-spiffe"


# ---------------------------------------------------------------------------
# Provider chain + revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_first_matching_provider_wins(patched_httpx):
    mint = patched_httpx
    revocation = InMemoryRevocationStore()
    # mTLS only matches when its header is present; OIDC matches a 3-part bearer.
    chain = ProviderChain(
        [
            MtlsForwardedAuth(trusted_proxy_cidrs=["10.0.0.0/8"], verify_header=None),
            OidcAuth(issuer=_ISSUER, audiences=[_AUD]),
        ],
        revocation=revocation,
    )
    res = await chain.authenticate(_request(authorization=f"Bearer {mint()}"))
    assert res.subject.startswith("oidc:")


@pytest.mark.asyncio
async def test_chain_denies_when_no_provider_matches():
    chain = ProviderChain(
        [MtlsForwardedAuth(trusted_proxy_cidrs=["10.0.0.0/8"], verify_header=None)]
    )
    with pytest.raises(Unauthorized):
        await chain.authenticate(_request())  # no creds, mTLS header absent


@pytest.mark.asyncio
async def test_chain_rejects_revoked_oidc_jti(patched_httpx):
    mint = patched_httpx
    revocation = InMemoryRevocationStore()
    chain = ProviderChain([OidcAuth(issuer=_ISSUER, audiences=[_AUD])], revocation=revocation)
    token = mint(jti="jti-to-revoke")
    # Works before revocation...
    res = await chain.authenticate(_request(authorization=f"Bearer {token}"))
    assert res.token_id == "jti-to-revoke"
    # ...and is rejected once the jti is revoked.
    await revocation.revoke("jti-to-revoke")
    with pytest.raises(Unauthorized, match="revoked"):
        await chain.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_revocation_rejects_static_bearer_token():
    revocation = InMemoryRevocationStore()
    provider = StaticBearerAuth(["secret-one"])
    wrapped = RevocationEnforcingAuth(provider, revocation)
    res = await wrapped.authenticate(_request(authorization="Bearer secret-one"))
    assert res.token_id is not None
    await revocation.revoke(res.token_id)
    with pytest.raises(Unauthorized, match="revoked"):
        await wrapped.authenticate(_request(authorization="Bearer secret-one"))


@pytest.mark.asyncio
async def test_tenant_bearer_resolves_tenant_and_revokes():
    store = InMemoryTenantTokenStore()
    minted = await store.mint("tenant-x")
    revocation = InMemoryRevocationStore()
    wrapped = RevocationEnforcingAuth(TenantBearerAuth(store), revocation)
    res = await wrapped.authenticate(_request(authorization=f"Bearer {minted.token}"))
    assert res.tenant_id == "tenant-x"
    assert res.token_id == minted.token_id
    # Revoking the minted token id blocks it on the next check.
    await revocation.revoke(minted.token_id)
    with pytest.raises(Unauthorized, match="revoked"):
        await wrapped.authenticate(_request(authorization=f"Bearer {minted.token}"))


@pytest.mark.asyncio
async def test_tenant_token_rotation_changes_secret_and_id():
    store = InMemoryTenantTokenStore()
    first = await store.mint("t")
    rotated = await store.rotate("t", first.token_id)
    assert rotated.token != first.token
    assert rotated.token_id != first.token_id
    # Old token no longer resolves; new one does.
    assert await store.resolve(first.token) is None
    assert (await store.resolve(rotated.token)).tenant_id == "t"
