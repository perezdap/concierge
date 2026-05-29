"""
Streamable HTTP transport facade.

Implements the downstream-facing wire protocol:

 * `POST /mcp` — JSON-RPC request, returns either a JSON body (single response)
   or a `text/event-stream` body containing the JSON response followed by any
   queued notifications.
 * `GET /mcp` — opens an SSE stream that drains the session's notification
   queue (e.g. notifications/tools/list_changed).
 * `DELETE /mcp` — closes the session.

Sessions are identified by the `MCP-Session-Id` header. Brand-new clients
issue an `initialize` request without that header; we mint a session id and
return it on the response.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.notifications import NotificationBus
from ..core.session import SessionManager
from ..core.types import JsonRpcRequest, Session
from ..errors import (
    GatewayError,
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_PARSE_ERROR,
    Unauthorized,
)
from ..gateway.service import GatewayService
from ..util.log import get_logger
from .auth import AuthProvider

_log = get_logger("concierge.facade")


def _origin_allowed(request: Request, allowed: list[str]) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        # Non-browser clients usually omit Origin — only enforce when present.
        return True
    return any(origin == a or origin.startswith(a) for a in allowed)


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
    allowed_origins: list[str],
    path: str = "/mcp",
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
            s = sessions.get(session_header)
            if s is None:
                raise Unauthorized("unknown or expired session")
            return s
        if allow_create and method == "initialize":
            auth_res = await auth.authenticate(request)
            return await sessions.create(tenant_id=auth_res.tenant_id, auth_subject=auth_res.subject)
        raise Unauthorized("missing MCP-Session-Id")

    @router.post(path)
    async def handle_post(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="MCP-Session-Id"),
    ) -> Response:
        if not _origin_allowed(request, allowed_origins):
            raise HTTPException(status_code=403, detail="origin not allowed")

        # Existing sessions still need auth checked on every request.
        if mcp_session_id is None:
            pass  # auth happens inside _resolve_session for initialize
        else:
            try:
                await auth.authenticate(request)
            except Unauthorized as e:
                return JSONResponse(
                    _jsonrpc_error_response(None, e.code, e.message),
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
                    request, mcp_session_id, rpc.method, allow_create=(mcp_session_id is None),
                )
                if mcp_session_id is None and rpc.method == "initialize":
                    new_session_id = session.session_id

                result = await service.dispatch(session, rpc.method, rpc.params if isinstance(rpc.params, dict) else None)
                responses.append(_jsonrpc_ok_response(rpc.id, result))
            except GatewayError as e:
                responses.append(_jsonrpc_error_response(rpc.id, e.code, e.message, e.data))
            except Exception as e:  # noqa: BLE001
                _log.exception("dispatch error")
                responses.append(_jsonrpc_error_response(rpc.id, JSONRPC_INTERNAL_ERROR, str(e)))

        # Pick response shape
        body = responses if is_batch else (responses[0] if responses else None)
        headers: dict[str, str] = {}
        if new_session_id:
            headers["MCP-Session-Id"] = new_session_id

        # Streaming response is allowed by Streamable HTTP. For initialize and
        # other small RPCs we just return JSON; this keeps clients that don't
        # implement SSE happy.
        return JSONResponse(body, headers=headers)

    @router.get(path)
    async def handle_get(
        request: Request,
        mcp_session_id: str | None = Header(default=None, alias="MCP-Session-Id"),
    ) -> Response:
        if not _origin_allowed(request, allowed_origins):
            raise HTTPException(status_code=403, detail="origin not allowed")
        try:
            await auth.authenticate(request)
        except Unauthorized as e:
            raise HTTPException(status_code=401, detail=e.message)
        if not mcp_session_id:
            raise HTTPException(status_code=400, detail="MCP-Session-Id required")
        session = sessions.get(mcp_session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")

        queue = bus.queue_for(session.session_id)

        async def event_stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        # Keepalive comment — keeps proxies from closing.
                        yield b": keepalive\n\n"
                        continue
                    payload = json.dumps(msg, ensure_ascii=False)
                    yield f"event: message\ndata: {payload}\n\n".encode("utf-8")
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
