"""Gateway error JSON-RPC mapping."""
from __future__ import annotations

from concierge.errors import Forbidden, GatewayError, RateLimited


def test_gateway_error_to_jsonrpc_includes_data():
    err = GatewayError("boom", data={"detail": "x"})
    payload = err.to_jsonrpc()
    assert payload["code"] == -32603
    assert payload["message"] == "boom"
    assert payload["data"] == {"detail": "x"}


def test_subclass_codes():
    assert Forbidden().code == -32002
    assert RateLimited("slow").code == -32003
