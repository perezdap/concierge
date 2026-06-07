# Task Plan: Upstream OAuth Token Injection

## Goal
Bridge stored upstream OAuth credentials (`UpstreamCredentialStore` /
`UpstreamOAuthService`) into the HTTP-based upstream adapters so that tokens
obtained via the `/admin/oauth/*` flows (authorization-code+PKCE and
client-credentials) are actually sent on every upstream request, and are
auto-refreshed on expiry — closing the gap where admin OAuth stores a token
that the adapters never read.

## Branch
`feat/upstream-oauth-token-injection`

## Background (see findings.md for detail)
- `StreamableHttpAdapter` / `LegacySseAdapter` only send static `cfg.headers`
  (`base_headers`), set once at build time.
- `UpstreamOAuthService` + `UpstreamCredentialStore` are wired ONLY into
  `/admin/oauth/*` routes via `_build_oauth_deps`. Nothing reads the stored
  `OAuthTokenSet` back into adapters.
- `OAuthTokenSet` already persists `token_endpoint`, `client_id`,
  `client_secret`, `issuer`, `revocation_endpoint`, `flow` — enough to refresh
  without extra config.

## Design
A pluggable async **auth-header provider** injected into the HTTP/SSE adapters:

1. New `AuthHeaderProvider` protocol: `async def headers(server_id) -> dict[str,str]`.
2. `OAuthAuthHeaderProvider` (in `admin/oauth.py` or a new module) wraps
   `UpstreamCredentialStore` + `UpstreamOAuthService`:
   - load tokens for `server_id`; if none → return `{}` (no auth).
   - if expired and `refresh_token` present → `refresh_if_needed` using the
     token-set's stored `token_endpoint`/`client_id`/`client_secret`.
   - return `{"Authorization": f"{token_type} {access_token}"}`.
3. `StreamableHttpAdapter` / `LegacySseAdapter` accept optional
   `auth_header_provider`; before each request they `await` it and merge the
   returned headers over the static base headers (dynamic wins).
4. `app.py`: build ONE shared credential store + oauth service; pass the
   provider into `_build_adapter`, and pass the same store/service into
   `_build_oauth_deps`. Per-server opt-in keyed by `upstream_id == server_id`.

## Phases

### Phase 1: Requirements & Discovery
- **Status:** complete
- Confirmed adapter header flow, oauth service/store API, app wiring gap.

### Phase 2: Provider abstraction + refresh-from-stored-tokenset
- **Status:** complete
- Add `refresh_if_needed`-style convenience that reads endpoint/client creds
  from the stored `OAuthTokenSet` (no caller-supplied args).
- Add `OAuthAuthHeaderProvider` returning Authorization headers, with tests.

### Phase 3: Adapter integration
- **Status:** complete
- Add optional `auth_header_provider` to `StreamableHttpAdapter` and
  `LegacySseAdapter`; merge dynamic headers per request. Tests with a fake
  provider asserting header presence + refresh-on-expiry.

### Phase 4: App wiring
- **Status:** complete
- Share one credential store/oauth service between adapters and admin routes;
  pass provider through `_build_adapter`. Keep behavior identical when no
  credential is stored (no Authorization header added).

### Phase 5: Validation & docs
- **Status:** complete
- Run full test suite + lint; update `docs/AUTH.md` (or ADMIN docs) to note
  upstream tokens are now injected/refreshed automatically.

## Notes
- Must NOT break existing static-`headers` configs (e.g. GitHub PAT path).
- Must NOT log raw tokens (reuse existing redaction; never print Authorization).
- Backwards compatible: provider absent or no stored token ⇒ unchanged behavior.
