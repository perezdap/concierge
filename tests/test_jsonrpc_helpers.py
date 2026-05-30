"""JSON-RPC helper utilities used by adapters."""
from __future__ import annotations

import pytest

from concierge.adapters._jsonrpc import build_notification, build_request, unwrap_result
from concierge.errors import UpstreamProtocolError


def test_build_request_has_id_and_method():
    req = build_request("tools/list", {"cursor": "x"})
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "tools/list"
    assert "id" in req
    assert req["params"] == {"cursor": "x"}


def test_build_notification_has_no_id():
    note = build_notification("notifications/initialized")
    assert "id" not in note
    assert note["method"] == "notifications/initialized"


def test_unwrap_result_success():
    assert unwrap_result({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) == {"ok": True}


def test_unwrap_result_error_raises():
    with pytest.raises(UpstreamProtocolError):
        unwrap_result({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "nope"}})
