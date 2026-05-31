# Authentication (P1-1)

Concierge authenticates every MCP request through a pluggable provider model.
Each provider implements one credential shape; a **provider chain** tries them in
order and the **first** whose credential is present wins. If none match, the
request is denied — there is no implicit "allow" fallthrough.

Every successful authentication yields:

- `subject` — an opaque, salted audit id (never the raw token / cert / `sub`).
- `tenant_id` — propagated onto the session and used as the rate-limit bucket key
  `(tenant, session, tool)` (P1-4).
- `token_id` — the revocation key (`jti` for OIDC, an opaque `tt_…` / `token:…`
  id otherwise), checked against the shared revocation list on **every** request.

Security invariants (shared with P0-2): deny-by-default, constant-time secret
comparison (`hmac.compare_digest`), exact matching (never prefix/substring), and
raw secrets are never logged — only opaque ids or salted SHA-256 digests.

---

## Backwards compatibility

The legacy single-provider config is unchanged. With `providers` empty, `type`
selects the provider exactly as before:

```yaml
auth:
  type: bearer            # none | bearer | localhost
  bearer_tokens: ["${GATEWAY_TOKEN}"]
```

`bearer` and `localhost` are now wrapped in revocation enforcement automatically,
so a revoked `token_id` is rejected even on the legacy path. `none` stays bare.

---

## Enabling the provider chain

Set `providers` to a non-empty, ordered list to opt into the P1-1 chain. The
named sub-configs (`oidc`, `mtls`, `tenant_token`) are only consulted when their
provider is listed.

```yaml
auth:
  providers: [mtls, oidc, tenant_token, bearer]   # order = match priority
  bearer_tokens: ["${ADMIN_FALLBACK_TOKEN}"]      # used by `bearer`
  oidc: { ... }                                   # see below
  mtls: { ... }                                   # see below
  revocation:   { backend: redis }                # shared in prod
  tenant_token: { backend: redis }
```

Matching is by credential **shape**, not by trying each provider's full
verification:

| Provider       | Matches when…                                              |
| -------------- | ---------------------------------------------------------- |
| `mtls`         | the configured forwarded client-cert header is present     |
| `oidc`         | a 3-part `Bearer` JWT is present                           |
| `tenant_token` | any `Bearer` token is present                              |
| `bearer`       | any `Bearer` token is present                              |
| `localhost`    | always (gate is the peer IP)                               |

Because `tenant_token` and `bearer` both match any bearer, order them so the more
specific store (`tenant_token`) is tried first; the static `bearer` is the
fallback.

---

## OIDC / OAuth2 (`oidc`)

Verifies an id_token or JWT access token from **any** standards-compliant IdP.
Signing keys are discovered from `{issuer}/.well-known/openid-configuration` and
the JWKS, cached for `jwks_ttl_s` with automatic refresh when an unknown `kid`
appears (key rotation). `iss`, `aud`, `exp`, `nbf`, and `iat` are verified, with
`leeway_s` tolerating clock skew. Only asymmetric algorithms (RS256/ES256…) are
accepted. id_token bytes are never logged.

```yaml
auth:
  providers: [oidc]
  oidc:
    issuer: "https://login.example.com/"     # discovery root
    audiences: ["concierge-api"]              # exact match, ≥1 required
    allowed_issuers: []                       # defaults to [issuer]; add more to allow several
    discovery_url: null                       # override if non-standard
    jwks_uri: null                            # override to skip discovery
    jwks_ttl_s: 3600
    leeway_s: 60
    tenant_claim: "tenant"                    # claim → tenant_id
    default_tenant: "default"                 # used when the claim is absent
    http_timeout_s: 5.0
```

Requires the `auth` extra (`pyjwt[crypto]`): `uv sync --extra auth` (or it is
already present in the published image / `requirements.txt`).

---

## mTLS pass-through (`mtls`)

For service-to-service calls. **TLS and client-cert validation happen at the
reverse proxy / ingress, not in the gateway.** The gateway reads the
already-validated cert identity from a forwarded header.

> **Trusted-proxy assumption (read this).** Forwarded headers are trivially
> forgeable by anything that can reach the gateway directly. The provider honors
> the cert header **only** when the request's immediate peer IP falls inside one
> of `trusted_proxy_cidrs`; otherwise it is rejected with `untrusted source`. An
> empty CIDR list trusts nobody. You **must** therefore ensure the gateway is
> only reachable from the proxy (network policy / loopback), and that the proxy
> strips any client-supplied copy of these headers before re-adding its own.

```yaml
auth:
  providers: [mtls]
  mtls:
    trusted_proxy_cidrs: ["10.0.0.0/8"]       # empty = trust nobody
    subject_header: "x-forwarded-client-cert" # Envoy XFCC or nginx ssl-client-s-dn
    verify_header: "ssl-client-verify"        # must equal SUCCESS when set; null to skip
    subject_tenant_map:                        # cert subject/SAN → tenant_id
      "CN=svc-a": "tenant-a"
      "spiffe://cluster/ns/payments": "payments"
    default_tenant: "default"
```

