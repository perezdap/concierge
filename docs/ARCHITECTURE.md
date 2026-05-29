# MCP Gateway — Architecture

Status: MVP, designed for production hardening
Audience: infra engineers operating many MCP backends

---

## 1. Goals

- Present **one** remote MCP endpoint (Streamable HTTP, single `/mcp` URL) to downstream MCP clients.
- Fan out internally to many upstream MCP servers across heterogeneous transports
  (local stdio, remote Streamable HTTP, legacy HTTP+SSE, custom).
- Maintain a **central catalog** of all upstream tools/resources/prompts,
  but expose **only a small, curated subset** in `tools/list` / `resources/list` / `prompts/list`
  per active session.
- Provide a discovery primitive so clients (and models) can browse the catalog,
  enable a subset of capabilities, and have the active surface area mutate at runtime
  via `notifications/tools/list_changed`.
- Normalize and **sanitize** upstream metadata so that an untrusted upstream
  cannot inject instructions, override descriptions, or collide on names.
- Provide clear extension points for auth, multi-tenant policy, audit,
  approval workflows, persistence, and an admin UI.

## 2. Non-goals (for MVP)

- A full identity provider. Auth is pluggable; MVP ships static bearer tokens
  + an "allow-localhost" mode.
- A persistent registry. MVP ships in-memory with an interface that allows
  swap-in of SQLite/Postgres/Redis.
- A management UI. MVP exposes `/admin/*` JSON endpoints behind the same auth.
- Tool result streaming transformations. We forward as-is.
- Multi-tenant policy. The policy engine is single-tenant aware but the
  request context carries `tenant_id` from day one so this is additive.

## 3. Core architectural decisions

| Concern | Decision | Why |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for MCP today; asyncio fits fan-out IO well; mirrors most upstream SDKs. |
| HTTP framework | FastAPI + Uvicorn | First-class async, easy SSE/Streamable HTTP, good typing, mature middleware. |
| MCP SDK | **Direct protocol implementation** for the gateway façade and adapters. | The gateway needs to (a) be a server *and* a client across multiple transports, (b) intercept and rewrite metadata, (c) selectively publish primitives. A raw JSON-RPC implementation gives precise control and avoids fighting an SDK's session model. We keep wire compatibility with MCP 2025-06-18 / Streamable HTTP. |
| Concurrency | asyncio everywhere | Adapter fan-out is IO-bound. |
| Session model | Server-generated `MCP-Session-Id`, in-memory `Session` object holds published set + per-session policy decisions. | Matches Streamable HTTP spec; easy to back with Redis later. |
| Registry storage | In-memory `Catalog` behind a `CatalogStore` interface. | Avoids DB dependency in MVP; clean extension point. |
| Notifications | Per-session async event queue drained by the open SSE/Streamable response. `notifications/tools/list_changed` emitted on publish-set mutation. | Standards-aligned, no upstream coupling. |
| Adapter interface | Single `UpstreamAdapter` ABC with normalized methods. | Lets new transports be added without touching the registry or facade. |
| Naming | `<server_id>.<sanitized_primitive>` canonical; `display_label` is a separate, sanitized human string. | Collision-proof and prompt-injection-resistant. |
| Error model | Internal `GatewayError` hierarchy mapped to JSON-RPC error codes at the façade boundary. Upstream errors are wrapped, not leaked. | Stable contract for downstream clients. |
| Retry/backoff | Per-adapter exponential backoff with jitter on connect; circuit breaker (failure ratio + cooldown) around `call_tool`. | Protects against flaky upstreams without amplifying failures. |
| Timeouts | Hard per-call timeout (default 30s, override per-tool via policy). | Bounds tail latency. |
| Servers without `list_changed` support | Periodic catalog refresh (configurable interval) + invalidate-on-error. The gateway *always* emits its own `list_changed` to downstream because publishing is gateway-controlled, not upstream-controlled. | Decouples downstream UX from upstream maturity. |
| Sanitization | All upstream-provided strings (`name`, `title`, `description`) pass through `sanitize_metadata()` before being cataloged. JSON schemas are structurally validated, not blindly forwarded. | Mitigates prompt/tool poisoning. |

## 4. Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Downstream MCP Client                       │
└───────────────────────────────▲─────────────────────────────────────┘
                                │ Streamable HTTP (POST + SSE) /mcp
