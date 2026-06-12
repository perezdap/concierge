"""Tiny JSON-RPC helpers used by every adapter."""
from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from ..errors import UpstreamProtocolError, UpstreamTimeout

_id_counter = itertools.count(1)


def next_request_id() -> int:
    return next(_id_counter)


def build_request(
    method: str, params: dict[str, Any] | None = None, *, rid: int | None = None
) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Shared dispatch / request helpers for stdio and sse_legacy transports.
# These extend the existing seam (build_request/build_notification/unwrap_result)
# so the two legacy adapters can share the pending-future + list_changed routing
# and the timeout/unwrap pattern without changing their I/O models.
# streamable_http.py is intentionally left alone (synchronous POST/response).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def request_future(
    pending: dict[int | str, asyncio.Future[dict[str, Any]]],
    method: str,
    params: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]]:
    """Yield (req_dict, future) and ensure the pending entry is removed on exit.

    Used by _request (stdio) and _post (sse_legacy) to deduplicate the
    "create future, stash by id, clean up on error/success" dance.
    """
    req = build_request(method, params)
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    rid: int = req["id"]
    pending[rid] = fut
    try:
        yield req, fut
    finally:
        pending.pop(rid, None)


async def wait_for_response(
    fut: asyncio.Future[dict[str, Any]],
    *,
    timeout_s: float,
    server_id: str,
    method: str,
    bump_failures: Callable[[], None] | None = None,
) -> Any:
    """Await fut with timeout; on timeout bump (if provided) then UpstreamTimeout, else unwrap."""
    try:
        response = await asyncio.wait_for(fut, timeout=timeout_s)
    except TimeoutError:
        if bump_failures:
            bump_failures()
        raise UpstreamTimeout(f"{server_id}.{method}") from None
    return unwrap_result(response)


def route_incoming_message(
    msg: Any,
    pending: dict[int | str, asyncio.Future[dict[str, Any]]],
    change_q: asyncio.Queue[str] | None = None,
) -> None:
    """Route one inbound JSON-RPC object for the legacy transports.

    - Response with id present in pending → resolve the future.
    - notifications/*/list_changed → put the kind ("tools"|"resources"|"prompts") onto change_q.
    Idempotent and safe for non-dict input (no-op).
    """
    if not isinstance(msg, dict):
        return
    if "id" in msg and msg["id"] in pending:
        pending.pop(msg["id"]).set_result(msg)
        return
    method = msg.get("method")
    if isinstance(method, str) and method.endswith("/list_changed"):
        # e.g. "notifications/tools/list_changed"
        if change_q is not None:
            parts = method.split("/")
            if len(parts) >= 2:
                kind = parts[-2]
                change_q.put_nowait(kind)