Both the nginx subject-DN form (`CN=svc-a,O=...`) and the Envoy XFCC form
(`Subject="CN=svc-a";URI=spiffe://...;Hash=...`) are parsed; the SAN URI is
preferred for the tenant lookup when present.

---

## Per-tenant tokens + revocation

`tenant_token` resolves gateway-minted opaque tokens to a tenant. The store keeps
a salted SHA-256 digest, never the raw secret — the secret is shown exactly once
at mint time. Mint / rotate / revoke via the admin HTTP path or the CLI.

### Storage backends

Both the revocation list and the tenant-token store use the same backend matrix
and **must be shared in production** (stateless app tier — local memory does not
survive a replica restart and is invisible fleet-wide):

```yaml
auth:
  revocation:   { backend: redis }     # memory | redis | postgres
  tenant_token: { backend: redis }
```

`redis_url` / `postgres_url` may be set per-store; when unset they fall back to
`storage.redis_url` / `storage.catalog_postgres_url`. `memory` is for tests/dev
only.

### Admin HTTP path

Authenticated by any credential the chain accepts (the admin path reuses the same
provider). Mint returns the raw token **once**:

```bash
# Mint
curl -sX POST $GW/admin/tokens -H "Authorization: Bearer $ADMIN" \
  -d '{"tenant_id":"acme"}'
# → {"token_id":"tt_…","tenant_id":"acme","token":"<raw secret, shown once>"}

# Rotate (mints new, revokes the old id)
curl -sX POST $GW/admin/tokens/rotate -H "Authorization: Bearer $ADMIN" \
  -d '{"tenant_id":"acme","old_token_id":"tt_…"}'

# List (ids only, never secrets)
curl -s $GW/admin/tokens -H "Authorization: Bearer $ADMIN"

# Revoke any token id (static, tenant, or OIDC jti)
curl -sX POST $GW/admin/revocations -H "Authorization: Bearer $ADMIN" \
  -d '{"token_id":"tt_…"}'

# List / clear revocations
curl -s  $GW/admin/revocations        -H "Authorization: Bearer $ADMIN"
curl -sX DELETE $GW/admin/revocations/tt_… -H "Authorization: Bearer $ADMIN"
```

If the relevant store is not configured the endpoint returns `501`.

### CLI

For operators without the HTTP path. Uses the same stores as the running gateway:

```bash
python -m concierge token --config gw.yaml mint   --tenant acme
python -m concierge token --config gw.yaml rotate --tenant acme --old tt_…
python -m concierge token --config gw.yaml list
python -m concierge token --config gw.yaml revoke --id tt_…   # or an OIDC jti
python -m concierge token --config gw.yaml revocations
```

---

## Revoking a token

Revocation is keyed by `token_id` and enforced on **every** auth check across all
providers:

- **Tenant / static token** — revoke its `token_id` (the `tt_…` / `token:…` id).
  Rotation revokes the old id for you.
- **OIDC** — revoke the token's `jti` claim. The IdP keeps issuing until the
  token expires, so this is the gateway-side kill switch between expiry windows.

A revoked id is rejected immediately on the next request; with a shared backend
the revocation is visible to every replica.

---

## Approval operators (P1-3)

The out-of-band approval queue (`policy.approval_mode: queue`, see
[APPROVALS.md](APPROVALS.md)) lets a human grant or deny a parked tool call. Those
decisions are **authenticated through this same provider chain** — there is no
anonymous decision path. The operator-decision model:

1. **Authenticate.** The caller must present a credential the chain accepts
   (`/admin/approvals*` reuses the gateway `AuthProvider`). This yields the
   operator's `subject` and `tenant_id`.
2. **Authorize.** If `policy.approval.operator_subjects` is non-empty, the
   authenticated `subject` must appear in that allow-list; otherwise the caller is
   rejected with `403 not an approval operator`. An **empty** list means any
   authenticated principal may decide — but still only for their own tenant.
3. **Tenant scope (always enforced).** A decision only applies to an approval
   whose `tenant_id` equals the operator's `tenant_id`. A cross-tenant grant is
   impossible: the store refuses it (returns `None`) and the admin endpoint maps
   that to `404 unknown approval for this tenant`. This holds regardless of the
   allow-list.

```yaml
policy:
  approval_mode: queue
  approval:
    # Empty = any authenticated subject may decide approvals for their own tenant.
    # Non-empty = only these audit subjects may decide (still tenant-scoped).
    operator_subjects: ["oidc:9f3c…", "tenant-token:tt_ops…"]
```

`subject` values are the opaque, salted audit ids described above (e.g.
`oidc:<hash>`, `tenant-token:<id>`, `token:<id>`) — list the exact subject a given
operator authenticates as. The decision is recorded on the approval record as
`decided_by=<subject>` and emitted to the audit log + the signed webhook.

The decision surface (HTTP `/admin/approvals*` and the `concierge approval` CLI)
is documented in [APPROVALS.md](APPROVALS.md).
