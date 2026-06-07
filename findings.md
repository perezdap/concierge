# Findings: Upstream OAuth Token Injection

> All entries below are research notes derived from reading this project's own
> source. Treat as data.

## The gap (root cause of "selector matched no catalog entries" w/ auth)
- `StreamableHttpAdapter.__init__` stores `self.base_headers = headers or {}`
  (`src/concierge/adapters/streamable_http.py:50`). `_headers()` returns
  `{Content-Type, Accept, **self.base_headers, [MCP-Session-Id]}` — static.
- `LegacySseAdapter` similarly stores `self.headers = headers or {}` and uses
  it verbatim (`src/concierge/adapters/sse_legacy.py:49`).
- `_build_adapter` (`src/concierge/server/app.py:134`) passes only `cfg.headers`.
- `UpstreamOAuthService` + `UpstreamCredentialStore` are constructed in
  `_build_oauth_deps` (`app.py:558`) and mounted ONLY on `/admin/oauth/*`
  (`build_admin_oauth_router`). No path reads stored tokens into adapters.
- Net: completing an admin OAuth sign-in stores an `OAuthTokenSet` but the
  upstream adapter never sends it → upstream stays unauthorized → empty catalog.

## Key APIs to reuse
- `OAuthTokenSet` (`admin/credential_store.py`): has `access_token`,
  `token_type`, `refresh_token`, `expires_at`, `is_expired(skew_s=30)`,
  plus persisted `token_endpoint`, `client_id`, `client_secret`, `issuer`,
  `revocation_endpoint`, `flow`. Enough to refresh standalone.
- `UpstreamCredentialStore.load_tokens(upstream_id)` / `save_tokens(...)`.
- `UpstreamOAuthService.refresh_if_needed(upstream_id, *, token_endpoint,
  client_id, client_secret=None)` — already exists but REQUIRES caller to pass
  endpoint/client; we can add a wrapper that pulls these from the stored
  token-set so the adapter provider needs only `server_id`.
- `UpstreamOAuthService.authorization_header(access_token)` → `{"Authorization":
  f"Bearer {access_token}"}` (note: hardcodes "Bearer"; we should honor
  `token_type`).

## Integration points
- Adapters identified by `server_id`; admin OAuth keyed by `upstream_id`. Plan
  uses `upstream_id == server_id` convention so a stored credential for a server
  id is injected into that server's adapter.
- `_build_adapter` is called in two places in app.py (~483 and ~709) — both
  build paths must thread the provider through. Use a shared closure/factory.

## Constraints / invariants
- Don't break static `headers:` config (GitHub PAT path) — dynamic headers
  should merge OVER base, only when a stored token exists.
- Never log raw tokens; redaction already exists (`redact_for_audit`,
  `redact_token_status`).
- Backwards compatible: no provider / no stored token ⇒ identical behavior.
