"""
Origin allow-list enforcement for the gateway's HTTP surface (P0-1).

This module is the single source of truth for Origin validation. Both the
request handlers and the global :class:`OriginAllowlistMiddleware` use these
helpers, so the allow-list policy is defined exactly once.

Threat model: a malicious web page loaded in the user's browser can try to
reach a locally-bound MCP gateway (a DNS-rebinding / CSRF-style attack). The
browser attaches an ``Origin`` header it cannot forge. Enforcing a strict,
exact-match Origin allow-list on *every* downstream route prevents an untrusted
browser origin from driving the gateway even when it can reach the socket.

Policy (explicit, see :func:`_origin_allowed`):
  * Missing ``Origin``: allowed. Non-browser MCP clients (CLIs, LLM runtimes,
    many SDKs) routinely omit it; only browsers are forced to send it.
  * ``Origin: "null"`` (the literal string): allowed only if ``"null"`` is
    explicitly listed. It comes from sandboxed iframes, ``data:`` URLs and some
    privacy modes — hostile by default.
  * Any other present ``Origin``: must match a listed origin exactly after
    normalising scheme + host + port. No ``startswith`` / prefix matching, so
    ``http://localhost.evil.com`` never matches ``http://localhost``.
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


def _normalize_origin(value: str | None) -> str | None:
    """Return a canonical ``scheme://host[:port]`` string for exact matching.

    Default ports (80/443) are dropped so ``http://h:80`` and ``http://h``
    compare equal. Returns ``"null"`` for the literal ``"null"`` origin, and
    ``None`` for empty or unparseable values.
    """
    if not value:
        return None
    if value == "null":
        return "null"
    try:
        p = urlparse(value)
        if not p.scheme or not p.hostname:
            return None
        port = p.port
        if port is None:
            if p.scheme == "https":
                port = 443
            elif p.scheme == "http":
                port = 80
        if port in (80, 443):
            return f"{p.scheme}://{p.hostname}"
        return f"{p.scheme}://{p.hostname}:{port}"
    except Exception:  # noqa: BLE001 — any parse failure is a rejection
        return None


def _origin_header_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    """Core allow-list check against a raw ``Origin`` header value.

    See the module docstring for the policy. Used by both the ``Request``
    convenience wrapper and the ASGI middleware.
    """
    if origin is None:
        return True

    normalized_incoming = _normalize_origin(origin)
    if normalized_incoming is None:
        return False

    allowed_normalized = {_normalize_origin(a) for a in allowed if a}
    return normalized_incoming in allowed_normalized


def _origin_allowed(request: Request, allowed: list[str]) -> bool:
    """Exact Origin allow-list check for a Starlette/FastAPI ``Request``."""
    return _origin_header_allowed(request.headers.get("Origin"), allowed)


def _header_value(scope: Scope, name: bytes) -> str | None:
    """Read a single header (lower-cased name) from a raw ASGI scope."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _send_403(send: Send) -> None:
    body = b'{"detail":"origin not allowed"}'
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class OriginAllowlistMiddleware:
    """Pure-ASGI middleware that 403s requests with a disallowed ``Origin``.

    It is deliberately *pure ASGI* rather than ``BaseHTTPMiddleware`` so it never
    buffers the long-lived SSE response served by ``GET /mcp``; it inspects the
    request before delegating and rejects early on a bad Origin.

    Only ``guarded_paths`` are gated — typically the configured MCP endpoint.
    Because the check runs before the route handler, it covers every method on
    that path (POST/GET/DELETE), guaranteeing no handler is reached with an
    untrusted browser Origin.

    Args:
        app: the wrapped ASGI application.
        allowed_origins: exact origins permitted (normalised on each check).
        guarded_paths: request paths to enforce the gate on (exact match).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: list[str],
        guarded_paths: Iterable[str],
    ) -> None:
        self.app = app
        self._allowed = [o for o in allowed_origins if o]
        self._guarded = frozenset(guarded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self._guarded:
            await self.app(scope, receive, send)
            return
        origin = _header_value(scope, b"origin")
        if not _origin_header_allowed(origin, self._allowed):
            await _send_403(send)
            return
        await self.app(scope, receive, send)
