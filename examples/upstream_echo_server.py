"""
Tiny MCP server over stdio for demos & tests.

Implements just enough of the MCP wire protocol to register one tool ("echo")
and respond to tools/list and tools/call. No external deps.

Run standalone:
    python examples/upstream_echo_server.py

Or have the gateway spawn it via the stdio upstream config.
"""
from __future__ import annotations

import datetime
import json
import sys
from typing import Any

TOOLS = [
    {
        "name": "echo",
        "title": "Echo",
        "description": "Returns the provided text back to the caller.",
        "inputSchema": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "description": "Text to echo back."}
            },
        },
    },
    {
        "name": "now",
        "title": "Current time",
        "description": "Returns the current ISO timestamp (UTC).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, msg: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if rid is None:
        # notification — ignore
        return None
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "echo-upstream", "version": "0.0.1"},
            "capabilities": {"tools": {}},
        })
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "resources/list":
        return _ok(rid, {"resources": []})
    if method == "prompts/list":
        return _ok(rid, {"prompts": []})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            return _ok(rid, {"content": [{"type": "text", "text": str(args.get("text", ""))}]})
        if name == "now":
            now_text = datetime.datetime.now(datetime.UTC).isoformat()
            now_text = now_text.replace("+00:00", "Z")
            return _ok(rid, {"content": [{"type": "text", "text": now_text}]})
        return _err(rid, -32601, f"unknown tool: {name}")
    if method == "ping":
        return _ok(rid, {})
    return _err(rid, -32601, f"unknown method: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
