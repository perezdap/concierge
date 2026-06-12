"""
Streamable HTTP transport facade.

Implements the downstream-facing wire protocol (P0-4 conformance updates applied):

 * `POST /mcp` — JSON-RPC request or notification.
   - Requests (with id): returns 200 + JSON body (single result or array for batch).
   - Pure notifications (no id): returns 202 Accepted with empty body per spec.
   - Never returns an event-stream body from POST (use the GET path below for SSE).
 * `GET /mcp` — opens a long-lived SSE stream that drains the session's
   notification queue (e.g. notifications/tools/list_changed).
 * `DELETE /mcp` — closes the session.

Sessions are identified by the `MCP-Session-Id` header. Brand-new clients
issue an `initialize` request without that header; we mint a session id and
return it on the response.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.notifications import NotificationBus
from ..core.session import SessionManager
from ..core.types import JsonRpcRequest, Session
from ..errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_REQUEST,
    JSONRPC_PARSE_ERROR,
    GatewayError,
    Unauthorized,
    session_error_data,
    session_unauthorized,
)
from ..gateway.service import SUPPORTED_PROTOCOL_VERSIONS, GatewayService
from ..util.log import get_logger
from .auth import AuthProvider
from .lifecycle import DrainController

_log = get_logger("concierge.facade")


def _unsupported_protocol_version(value: str | None) -> bool:
    """True when an MCP-Protocol-Version header is present but not supported.

    Absent header → False (permissive: older clients and many non-browser MCP
    SDKs omit it; per spec the server assumes a compatible baseline). A present
    but unknown version → True, so callers can reject it with a clear error.
    """
    return value is not None and value not in SUPPORTED_PROTOCOL_VERSIONS


def _jsonrpc_error_response(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _jsonrpc_ok_response(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def build_facade_router(
    *,
    service: GatewayService,
    sessions: SessionManager,
    bus: NotificationBus,
    auth: AuthProvider,
    path: str = "/mcp",
    drain: DrainController | None = None,
) -> APIRouter:
    router = APIRouter()

    async def _resolve_session(
        request: Request,
        session_header: str | None,
        method: str,
        *,
        allow_create: bool,
    ) -> Session:
        # initialize is the only call where session_id may be absent.
        if session_header:
            s = await sessions.get(session_header)
            if s is None:
                raise session_unauthorized(reason="session_not_found")
            return s
        if allow_create and method == "initialize":
            auth_res = await auth.authenticate(request)
            return await sessions.create(
                tenant_id=auth_res.tenant_id, auth_subject=auth_res.subject
            )
        raise session_unauthorized(reason="session_missing_header")

    @router.post(path)
    async def handle_post(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="MCP-Session-Id"),
    ) -> Response:
        # Graceful drain (P1-7): once the process is draining for a rolling
        # restart, refuse to mint brand-new sessions so the client reconnects to
        # a healthy replica. Requests carrying an existing MCP-Session-Id are
        # still served to completion. New sessions arrive without the header.
        if drain is not None and drain.draining and mcp_session_id is None:
            return JSONResponse(
                _jsonrpc_error_response(
                    None,
                    JSONRPC_INTERNAL_ERROR,
                    "gateway is draining for shutdown; retry on another instance",
                ),
                status_code=503,
                headers={"Retry-After": "1"},
            )

        # Extract transport-level protocol version header. On an established
        # session (post-initialize) a present-but-unsupported version is a hard
        # error per the MCP Streamable HTTP spec. The initialize handshake itself
        # negotiates via the body's `protocolVersion`, so we only gate follow-up
        # requests (those carrying an MCP-Session-Id).
        mcp_protocol_version = request.headers.get("MCP-Protocol-Version")
        if mcp_session_id is not None and _unsupported_protocol_version(mcp_protocol_version):
            return JSONResponse(
                _jsonrpc_error_response(
                    None,
                    JSONRPC_INVALID_REQUEST,
                    f"unsupported MCP-Protocol-Version {mcp_protocol_version!r}; "
                    f"supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
                ),
                status_code=400,
            )

        # Existing sessions still need auth checked on every request.
        if mcp_session_id is None:
            pass  # auth happens inside _resolve_session for initialize
        else:
            try:
                await auth.authenticate(request)
            except Unauthorized as e:
                return JSONResponse(
                    _jsonrpc_error_response(None, e.code, e.message, e.data),
                    status_code=401,
                )

        try:
            payload = await request.json()
        except json.JSONDecodeError as e:
            return JSONResponse(
                _jsonrpc_error_response(None, JSONRPC_PARSE_ERROR, f"parse error: {e}"),
                status_code=400,
            )

        # Support batched requests transparently.
        is_batch = isinstance(payload, list)
        items = payload if is_batch else [payload]
        responses: list[dict[str, Any]] = []
        new_session_id: str | None = None

        # Count this POST as in-flight for the duration of dispatch so a
        # concurrent SIGTERM drain waits for it to finish (P1-7). Released in the
        # finally below even on error.
        if drain is not None:
            await drain.acquire()
        try:
            for item in items:
                try:
                    rpc = JsonRpcRequest.model_validate(item)
                except Exception as e:  # noqa: BLE001
                    responses.append(_jsonrpc_error_response(
                        (item or {}).get("id") if isinstance(item, dict) else None,
                        JSONRPC_PARSE_ERROR, f"invalid JSON-RPC: {e}",
                    ))
                    continue

                # Notifications (no id) — fire-and-forget. We accept and ignore the
                # canonical notifications/initialized message here.
                if rpc.id is None:
                    _log.debug("received notification %s", rpc.method)
                    continue

                try:
                    session = await _resolve_session(
                        request, mcp_session_id, rpc.method,
                        allow_create=(mcp_session_id is None),
                    )
                    if mcp_session_id is None and rpc.method == "initialize":
                        new_session_id = session.session_id

                    result = await service.dispatch(
                        session, rpc.method,
                        rpc.params if isinstance(rpc.params, dict) else None,
                    )
                    responses.append(_jsonrpc_ok_response(rpc.id, result))
                except GatewayError as e:
                    responses.append(_jsonrpc_error_response(rpc.id, e.code, e.message, e.data))
                except Exception as e:  # noqa: BLE001
                    _log.exception("dispatch error")
                    responses.append(
                        _jsonrpc_error_response(rpc.id, JSONRPC_INTERNAL_ERROR, str(e))
                    )
        finally:
            if drain is not None:
                await drain.release()

        # Pick response shape.
        # Per MCP Streamable HTTP + P0-4: pure notification POSTs (no id fields)
        # must return HTTP 202 Accepted with empty body. Requests (with id) return
        # 200 + JSON (single object or array for batch).
        headers: dict[str, str] = {}
        if new_session_id:
            headers["MCP-Session-Id"] = new_session_id

        if not responses:
            # No JSON-RPC responses to return → this POST contained only notifications.
            # Return 202 Accepted, empty body (no content).
            return Response(status_code=202, headers=headers)

        body = responses if is_batch else responses[0]
        # Streaming response is allowed by Streamable HTTP. For initialize and
        # other small RPCs we just return JSON; this keeps clients that don't
        # implement SSE happy.
        return JSONResponse(body, headers=headers)

    @router.get(path)
    async def handle_get(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="MCP-Session-Id"),
    ) -> Response:
        if _unsupported_protocol_version(request.headers.get("MCP-Protocol-Version")):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported MCP-Protocol-Version; "
                    f"supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
                ),
            )
        try:
            await auth.authenticate(request)
        except Unauthorized as e:
            raise HTTPException(status_code=401, detail=e.message)
        if not mcp_session_id:
            raise HTTPException(status_code=400, detail="MCP-Session-Id required")
        session = await sessions.get(mcp_session_id)
        if session is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "unknown session",
                    **session_error_data("session_not_found"),
                },
            )

        queue = bus.queue_for(session.session_id)

        async def event_stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except TimeoutError:
                        # Keepalive comment — keeps proxies from closing.
                        yield b": keepalive\n\n"
                        continue
                    payload = json.dumps(msg, ensure_ascii=False)
                    yield f"event: message\ndata: {payload}\n\n".encode()
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.delete(path)
    async def handle_delete(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="MCP-Session-Id"),
    ) -> Response:
        if not mcp_session_id:
            raise HTTPException(status_code=400, detail="MCP-Session-Id required")
        try:
            await auth.authenticate(request)
        except Unauthorized as e:
            raise HTTPException(status_code=401, detail=e.message)
        await sessions.aclose(mcp_session_id)
        bus.drop(mcp_session_id)
        return Response(status_code=204)

    return router
