"""Regression tests for the Origin allow-list (P0-1).

The canonical Origin policy lives in ``concierge.server.origin`` — pure helpers
plus a global ASGI middleware (``OriginAllowlistMiddleware``) that enforces the
allow-list on every guarded route, so no handler can be reached with an
untrusted browser Origin (DNS-rebinding / CSRF defence).
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from concierge.server.origin import (
    OriginAllowlistMiddleware,
    _normalize_origin,
    _origin_allowed,
    _origin_header_allowed,
)


def _request_with_origin(origin: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }
    return Request(scope)


# --- normalize ------------------------------------------------------------


def test_normalize_origin_strips_default_ports():
    assert _normalize_origin("http://localhost:80") == "http://localhost"
    assert _normalize_origin("https://example.com:443") == "https://example.com"


def test_normalize_origin_keeps_non_default_port():
    assert _normalize_origin("http://localhost:8765") == "http://localhost:8765"


def test_normalize_origin_null_literal():
    assert _normalize_origin("null") == "null"


def test_normalize_origin_unparseable_is_none():
    assert _normalize_origin("not a url") is None
    assert _normalize_origin("") is None


# --- allow-list (Request variant) ----------------------------------------


def test_origin_rejects_localhost_evil_com_prefix_attack():
    allowed = ["http://localhost", "http://127.0.0.1"]
    req = _request_with_origin("http://localhost.evil.com")
    assert _origin_allowed(req, allowed) is False


def test_origin_rejects_unlisted_host():
    allowed = ["http://localhost", "http://127.0.0.1"]
    assert _origin_allowed(_request_with_origin("http://evil.example:9999"), allowed) is False


def test_origin_allows_exact_match():
    allowed = ["http://localhost", "http://127.0.0.1"]
    assert _origin_allowed(_request_with_origin("http://localhost"), allowed) is True
    assert _origin_allowed(_request_with_origin("http://127.0.0.1"), allowed) is True


def test_origin_missing_header_allowed():
    assert _origin_allowed(_request_with_origin(None), ["http://localhost"]) is True


def test_origin_null_only_when_explicitly_listed():
    allowed = ["http://localhost"]
    assert _origin_allowed(_request_with_origin("null"), allowed) is False
    assert _origin_allowed(_request_with_origin("null"), ["http://localhost", "null"]) is True


# --- allow-list (raw header variant; used by the middleware) --------------


def test_origin_header_allowed_matches_request_variant():
    allowed = ["http://localhost"]
    assert _origin_header_allowed(None, allowed) is True  # missing → allowed
    assert _origin_header_allowed("http://localhost", allowed) is True
    assert _origin_header_allowed("http://localhost.evil.com", allowed) is False
    assert _origin_header_allowed("null", allowed) is False
    assert _origin_header_allowed("garbage", allowed) is False


# --- middleware -----------------------------------------------------------


def _guarded_app() -> TestClient:
    async def ok(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("reached")

    app = Starlette(routes=[
        Route("/mcp", ok, methods=["GET", "POST", "DELETE"]),
        Route("/open", ok, methods=["GET"]),
    ])
    app.add_middleware(
        OriginAllowlistMiddleware,
        allowed_origins=["http://localhost"],
        guarded_paths=["/mcp"],
    )
    return TestClient(app)


def test_middleware_blocks_disallowed_origin_on_guarded_path():
    client = _guarded_app()
    for method in ("get", "post", "delete"):
        resp = getattr(client, method)("/mcp", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403, method
        assert "reached" not in resp.text


def test_middleware_allows_listed_origin_on_guarded_path():
    client = _guarded_app()
    for method in ("get", "post", "delete"):
        resp = getattr(client, method)("/mcp", headers={"Origin": "http://localhost"})
        assert resp.status_code == 200, method
        assert resp.text == "reached"


def test_middleware_allows_missing_origin():
    client = _guarded_app()
    assert client.post("/mcp").status_code == 200


def test_middleware_ignores_unguarded_paths():
    client = _guarded_app()
    # A hostile Origin on a non-guarded path is the route's own concern, not the
    # MCP origin gate's — the middleware must not 403 it.
    resp = client.get("/open", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200
