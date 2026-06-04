"""P1-1 app-level wiring: chain config, admin token endpoints, tenant propagation.

Exercises the gateway end-to-end through the FastAPI TestClient: a configured
provider chain authenticates real requests, the admin path mints/rotates/revokes
tokens, a minted tenant token authenticates and its tenant lands on the session,
and the legacy static-bearer config still works unchanged.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from concierge.config import (
    AuthConfig,
    GatewayConfig,
    GatewayHttpConfig,
)
from concierge.server.app import build_app

_ORIGINS = ["http://localhost", "http://127.0.0.1"]


def _cfg(auth: AuthConfig) -> GatewayConfig:
    return GatewayConfig(
        auth=auth,
        gateway=GatewayHttpConfig(allowed_origins=_ORIGINS),
        upstream_servers=[],
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )


def _initialize(client: TestClient, *, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )


def _assert_authed(resp) -> str:
    """A successful initialize returns 200 + an MCP-Session-Id header."""
    assert resp.status_code == 200
    sid = resp.headers.get("MCP-Session-Id")
    assert sid
    return sid


def _assert_rejected(resp) -> None:
    """On the initialize/create path a failed auth surfaces as a JSON-RPC error
    inside an HTTP 200 envelope (the established-session path returns HTTP 401);
    no session is minted either way."""
    assert resp.status_code == 200
    assert resp.headers.get("MCP-Session-Id") is None
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Backwards compatibility: legacy static bearer still works unchanged.
# ---------------------------------------------------------------------------


def test_legacy_bearer_config_still_authenticates():
    cfg = _cfg(AuthConfig(type="bearer", bearer_tokens=["legacy-secret"]))
    with TestClient(build_app(cfg)) as client:
        _assert_authed(_initialize(client, token="legacy-secret"))
        _assert_rejected(_initialize(client, token="wrong"))


# ---------------------------------------------------------------------------
# Provider chain: tenant_token + bearer both enabled.
# ---------------------------------------------------------------------------


def test_chain_tenant_token_then_static_bearer():
    cfg = _cfg(
        AuthConfig(
            type="none",  # ignored when providers is set
            providers=["tenant_token", "bearer"],
            bearer_tokens=["static-fallback"],
        )
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        # Mint a tenant token via the admin path (any chain credential authorizes
        # admin too; use the static bearer here).
        mint = client.post(
            "/admin/tokens",
            headers={"Authorization": "Bearer static-fallback"},
            json={"tenant_id": "acme"},
        )
        assert mint.status_code == 200
        minted = mint.json()
        assert minted["tenant_id"] == "acme"

        # The minted tenant token authenticates and stamps tenant_id on the session.
        init = _initialize(client, token=minted["token"])
        assert init.status_code == 200
        sid = init.headers["MCP-Session-Id"]

        sessions = client.get(
            "/admin/sessions", headers={"Authorization": "Bearer static-fallback"}
        ).json()["sessions"]
        target = next(s for s in sessions if s["session_id"] == sid)
        assert target["tenant_id"] == "acme"

        # The static-bearer fallback in the same chain still works.
        assert _initialize(client, token="static-fallback").status_code == 200


def test_chain_denies_unknown_credential():
    cfg = _cfg(AuthConfig(providers=["tenant_token"]))
    with TestClient(build_app(cfg)) as client:
        # No tenant token minted yet → unknown bearer is rejected.
        _assert_rejected(_initialize(client, token="bogus"))


# ---------------------------------------------------------------------------
# Revocation through the admin path kills a minted token.
# ---------------------------------------------------------------------------


def test_admin_revoke_blocks_minted_tenant_token():
    cfg = _cfg(
        AuthConfig(providers=["tenant_token", "bearer"], bearer_tokens=["admin-key"])
    )
    with TestClient(build_app(cfg)) as client:
        minted = client.post(
            "/admin/tokens",
            headers={"Authorization": "Bearer admin-key"},
            json={"tenant_id": "t1"},
        ).json()

        # Works before revocation.
        _assert_authed(_initialize(client, token=minted["token"]))

        # Revoke by token id.
        rev = client.post(
            "/admin/revocations",
            headers={"Authorization": "Bearer admin-key"},
            json={"token_id": minted["token_id"]},
        )
        assert rev.status_code == 200

        # Now rejected.
        _assert_rejected(_initialize(client, token=minted["token"]))


def test_admin_rotate_revokes_old_token():
    cfg = _cfg(
        AuthConfig(providers=["tenant_token", "bearer"], bearer_tokens=["admin-key"])
    )
    with TestClient(build_app(cfg)) as client:
        first = client.post(
            "/admin/tokens",
            headers={"Authorization": "Bearer admin-key"},
            json={"tenant_id": "t2"},
        ).json()
        rotated = client.post(
            "/admin/tokens/rotate",
            headers={"Authorization": "Bearer admin-key"},
            json={"tenant_id": "t2", "old_token_id": first["token_id"]},
        ).json()
        assert rotated["revoked_old"] is True

        # Old secret no longer resolves; new one does.
        _assert_rejected(_initialize(client, token=first["token"]))
        _assert_authed(_initialize(client, token=rotated["token"]))


def test_admin_list_tokens_never_leaks_secret():
    cfg = _cfg(
        AuthConfig(providers=["tenant_token", "bearer"], bearer_tokens=["admin-key"])
    )
    with TestClient(build_app(cfg)) as client:
        minted = client.post(
            "/admin/tokens",
            headers={"Authorization": "Bearer admin-key"},
            json={"tenant_id": "t3"},
        ).json()
        listed = client.get(
            "/admin/tokens", headers={"Authorization": "Bearer admin-key"}
        ).json()["tokens"]
        ids = {t["token_id"] for t in listed}
        assert minted["token_id"] in ids
        # No raw secret anywhere in the listing payload.
        import json as _json

        assert minted["token"] not in _json.dumps(listed)


# ---------------------------------------------------------------------------
# Tenant propagation reaches the rate-limit bucket (P1-4 keys by tenant).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_propagation_into_session_and_ratelimit_key():
    from concierge.policy.ratelimit import encode_key

    cfg = _cfg(
        AuthConfig(providers=["tenant_token", "bearer"], bearer_tokens=["admin-key"])
    )
    app = build_app(cfg)
    with TestClient(app) as client:
        minted = client.post(
            "/admin/tokens",
            headers={"Authorization": "Bearer admin-key"},
            json={"tenant_id": "tenant-rl"},
        ).json()
        init = _initialize(client, token=minted["token"])
        sid = init.headers["MCP-Session-Id"]

    sessions = await app.state.sessions.all()
    target = next(s for s in sessions if s.session_id == sid)
    assert target.tenant_id == "tenant-rl"
    # The rate-limit bucket key for this session now carries the real tenant,
    # not the "default" placeholder.
    key = encode_key(target.tenant_id, target.session_id, "some__tool")
    assert key.startswith("rl:v1:tenant-rl:")
