# Roadmap

The MVP delivers an end-to-end working gateway with discovery, publishing,
sanitization, policy hooks, audit, and three upstream transports. The
roadmap below sequences the work needed to take it from "lab" to "production",
ordered by dependency.

## Phase 2 — production hardening

1. **Auth providers**
   - OAuth2 / OIDC `AuthProvider` (uses any IdP that does `id_token` verification).
   - mTLS pass-through provider for service-to-service deployments.
   - Per-tenant token issuance + revocation list.

2. **Persistent registry**
   - `SQLiteCatalogStore` and `PostgresCatalogStore` implementing `CatalogStore`.
   - Migration runner.
   - Optional Redis-backed session store (drop-in for `SessionManager`) so the
     gateway can be horizontally scaled behind a load balancer.

3. **Approval workflow**
   - Real `ApprovalBroker` backed by a queue and an operator console.
   - Pending-approval resources surfaced as MCP resources so clients can poll.
   - Webhook callbacks for approval grant/deny.

4. **Rate-limiting**
   - Distributed token bucket (Redis) keyed by (tenant, session, tool).
   - Per-tenant quotas, leaky-bucket option.
   - 429-equivalent error with `Retry-After`.

5. **Result filtering**
   - `OutputFilter` chain (PII scrubbing, secrets detection, optional schema
     coercion).
   - Length caps and content-type allow lists.

6. **Caching**
   - `CachingAdapter` decorator for idempotent read tools.
   - Per-tool TTL configuration.

## Phase 3 — multi-tenant + enterprise

1. **Multi-tenant policy engine**
   - `TenantPolicyEngine` that loads ABAC rules per tenant.
   - Per-tenant catalog views (so tenant A only sees a subset of upstreams).

2. **Admin UI**
   - Live catalog browser, session inspector, audit search, approval review.
   - Builds on the existing `/admin/*` JSON endpoints.

3. **Observability**
   - OpenTelemetry traces across gateway → adapter → upstream.
   - Prometheus metrics endpoint (`/metrics`).
   - Audit sink interface (Kafka, S3, OpenSearch).

4. **Tool poisoning defense in depth**
   - Cross-source duplicate detection (same name from two servers → quarantine).
   - Heuristic instruction-leak detector on upstream descriptions.
   - Operator-defined description overrides ("if the upstream description
     matches this regex, use my description instead").

5. **High availability**
   - Health-aware adapter failover (primary/replica config per upstream).
   - Graceful drain on SIGTERM.
   - Configurable session affinity for sticky-routed deployments.

6. **Resource & prompt parity**
   - Streaming resource passthrough.
   - `notifications/resources/updated` propagation.
   - Prompt argument validation at the gateway boundary.
