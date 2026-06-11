"""
Gateway error hierarchy and JSON-RPC mapping.

We never leak raw upstream errors to downstream clients verbatim. Instead we
classify, wrap, and emit a stable JSON-RPC error code in the documented range.
"""
from __future__ import annotations

from typing import Any

# Reserved JSON-RPC error codes used by MCP clients per the spec.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# Gateway-specific codes (within the -32000..-32099 server error band).
GW_UNAUTHORIZED = -32001
GW_FORBIDDEN = -32002
GW_RATE_LIMITED = -32003
GW_APPROVAL_REQUIRED = -32004
GW_NOT_PUBLISHED = -32005
GW_UNKNOWN_PRIMITIVE = -32006
GW_UPSTREAM_UNAVAILABLE = -32010
GW_UPSTREAM_TIMEOUT = -32011
GW_UPSTREAM_CIRCUIT_OPEN = -32012
GW_UPSTREAM_PROTOCOL = -32013
GW_SANITIZATION_FAILED = -32020

SESSION_OPERATOR_HINT = (
    "Re-run MCP initialize: reload Cursor window, toggle MCP server, or start a "
    "new chat. If Concierge restarted, all prior session IDs are invalid."
)
SESSION_MISSING_HEADER_HINT = (
    "Call MCP initialize first; the response includes MCP-Session-Id for "
    "subsequent requests."
)

_SESSION_DEFAULT_MESSAGES = {
    "session_not_found": "unknown or expired session",
    "session_missing_header": "missing MCP-Session-Id",
}


def session_error_data(reason: str) -> dict[str, Any]:
    """Structured JSON-RPC ``error.data`` for stale or missing MCP sessions."""
    recoverable = reason == "session_not_found"
    return {
        "reason": reason,
        "recoverable": recoverable,
        "operator_hint": SESSION_OPERATOR_HINT if recoverable else SESSION_MISSING_HEADER_HINT,
    }


class GatewayError(Exception):
    code: int = JSONRPC_INTERNAL_ERROR
    message: str = "internal error"

    def __init__(self, message: str | None = None, data: Any = None) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.data = data

    def to_jsonrpc(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


class Unauthorized(GatewayError):
    code = GW_UNAUTHORIZED
    message = "unauthorized"


def session_unauthorized(*, reason: str, message: str | None = None) -> Unauthorized:
    """Build ``Unauthorized`` (-32001) with structured session recovery metadata."""
    return Unauthorized(
        message or _SESSION_DEFAULT_MESSAGES[reason],
        data=session_error_data(reason),
    )


class Forbidden(GatewayError):
    code = GW_FORBIDDEN
    message = "forbidden"


class RateLimited(GatewayError):
    code = GW_RATE_LIMITED
    message = "rate limited"

    @classmethod
    def with_retry_after(cls, message: str, retry_after_s: float) -> RateLimited:
        """Build a 429-equivalent error carrying ``Retry-After`` in ``data``.

        ``retry_after_s`` is rounded *up* to whole seconds (HTTP ``Retry-After``
        is integer seconds) so a client that waits exactly that long is past the
        refill threshold. An infinite/never-refilling bucket is reported as a
        large finite backoff rather than a non-serializable ``inf``.
        """
        import math

        if retry_after_s == float("inf"):
            seconds = 86400
        else:
            seconds = max(1, math.ceil(retry_after_s))
        return cls(message, data={"retry_after": seconds})


class ApprovalRequired(GatewayError):
    code = GW_APPROVAL_REQUIRED
    message = "approval required"


class NotPublished(GatewayError):
    code = GW_NOT_PUBLISHED
    message = "primitive not published in this session"


class UnknownPrimitive(GatewayError):
    code = GW_UNKNOWN_PRIMITIVE
    message = "unknown primitive"


class InvalidParams(GatewayError):
    code = JSONRPC_INVALID_PARAMS
    message = "invalid params"


class MethodNotFound(GatewayError):
    code = JSONRPC_METHOD_NOT_FOUND
    message = "method not found"


class UpstreamUnavailable(GatewayError):
    code = GW_UPSTREAM_UNAVAILABLE
    message = "upstream unavailable"


class UpstreamTimeout(GatewayError):
    code = GW_UPSTREAM_TIMEOUT
    message = "upstream timeout"


class UpstreamCircuitOpen(GatewayError):
    code = GW_UPSTREAM_CIRCUIT_OPEN
    message = "upstream circuit open"


class UpstreamProtocolError(GatewayError):
    code = GW_UPSTREAM_PROTOCOL
    message = "upstream protocol error"


class SanitizationFailed(GatewayError):
    code = GW_SANITIZATION_FAILED
    message = "metadata sanitization failed"
