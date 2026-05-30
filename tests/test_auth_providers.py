"""Auth provider unit tests (P0-2 regression surface)."""
from __future__ import annotations

import pytest
from starlette.requests import Request

from concierge.errors import Unauthorized
from concierge.server import auth as auth_module
from concierge.server.auth import LocalhostAllowAuth, NoAuth, StaticBearerAuth


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
async def test_localhost_auth_allows_loopback():
    res = await LocalhostAllowAuth().authenticate(_request(client_host="127.0.0.1"))
    assert res.subject == "localhost"


@pytest.mark.asyncio
async def test_localhost_auth_rejects_remote_client():
    with pytest.raises(Unauthorized, match="non-localhost"):
        await LocalhostAllowAuth().authenticate(_request(client_host="203.0.113.1"))
