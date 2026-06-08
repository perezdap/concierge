"""
End-to-end client demo against the gateway.

Walks through:

    1. initialize
    2. tools/list (small — only gateway-native primitives)
    3. tools/call gateway_discover_catalog
    4. tools/call gateway_enable_tools
    5. (optional) listen for notifications/tools/list_changed on GET /mcp
    6. tools/list (now shows the enabled echo__echo)
    7. tools/call echo__echo
    8. tools/call gateway_disable_tools

Run the gateway first:
    python -m concierge --config config/gateway.example.yaml

Then in another terminal:
    python examples/session_flow.py
"""
from __future__ import annotations

import asyncio
import json

import httpx

BASE_URL = "http://127.0.0.1:8765/mcp"


async def rpc(
    client: httpx.AsyncClient,
    method: str,
    params: dict | None = None,
    session_id: str | None = None,
):
    headers = {"Accept": "application/json"}
    if session_id:
        headers["MCP-Session-Id"] = session_id
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = await client.post(BASE_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return resp, resp.json()


async def listen_one(client: httpx.AsyncClient, session_id: str, timeout: float = 3.0):
    """Drain one notification from the SSE stream (best-effort)."""
    headers = {"MCP-Session-Id": session_id, "Accept": "text/event-stream"}
    try:
        async with client.stream("GET", BASE_URL, headers=headers, timeout=timeout) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
        return None


async def main() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. initialize — no session id yet; server assigns one
        resp, body = await rpc(client, "initialize", {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "demo-client", "version": "0.1"},
            "capabilities": {},
        })
        session_id = resp.headers["MCP-Session-Id"]
        print("session_id:", session_id)
        print("initialize result:", json.dumps(body["result"], indent=2)[:300])

        # 2. tools/list — only gateway-native primitives
        _, body = await rpc(client, "tools/list", session_id=session_id)
        names = [t["name"] for t in body["result"]["tools"]]
        print("\ninitial tools/list:", names)

        # 3. discover catalog
        _, body = await rpc(client, "tools/call", {
            "name": "gateway_discover_catalog",
            "arguments": {"query": "echo"},
        }, session_id=session_id)
        print("\ndiscovery result:", json.dumps(body["result"]["structuredContent"], indent=2))

        catalog_names = [e["name"] for e in body["result"]["structuredContent"]["entries"]]
        if not catalog_names:
            print("no upstream tools cataloged yet — is the echo server running?")
            return

        # 4. enable tools
        _, body = await rpc(client, "tools/call", {
            "name": "gateway_enable_tools",
            "arguments": {"names": catalog_names},
        }, session_id=session_id)
        print("\nenable result:", body["result"]["structuredContent"])

        # 5. drain one notification (list_changed)
        # NOTE: this is best-effort because the notification may have already fired
        # before we open the GET stream — but we still confirm SSE works.
        async def _listen():
            return await listen_one(client, session_id, timeout=2.0)
        listener = asyncio.create_task(_listen())

        # 6. tools/list now includes echo__echo
        _, body = await rpc(client, "tools/list", session_id=session_id)
        names = [t["name"] for t in body["result"]["tools"]]
        print("\npost-enable tools/list:", names)

        # 7. invoke the upstream tool through the gateway
        _, body = await rpc(client, "tools/call", {
            "name": "echo__echo",
            "arguments": {"text": "hello from gateway"},
        }, session_id=session_id)
        print("\necho__echo result:", body["result"])

        # 8. disable everything
        _, body = await rpc(client, "tools/call", {
            "name": "gateway_disable_tools",
            "arguments": {"all": True},
        }, session_id=session_id)
        print("\ndisable result:", body["result"]["structuredContent"])

        notif = await listener
        print("\nfirst SSE notification (if any):", notif)


if __name__ == "__main__":
    asyncio.run(main())
