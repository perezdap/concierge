# MCP clients — sessions and troubleshooting

Concierge exposes a Streamable HTTP MCP endpoint (default `POST /mcp`). Each
client connection gets an **`MCP-Session-Id`** during the `initialize`
handshake; that header must be sent on every follow-up RPC (`tools/list`,
`tools/call`, `ping`, etc.).

## Server enabled vs valid session

These are different states:

| What you see | Meaning |
|--------------|---------|
| Cursor `/mcp list` shows Concierge **enabled** | Transport and config are OK — the client can reach the gateway URL and auth (if any) is configured. |
| Tool calls return `-32001` with `reason: session_not_found` | The client is sending a session ID the gateway no longer holds. The server is up, but **this chat's session is stale**. |

A common pattern: Concierge restarts or idle-GC evicts the session while Cursor
keeps the old `MCP-Session-Id` from an earlier `initialize`. Tool calls fail even
though the MCP server still appears enabled.

Structured error responses include:

- `reason` — `session_not_found` or `session_missing_header`
- `recoverable` — `true` when the client should re-run `initialize`
- `operator_hint` — short recovery steps

## Recovery steps (Cursor)

When tool calls fail with `-32001 unknown or expired session`:

1. **Reload Window** — Command Palette → *Developer: Reload Window* (forces MCP
   reconnect and a fresh `initialize`).
2. **Toggle the MCP server** — disable Concierge in MCP settings, then re-enable.
3. **Start a new chat** — some clients bind session state to the conversation;
   a new chat triggers a new handshake.

If Concierge **restarted** (local process, Docker recreate, deploy), every prior
session ID is invalid regardless of client UI state — re-initialize is required.

## Session storage (operators)

| Backend | Config | Survives gateway restart? |
|---------|--------|---------------------------|
| In-memory (default) | no Redis URL | **No** — all sessions lost on process exit |
| Redis | `storage.redis_url` set | **Yes** — sessions live in Redis with TTL |

Idle sessions are also evicted by background GC when unused longer than
`session_pool.idle_ttl_s` (default 3600 s). That behaves like a stale session
from the client's perspective: re-run `initialize`.

See [`GETTING_STARTED.md`](GETTING_STARTED.md) for startup configs and
[`DEPLOYMENT.md`](DEPLOYMENT.md) for Redis-backed multi-replica setups.

## Missing session header

Non-`initialize` requests without `MCP-Session-Id` return `-32001` with
`reason: session_missing_header` and `recoverable: false`. Call `initialize`
first; the response includes the session id in the `MCP-Session-Id` header.
