# Approval workflow (P1-3)

Some tools are too consequential to invoke on the model's say-so. Concierge gates
them behind an **operator approval**: a dangerous / `requires_approval` call is
*parked* as a pending record in shared state and the request **pauses** until a
human grants or denies it (or the record's TTL elapses). On grant the parked call
resumes and executes upstream; on deny / expiry the client gets a structured
denial. This is the real out-of-band workflow that replaces the interim
deny-by-default and allow-list brokers (both still selectable).

Deny-by-default holds at every layer: a tool only runs after an explicit,
authenticated grant scoped to the caller's tenant.

---

## Choosing a mode

`policy.approval_mode` selects the broker for every gated tool (a tool is gated
when its catalog entry has `requires_approval`, e.g. via
`upstream_servers[].requires_approval_for`, or it is `dangerous` and
`block_dangerous_without_approval` is set):

| Mode         | Behavior                                                                 |
| ------------ | ------------------------------------------------------------------------ |
| `deny`       | Every gated tool is uninvokable. Safe MVP default; no operator console.  |
| `allow_list` | Canonical names in `approval_allow_list` are pre-approved; others denied.|
| `queue`      | **P1-3** — park the call and wait for an out-of-band operator decision.  |

```yaml
policy:
  approval_mode: queue
  block_dangerous_without_approval: true
  approval:
    backend: postgres        # memory | redis | postgres
    ttl_s: 300               # how long a parked record stays grantable
    wait_timeout_s: 300      # how long the request blocks (capped to ttl_s)
    poll_interval_s: 1.0     # cross-replica grant detection cadence
    operator_subjects: []    # see docs/AUTH.md (empty = any authed, own-tenant)
    webhooks: { ... }        # see "Webhooks" below
```

---

## Enabling the queue backend

The queue lives in **shared state** so a parked approval survives a replica
restart and is visible fleet-wide (the app tier is stateless). The backend matrix
mirrors P1-1/P1-2/P1-4:

| Backend    | Use                                                                       |
| ---------- | ------------------------------------------------------------------------- |
| `postgres` | **Preferred for production** — durable, auditable (`approvals` table).     |
| `redis`    | Acceptable when ephemeral parking is OK (records carry a TTL + grace).     |
| `memory`   | Tests / single-process dev only — lost on restart, invisible to replicas. |

```yaml
policy:
  approval:
    backend: postgres
    postgres_url: null   # falls back to storage.catalog_postgres_url when unset
    # backend: redis
    # redis_url: null    # falls back to storage.redis_url when unset
```

Postgres auto-creates an `approvals` table on first use. Redis stores one JSON
blob per record under `approval:v1:{id}` with the record TTL plus a short grace so
a lapsed record reads back as `expired` rather than vanishing.

### Resumption strategy (why bounded await)

The gateway facade dispatches `tools/call` **synchronously**, so the broker simply
`await`s the record's terminal state inside `PolicyEngine.authorize_call`, bounded
by `wait_timeout_s` (default 5 min, capped to `ttl_s`). The wait wakes instantly
when the decision is made in *this* process (an in-memory `asyncio.Event`) and
otherwise polls the shared store every `poll_interval_s` so a grant issued on a
*different* replica is still observed. If the wait elapses with no decision, the
call is denied with an `expired` reason — the client never holds the connection
open indefinitely. No client-side polling or webhook round-trip is required for
resumption; the original request returns the upstream result directly on grant.

---

## Registering operator principals

Decisions are authenticated through the P1-1 provider chain and authorized by
`policy.approval.operator_subjects` + per-decision tenant scoping. See the
**Approval operators** section of [AUTH.md](AUTH.md) for the full model. In short:

- Empty `operator_subjects` → any authenticated principal may decide, but only for
  approvals belonging to **their own tenant**.
- Non-empty → the authenticated `subject` must be listed (else `403`); decisions
  are still tenant-scoped (cross-tenant → `404`).

---

## Deciding approvals

### MCP-native discovery

A model (or operator UI speaking MCP) can list what is parked for its tenant via
the built-in primitive `gateway_list_pending_approvals` — it returns redacted
summaries only (id, tool, argument summary, expiry), never raw arguments.

### Admin HTTP path

Mirrors `/admin/tokens` + `/admin/revocations`; same auth, plus the operator
authorization above.

```bash
# List pending approvals for the operator's tenant
curl -s $GW/admin/approvals -H "Authorization: Bearer $OPERATOR"

# Grant (the parked call resumes + executes upstream)
curl -sX POST $GW/admin/approvals/grant -H "Authorization: Bearer $OPERATOR" \
  -d '{"approval_id":"ap_…"}'

# Deny (the parked call returns a structured denial)
curl -sX POST $GW/admin/approvals/deny  -H "Authorization: Bearer $OPERATOR" \
  -d '{"approval_id":"ap_…","reason":"not this tenant"}'
```

Decisions are **idempotent** — the first terminal decision wins; a later grant/deny
on the same id is a no-op returning the recorded outcome. The endpoints return
`501` when `approval_mode != "queue"`, `403` for a non-operator, and `404` for an
unknown id or a cross-tenant attempt.

### CLI

For operators without the HTTP path. Uses the same shared store + webhook config
as the running gateway:

```bash
python -m concierge approval --config gw.yaml list  [--tenant acme]
python -m concierge approval --config gw.yaml grant --id ap_… --by ops@acme [--tenant acme]
python -m concierge approval --config gw.yaml deny  --id ap_… --by ops@acme --reason "…"
```

`--tenant` enforces the same cross-tenant refusal as the HTTP path.

---

## TTL knobs

| Knob              | Meaning                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| `ttl_s`           | How long a parked record stays **grantable**. After this it is `expired`. |
| `wait_timeout_s`  | How long the gated request **blocks** awaiting a decision. Capped to `ttl_s`. |
| `poll_interval_s` | How often the waiter re-checks the shared store (cross-replica grants).   |

A small `wait_timeout_s` with a larger `ttl_s` lets the request fail fast while the
record stays decidable for late operators (a subsequent identical call can pick up
the now-granted state). The default keeps them equal at 300 s.

---

## Webhooks

On every grant / deny the gateway can POST a signed JSON callback to one or more
operator URLs (per-tenant, or a global default). Delivery never blocks or fails the
decision — the decision is already recorded authoritatively in the store.

```yaml
policy:
  approval:
    webhooks:
      tenant_urls:
        acme: ["https://acme.example/hooks/concierge"]
      default_urls: ["https://ops.example/hooks/concierge"]
      tenant_secrets:
        acme: "${ACME_WEBHOOK_SECRET}"     # never commit raw secrets
      default_secret: "${DEFAULT_WEBHOOK_SECRET}"
      max_attempts: 4        # total tries before giving up
      backoff_base_s: 0.5    # exponential: base * 2**(attempt-1)
      backoff_max_s: 8.0     # delay cap
      timeout_s: 5.0         # per-request timeout
```

A tenant resolves its URL list from `tenant_urls[tenant]` else `default_urls`, and
its signing secret from `tenant_secrets[tenant]` else `default_secret`. **A tenant
with URLs but no resolvable secret is not called** — an unsigned callback is
untrustworthy; the skip is recorded in the audit log.

### Payload + signature scheme

The body is `{"event": "approval.granted"|"approval.denied", "approval": {…public
record…}}`, serialized with sorted keys. It is signed with **HMAC-SHA256 over the
exact request body bytes** using the tenant's secret, and the signature is sent as:

```
X-Concierge-Signature: sha256=<hex>
X-Concierge-Event: approval.granted
Content-Type: application/json
```

This is the same scheme GitHub / Stripe use, so existing receiver libraries work.

### Verifying a signature (receiver side)

Compute HMAC-SHA256 over the **raw** request body with the shared secret and
compare in constant time:

```python
import hashlib, hmac

def verify(secret: str, body: bytes, header_value: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value or "")
```

The gateway's own helpers `concierge.policy.webhook.sign_payload` /
`verify_signature` implement exactly this (the latter uses `hmac.compare_digest`).

### Retry policy + failure recording

A non-2xx response or a transport error is retried with exponential backoff
(`backoff_base_s * 2**(attempt-1)`, capped at `backoff_max_s`) up to `max_attempts`
total tries. Once the cap is exhausted the gateway gives up and records an
`approval.webhook_failed` audit event (with the approval id, tenant, tool, reason,
attempt count, and last status) — it never raises into the decision path. The
signing secret is never logged or echoed.

---

## Security invariants

- **Deny-by-default** at every layer; a tool runs only after an explicit grant.
- **Authenticated, tenant-scoped decisions** — no anonymous grants; cross-tenant
  grants are refused at both the store and the endpoint.
- **Idempotent** decisions — the first terminal decision wins.
- **Redacted parking** — argument summaries are run through the audit redactor and
  length-capped before persistence; raw secrets never reach the (possibly durable)
  queue.
- **No secret bytes logged** — neither token/cert bytes (P1-1) nor webhook signing
  secrets ever appear in logs or audit records.
