"""Auth provider unit tests (P0-2 regression + P1-1 JWT provider)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from starlette.requests import Request

from concierge.errors import Unauthorized
from concierge.server import auth as auth_module
from concierge.server.auth import (
    AuthProviderConfig,
    JwtBearerAuth,
    LocalhostAllowAuth,
    NoAuth,
    StaticBearerAuth,
    build_auth_provider,
)

_TEST_SECRET = "test-signing-secret"
_TEST_JWKS = {
    "keys": [
        {
            "kty": "oct",
            "kid": "test-hs256",
            "alg": "HS256",
            "k": base64.urlsafe_b64encode(_TEST_SECRET.encode()).decode().rstrip("="),
        }
    ]
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_hs256_jwt(
    *,
    secret: str = _TEST_SECRET,
    kid: str = "test-hs256",
    sub: str = "user-42",
    iss: str = "https://issuer.example",
    aud: str = "concierge",
    exp: int | None = None,
    jti: str | None = "jti-1",
) -> str:
    """Build a signed HS256 JWT for tests (stdlib only)."""
    if exp is None:
        exp = int(time.time()) + 3600
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload: dict[str, str | int] = {"sub": sub, "iss": iss, "aud": aud, "exp": exp}
    if jti is not None:
        payload["jti"] = jti
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


def _request(
    *,
    authorization: str | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
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


@pytest.mark.asyncio
async def test_no_auth_always_succeeds():
    res = await NoAuth().authenticate(_request())
    assert res.tenant_id == "default"
    assert res.subject is None


@pytest.mark.asyncio
async def test_bearer_accepts_valid_token():
    auth = StaticBearerAuth(["secret-one", "secret-two"])
    res = await auth.authenticate(_request(authorization="Bearer secret-one"))
    assert res.subject is not None
    # P0-2: subject is an opaque token id, never raw token bytes.
    assert res.subject.startswith("token:")
    assert "secret-one" not in res.subject
    assert "secret" not in res.subject


@pytest.mark.asyncio
async def test_bearer_subject_is_stable_and_distinct_per_token():
    auth = StaticBearerAuth(["secret-one", "secret-two"])
    a = await auth.authenticate(_request(authorization="Bearer secret-one"))
    a_again = await auth.authenticate(_request(authorization="Bearer secret-one"))
    b = await auth.authenticate(_request(authorization="Bearer secret-two"))
    assert a.subject == a_again.subject  # same token → same id
    assert a.subject != b.subject  # different token → different id


@pytest.mark.asyncio
async def test_bearer_checks_every_configured_digest(monkeypatch: pytest.MonkeyPatch):
    real_compare = auth_module.hmac.compare_digest
    calls: list[tuple[bytes, bytes]] = []

    def recording_compare(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", recording_compare)

    auth = StaticBearerAuth(["secret-one", "secret-two", "secret-three"])
    await auth.authenticate(_request(authorization="Bearer secret-one"))

    # Matching the first configured token must not short-circuit; every digest is checked.
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_bearer_rejects_missing_header():
    auth = StaticBearerAuth(["secret-one"])
    with pytest.raises(Unauthorized, match="missing bearer"):
        await auth.authenticate(_request())


@pytest.mark.asyncio
async def test_bearer_rejects_invalid_token():
    auth = StaticBearerAuth(["secret-one"])
    with pytest.raises(Unauthorized, match="invalid bearer"):
        await auth.authenticate(_request(authorization="Bearer wrong"))


@pytest.mark.asyncio
async def test_bearer_rejects_all_when_empty_string_in_tokens():
    """A config like bearer_tokens: [\"\"] must reject every request."""
    auth = StaticBearerAuth([""])
    with pytest.raises(Unauthorized, match="invalid bearer"):
        await auth.authenticate(_request(authorization="Bearer " ''))
    with pytest.raises(Unauthorized, match="invalid bearer"):
        await auth.authenticate(_request(authorization="Bearer any-token"))


@pytest.mark.asyncio
async def test_bearer_rejects_all_when_unset_var_expands_to_empty():
    """${UNSET:-} expands to \"; the resulting empty digest list must reject all."""
    auth = StaticBearerAuth(["${UNSET:-}"])
    with pytest.raises(Unauthorized, match="invalid bearer"):
        await auth.authenticate(_request(authorization="Bearer "))
    with pytest.raises(Unauthorized, match="invalid bearer"):
        await auth.authenticate(_request(authorization="Bearer anything"))


@pytest.mark.asyncio
async def test_localhost_auth_allows_loopback():
    res = await LocalhostAllowAuth().authenticate(_request(client_host="127.0.0.1"))
    assert res.subject == "localhost"


@pytest.mark.asyncio
async def test_localhost_auth_rejects_remote_client():
    with pytest.raises(Unauthorized, match="non-localhost"):
        await LocalhostAllowAuth().authenticate(_request(client_host="203.0.113.1"))


@pytest.mark.asyncio
async def test_jwt_accepts_valid_token():
    auth = JwtBearerAuth(
        issuer="https://issuer.example",
        audience="concierge",
        jwks=_TEST_JWKS,
    )
    token = _make_hs256_jwt()
    res = await auth.authenticate(_request(authorization=f"Bearer {token}"))
    assert res.subject.startswith("jwt:")
    assert token not in (res.subject or "")
    assert "user-42" not in (res.subject or "")


@pytest.mark.asyncio
async def test_jwt_rejects_expired_token():
    auth = JwtBearerAuth(
        issuer="https://issuer.example",
        audience="concierge",
        jwks=_TEST_JWKS,
    )
    token = _make_hs256_jwt(exp=int(time.time()) - 60)
    with pytest.raises(Unauthorized, match="expired"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_jwt_rejects_bad_signature():
    auth = JwtBearerAuth(
        issuer="https://issuer.example",
        audience="concierge",
        jwks=_TEST_JWKS,
    )
    token = _make_hs256_jwt(secret="wrong-secret")
    with pytest.raises(Unauthorized, match="invalid bearer token"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_jwt_rejects_revoked_jti():
    auth = JwtBearerAuth(
        issuer="https://issuer.example",
        audience="concierge",
        jwks=_TEST_JWKS,
        revoked_jti={"jti-revoked"},
    )
    token = _make_hs256_jwt(jti="jti-revoked")
    with pytest.raises(Unauthorized, match="revoked"):
        await auth.authenticate(_request(authorization=f"Bearer {token}"))


@pytest.mark.asyncio
async def test_build_auth_provider_static():
    provider = build_auth_provider(
        AuthProviderConfig(provider="static", bearer_tokens=["alpha"]),
    )
    assert isinstance(provider, StaticBearerAuth)
    res = await provider.authenticate(_request(authorization="Bearer alpha"))
    assert res.subject.startswith("token:")


@pytest.mark.asyncio
async def test_build_auth_provider_jwt():
    provider = build_auth_provider(
        AuthProviderConfig(
            provider="jwt",
            jwt_issuer="https://issuer.example",
            jwt_audience="concierge",
            jwt_jwks=_TEST_JWKS,
        ),
    )
    assert isinstance(provider, JwtBearerAuth)
    token = _make_hs256_jwt()
    res = await provider.authenticate(_request(authorization=f"Bearer {token}"))
    assert res.subject.startswith("jwt:")
