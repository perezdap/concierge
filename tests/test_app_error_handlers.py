"""App-level exception handlers for GatewayError / Unauthorized.

These map gateway JSON-RPC error codes to the right HTTP status and return a
clean JSON envelope instead of bubbling up as an unhandled exception (which
uvicorn logs as a multi-line traceback per request — noise on every
unauthenticated browser probe).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from concierge.config import AuthConfig, GatewayConfig
from concierge.errors import (
    GW_UNAUTHORIZED,
    Forbidden,
    RateLimited,
    Unauthorized,
    UpstreamTimeout,
)
from concierge.server.app import build_app


def test_unauthenticated_admin_api_returns_clean_401(
    bearer_gateway_config: GatewayConfig,
) -> None:
    """An auth-protected admin API call with no credential becomes a 401 with a
    WWW-Authenticate hint and a JSON-RPC-shaped error body — not a traceback."""
    with TestClient(build_app(bearer_gateway_config)) as client:
        resp = client.get(
            "/admin/upstreams", headers={"Accept": "application/json"}
        )
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
        body = resp.json()
        assert body["error"]["code"] == GW_UNAUTHORIZED
        assert "message" in body["error"]


def test_unauthorized_omits_bearer_challenge_for_non_bearer_auth() -> None:
    """A 401 under non-bearer auth (e.g. localhost) must NOT claim a Bearer
    scheme — that hint is wrong and misleads clients. Raise Unauthorized via a
    throwaway route so the assertion is independent of the auth provider's own
    accept/reject logic."""
    app = build_app(GatewayConfig(auth=AuthConfig(type="localhost")))

    @app.get("/_test/unauth")
    async def _unauth() -> None:
        raise Unauthorized("no credential")

    with TestClient(app) as client:
        resp = client.get("/_test/unauth")
        assert resp.status_code == 401
        assert "WWW-Authenticate" not in resp.headers
        assert resp.json()["error"]["code"] == GW_UNAUTHORIZED


def test_valid_token_still_reaches_admin_api(
    bearer_gateway_config: GatewayConfig,
) -> None:
    """The handler doesn't block authenticated requests."""
    with TestClient(build_app(bearer_gateway_config)) as client:
        resp = client.get(
            "/admin/upstreams",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer test-secret-token",
            },
        )
        assert resp.status_code == 200
        assert "upstreams" in resp.json()


def test_gateway_error_code_maps_to_http_status(
    gateway_config: GatewayConfig,
) -> None:
    """GatewayError subclasses surface with their mapped HTTP status and the
    JSON-RPC error code preserved in the body. Probe a few representative codes
    via throwaway routes wired onto the built app."""
    app = build_app(gateway_config)

    @app.get("/_test/forbidden")
    async def _forbidden() -> None:
        raise Forbidden("nope")

    @app.get("/_test/ratelimited")
    async def _ratelimited() -> None:
        raise RateLimited.with_retry_after("slow down", 3.0)

    @app.get("/_test/timeout")
    async def _timeout() -> None:
        raise UpstreamTimeout("upstream took too long")

    with TestClient(app) as client:
        forbidden = client.get("/_test/forbidden")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == -32002
        # Non-auth GatewayErrors go through the base handler, which never adds a
        # WWW-Authenticate challenge (only the dedicated Unauthorized handler does).
        assert "WWW-Authenticate" not in forbidden.headers

        limited = client.get("/_test/ratelimited")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == -32003
        assert limited.json()["error"]["data"]["retry_after"] == 3

        timeout = client.get("/_test/timeout")
        assert timeout.status_code == 504
        assert timeout.json()["error"]["code"] == -32011