┌───────────────────────────────┴─────────────────────────────────────┐
│  transport/http (FastAPI)          server/facade (JSON-RPC router)  │
│  ─ origin check, auth, session     ─ initialize / tools.list ...    │
│  ─ SSE writer                      ─ delegates to gateway services  │
├─────────────────────────────────────────────────────────────────────┤
│  core/session  core/publishing  core/catalog  policy/engine         │
│  ─ MCP-Session-Id lifecycle  ─ published set per session            │
│  ─ profiles                  ─ enable/disable + list_changed        │
├─────────────────────────────────────────────────────────────────────┤
│  adapters/manager  ─ holds connected UpstreamAdapter instances      │
│  adapters/stdio    adapters/streamable_http    adapters/sse_legacy  │
├─────────────────────────────────────────────────────────────────────┤
│  util/audit  util/redact  util/sanitize  util/ratelimit  util/log   │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. Request flow (call_tool)

1. Downstream POSTs JSON-RPC `tools/call` to `/mcp` with `MCP-Session-Id`.
2. HTTP layer: origin check, auth, session resolve.
3. Façade routes by method to `GatewayService.call_tool(session, name, args)`.
4. `PublishingService` verifies `name` is *published* and *callable* for the session.
5. `PolicyEngine.authorize_call(session, catalog_entry, args)` runs (rate limit, approval, redaction prep).
6. `AdapterManager` resolves `source_server_id` → adapter, applies circuit breaker + timeout.
7. Adapter calls upstream `tools/call` with the original upstream name.
8. Result is wrapped, audit-logged (with redaction), returned.

## 6. Session flow

```
client ──initialize──▶ gateway          (assigns MCP-Session-Id)
client ──tools/list──▶ gateway          (returns only gateway-native + any pre-published)
client ──tools/call gateway_discover_catalog──▶ gateway
                                        (compact catalog page)
client ──tools/call gateway_enable_tools──▶ gateway
                                        publishes selected entries
                                        emits notifications/tools/list_changed
client ──tools/list──▶ gateway          (now sees enabled tools)
client ──tools/call github.search_repos──▶ gateway ──▶ upstream
client ──tools/call gateway_disable_tools──▶ gateway
                                        emits notifications/tools/list_changed
```

## 7. Discovery flow

`gateway_discover_catalog` is intentionally **paginated, filtered, and compact**.
It never returns full JSON schemas — only an argument *summary*. Full schemas
become visible in `tools/list` once the tool is enabled. This keeps the model's
working context small during browsing.

## 8. Security model

| Layer | Mechanism |
|---|---|
| Client → gateway auth | Pluggable `AuthProvider`. MVP: `StaticBearerAuth` + `LocalhostAllowAuth`. |
| Origin protection | `Origin` header allowlist (DNS rebinding mitigation). |
| Bind address | Defaults to `127.0.0.1`. Public bind requires explicit `gateway.bind_public: true`. |
| Gateway → upstream auth | Per-server credentials in config, never logged, redacted in audit. |
| Per-tool policy | `risk_level` + `requires_approval` + rate limits + custom policy hooks. |
| Metadata sanitization | Strip control chars, cap length, reject names not matching `^[a-zA-Z0-9_.-]+$`, validate JSON Schemas. |
| Audit | Append-only structured log. Session, tool, decision, latency, status. |
| Rate limiting | Token bucket per (session, tool). Pluggable backend. |
| Approval | `requires_approval` tools yield an `approval_pending` resource; MVP returns `-32001 ApprovalRequired` error code; phase 2 adds out-of-band approval workflow. |

## 9. Transport compatibility model

The gateway speaks **Streamable HTTP** outward. Internally:

- **stdio**: subprocess managed by adapter; framed JSON-RPC over stdin/stdout; lifecycle = process lifecycle.
- **Streamable HTTP**: shares one `httpx.AsyncClient`, opens SSE for `list_changed`.
- **HTTP+SSE legacy**: two-URL pattern (`/sse` for read, separate POST for write); adapter bridges to the same `UpstreamAdapter` interface.
- **Custom**: implement `UpstreamAdapter`, register a `transport: "custom"` factory.

## 10. Failure handling

