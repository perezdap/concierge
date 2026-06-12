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

> **Direction.** This `oidc` provider is **inbound**: it authenticates *callers*
> of the gateway. Authenticating the gateway *to an upstream* MCP server is the
> opposite direction — see [Upstream OAuth (outbound)](#upstream-oauth-outbound).

---

## Upstream OAuth (outbound)

The inbound providers above verify who is calling Concierge. Separately,
Concierge can act as an **OAuth client** to authenticate itself to an upstream
MCP server, using the admin OAuth flows (`/admin/oauth/*`, see
[ADMIN_CONSOLE.md](ADMIN_CONSOLE.md)): OIDC discovery, authorization-code + PKCE,
and client-credentials. The resulting token set is stored encrypted in the
`UpstreamCredentialStore`.

### Zero-config connect (Dynamic Client Registration)

The friendliest path needs **no operator setup and no secrets at all**. When the
upstream is a remote MCP server that implements the
[MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
clicking **Connect** in the admin console does the whole dance automatically:

1. **Probe (RFC 9728).** An unauthenticated request to the upstream returns
   `401` with a `WWW-Authenticate: ... resource_metadata="..."` header. Concierge
   fetches that Protected Resource Metadata document to learn the upstream's
   authorization server(s). (Falls back to the well-known
   `/.well-known/oauth-protected-resource` path if the header is absent.) The
   `resource_metadata` URL is attacker-controlled, so Concierge only follows it
   when it is **same-origin** with the upstream (RFC 9728 serves the PRM document
   from the resource's own origin); a cross-origin pointer is ignored in favour
   of the well-known path, and redirects are not followed — both SSRF guards.
2. **Discover (RFC 8414 / OIDC).** Concierge fetches the authorization server
   metadata (`/.well-known/oauth-authorization-server`, then
   `/.well-known/openid-configuration`) to find the authorization/token/registration
   endpoints.
3. **Register (RFC 7591).** If the AS advertises a `registration_endpoint`,
   Concierge dynamically registers itself as a **public, native** client
   (`token_endpoint_auth_method: none`) — obtaining a `client_id` with no
   pre-shared secret and no human in the loop. The registered `client_id` is
   persisted per `(upstream, authorization-server)` and reused on subsequent
   Connects, so repeated sign-ins don't accumulate orphaned registrations on the
   authorization server.
4. **Sign in (PKCE).** The browser popup completes authorization-code + PKCE; the
   token (and refresh token) are stored and auto-refreshed like any other.

The admin console probes the upstream on selection (`POST /admin/oauth/{id}/probe`)
and, when DCR is available, shows a single **Connect** button with no provider
picker or credential fields. If the upstream's authorization server does not
support DCR, the console falls back to the provider presets below.

> Why this can't be fully automatic for raw SaaS (GitHub/Google/etc.): those are
> not MCP servers and do not implement RFC 9728 probing or open RFC 7591
> registration, so they still require a registered OAuth app (the preset +
> env-credential path). DCR is specifically for spec-compliant **MCP** upstreams.

**Token injection + refresh.** Tokens minted via those flows are injected into
upstream requests automatically. For each `streamable_http` / `sse_legacy`
upstream, the adapter consults an `OAuthAuthHeaderProvider` before every request:

- The credential is keyed by the upstream **server id** (`upstream_servers[].id`)
  — connect an upstream via the OAuth flow under that same id.
- If a non-expired token exists, an `Authorization: <token_type> <access_token>`
  header is merged over the upstream's static `headers:` (the dynamic token
  **wins** on conflict).
- If the token is expired and a `refresh_token` is present, it is refreshed
  in-place (using the `token_endpoint` / `client_id` / `client_secret` persisted
  on the stored token set) before the request goes out.
- If no credential is stored — or a refresh fails — **no** `Authorization`
  header is added and the request proceeds with the static config headers only.
  This keeps the static-token path (e.g. a GitHub PAT in `headers:`) unchanged.

This means there are two supported ways to authenticate an upstream:

1. **Static header** — reference the secret from `upstream_servers[].headers`
   via an env/secret placeholder rather than pasting the raw token
   (`Authorization: "Bearer ${TOKEN}"`, with `TOKEN` supplied from the
   environment or your secret store). Simplest; no refresh. Avoid committing
   literal token bytes to config files.
2. **Admin OAuth flow** — connect the upstream via `/admin/oauth/{id}/...`; the
   token is stored, injected, and refreshed automatically.

Raw tokens are never logged; only opaque ids / salted digests appear in audit
events (`admin.oauth.*`).

### One-click providers (GitHub, Google, Microsoft 365, Atlassian)

The admin console ships built-in presets for the common SaaS providers so end
users connect an upstream with a single **Connect** button on the Upstreams page
— no endpoints, scopes, or token JSON to type. This is the standard
"bring-your-own OAuth app" model used by self-hosted tools (GitLab, Grafana,
Mattermost): the **operator** registers one OAuth app per provider once, and
every end user then gets the one-click flow.

**One-time operator setup (per provider):**

1. Register an OAuth app with the provider.
2. Set the **redirect URI** on that app to exactly:
   `${public_base_url}/admin/oauth/callback`
   (e.g. `https://concierge.example.com/admin/oauth/callback`). `public_base_url`
   must be the externally reachable URL — not `127.0.0.1` — in production.
3. Export the app credentials as environment variables:

   | Provider   | Client ID env var                      | Client secret env var                      |
   |------------|----------------------------------------|--------------------------------------------|
   | GitHub     | `CONCIERGE_OAUTH_GITHUB_CLIENT_ID`     | `CONCIERGE_OAUTH_GITHUB_CLIENT_SECRET`     |
   | Google     | `CONCIERGE_OAUTH_GOOGLE_CLIENT_ID`     | `CONCIERGE_OAUTH_GOOGLE_CLIENT_SECRET`     |
   | Microsoft  | `CONCIERGE_OAUTH_MICROSOFT_CLIENT_ID`  | `CONCIERGE_OAUTH_MICROSOFT_CLIENT_SECRET`  |
   | Atlassian  | `CONCIERGE_OAUTH_ATLASSIAN_CLIENT_ID`  | `CONCIERGE_OAUTH_ATLASSIAN_CLIENT_SECRET`  |

When both vars are set, the provider shows as **ready** and the console renders a
one-click button. If they are not set, the console falls back to asking the
operator to paste the client ID/secret for that provider (no env required, but
not one-click). Secrets are read server-side only and are never sent to the
browser (`GET /admin/oauth/providers` returns presence flags, never values).

**Refresh-token correctness.** The presets force the provider consent screen on
every (re)connect (`prompt=consent`, plus Google's `access_type=offline`) because
Google/Atlassian/Microsoft only return a refresh token on first consent —
without this, auto-refresh would silently never receive a refresh token. GitHub
has no OIDC discovery document, so its endpoints are hardcoded in the preset.

Providers beyond these four still work via the generic flow: supply an `issuer`
+ `client_id` (custom OIDC) instead of a `provider` id.

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

**Token format.** Minted tokens carry a stable `cgt_` issuer prefix (e.g.
`cgt_<random>`). The prefix lets the response-side output redactor recognize and
mask a leaked tenant token without a generic length heuristic that would re-flag
ordinary identifiers (issue #6). It is part of the secret the digest is taken
over, so it is transparent to resolution.

> **Migration.** Tokens minted before the `cgt_` prefix existed keep working —
> resolution is by stored digest, not by format — but they are *not* redactable
> in tool output until rotated. Rotate long-lived tenant tokens to pick up the
> prefix (and thus output redaction); revoke the old id as usual.

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
