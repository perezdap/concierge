"""Tiny JSON-RPC helpers used by every adapter."""
from __future__ import annotations

import itertools
from typing import Any

from ..errors import UpstreamProtocolError


_id_counter = itertools.count(1)


def next_request_id() -> int:
    return next(_id_counter)


def build_request(method: str, params: dict[str, Any] | None = None, *, rid: int | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid if rid is not None else next_request_id(),
        "method": method,
        "params": params or {},
    }


def build_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def unwrap_result(response: dict[str, Any]) -> Any:
    if not isinstance(response, dict):
        raise UpstreamProtocolError("response is not an object")
    if "error" in response and response["error"] is not None:
        err = response["error"]
        raise UpstreamProtocolError(
            f"upstream error {err.get('code')}: {err.get('message')}",
            data=err,
        )
    if "result" not in response:
        raise UpstreamProtocolError("response missing result")
    return response["result"]