- Connect failure → adapter goes into `disconnected`, periodic reconnect with exp backoff; cataloged tools stay listed but are marked `callable=false`. `call_tool` returns `-32010 UpstreamUnavailable`.
- Tool call timeout → `-32011 UpstreamTimeout`, circuit breaker increments.
- Circuit open → fast-fail with `-32012 UpstreamCircuitOpen`.
- Sanitization failure during catalog refresh → entry **dropped**, logged, never surfaced.
- Notification queue overflow → coalesce `list_changed` (idempotent).

## 11. Extension points

| Future capability | Extension point |
|---|---|
| OAuth/OIDC auth | implement `AuthProvider` |
| Persistent registry | implement `CatalogStore` |
| Multi-tenant policy | `RequestContext.tenant_id` already threaded; add `TenantPolicyEngine` |
| Approval workflows | implement `ApprovalBroker`, swap into `PolicyEngine` |
| Admin UI | already JSON endpoints under `/admin/*` |
| Caching | wrap adapter `call_tool` with `CachingAdapter` decorator |
| Result-stream rewriting | `OutputFilter` chain in `GatewayService.call_tool` |

## 12. Upstream session registry

The `AdapterManager` distinguishes two roles per upstream:

- **Control plane** — one long-lived adapter per `server_id`, created at startup.
  Used for catalog refresh, `list_changed` watching, and — for `shared`
  upstreams — all tool calls. This is the original MVP behavior.
- **Data plane** — for upstreams configured `isolation: per_session`, tool
  calls are routed through a **pooled adapter keyed by
  `(server_id, router_session_id)`**, created lazily on first use and reused
  across calls within that router session.

| Concern | Behavior |
|---|---|
| Isolation mode | Per-upstream `isolation: shared` (default) or `per_session`. `shared` is best for stdio (avoids subprocess fan-out) and single-tenant; `per_session` isolates multi-tenant HTTP upstreams. |
| Lazy creation | A pooled session is built (via an adapter factory) on the first routed call for that `(upstream, router_session)`. |
| Reuse | Subsequent calls in the same router session reuse the pooled adapter (no reconnect). |
| Eviction | LRU eviction past `session_pool.max_upstream_sessions`; full teardown when the owning router session is closed (`DELETE /mcp`) or GC'd. |
| Connect resilience | `_connect_with_backoff` retries transient connect failures with exponential backoff + jitter (`connect_max_retries`, `connect_backoff_base_s`, `connect_backoff_max_s`). |
| Router-session GC | A background loop (`session_pool.gc_interval_s`) evicts router sessions idle past `session_pool.idle_ttl_s`, cascading to their pooled upstream sessions. |

The circuit breaker remains keyed by `server_id` (shared across a server's
sessions), so one failing upstream trips fast for everyone regardless of mode.

## 13. Payload optimization

To reduce tokens/bytes sent to downstream LLM clients (`util/payload.py`):

- **Slim `tools/list`** — when `payload.slim_tools_list` is enabled, published-tool
  input schemas pass through `slim_schema`: verbose, model-irrelevant keys
  (`examples`, `$comment`, …) are dropped and inline descriptions are capped
  (`payload.max_schema_description_chars`), while the structure needed to *call*
  the tool (`type`/`properties`/`required`) is preserved. The default `tools/list`
  and `/admin/catalog` surfaces keep the full (rich) schema.
  (The catalog allowlist `validate_input_schema` already strips most verbose
  keys at catalog time; the gateway slim adds a tighter, model-facing cap.)
- **Compact discovery** — `gateway_discover_catalog` omits null/empty/default
  fields per entry (no full schemas; argument summaries only).
- **Result caps** — `cap_result_text` optionally caps heavy `content[].text`
  blocks (`payload.max_result_bytes`, default `0` = disabled). Truncation is
  never silent: a marker is appended and `_meta.gateway_truncated` is set.

Discovery can also be **workflow-scoped**: `gateway_discover_catalog` accepts a
`profile` argument (restricting results to what that profile would enable), and
`gateway_list_profiles` lets a model browse available bundles before applying
one with `gateway_use_profile`.

## 14. Metrics / audit hooks

The audit stream (`util/audit.py`) carries operator-facing metrics without a new
dependency: `tool.call` records `latency_ms` plus `request_bytes`/`response_bytes`;
`upstream_session.{created,evicted,lru_evicted}` track the per-session pool; and
`session.evicted` records router-session teardown (with upstream sessions freed).
Upstream error rate is derivable from `tool.call` `ok=false` events. These are
the seams a future Prometheus/OpenTelemetry exporter plugs into.
