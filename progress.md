# Progress Log

## Session: Upstream OAuth Token Injection

### Phase 1: Requirements & Discovery — complete
- Traced adapter header flow, oauth service/store API, app wiring gap.
- Confirmed: admin OAuth stores tokens that adapters never read.

### Phase 2: Provider abstraction + refresh — complete
- `admin/oauth.py`: added `UpstreamOAuthService.valid_token_set(upstream_id)`
  — self-contained refresh using endpoint/client creds from stored token set.
- `adapters/auth_headers.py` (new): `AuthHeaderProvider` Protocol +
  `OAuthAuthHeaderProvider` returning `{Authorization: <type> <token>}`,
  empty dict when no credential / on refresh failure.

### Phase 3: Adapter integration — complete
- `streamable_http.py`: optional `auth_header_provider`; new `_request_headers()`
  merges dynamic auth over static headers; used in POST, notification, GET stream.
- `sse_legacy.py`: optional `auth_header_provider`; new `_auth_headers()` merge;
  used in SSE GET, POST request, POST notification.

### Phase 4: App wiring — complete
- `app.py`: `_build_oauth_service()` builds shared service; `_build_oauth_deps`
  now accepts/reuses it. `_build_adapter` + `_assemble_runtime_bundle` thread an
  `auth_header_provider`. `build_app` constructs one shared provider used by both
  adapters and admin OAuth routes.
- Verified: `IMPORT OK` (module imports + app import clean).

### Phase 5: Validation & docs — complete
- Added `tests/test_upstream_auth_injection.py` (10 tests: provider behavior,
  refresh-on-expiry, refresh-failure fallback, adapter merge/override, no-provider).
- Fixed `scripts/admin_e2e.py` `fake_oauth_deps` to accept the new `oauth` kwarg
  and inject its fake-IDP service (renamed local `oauth` -> `oauth_fake`).
- Full suite: 433 passed, 21 skipped. Ruff: all checks passed.
- Updated `docs/AUTH.md` with an "Upstream OAuth (outbound)" section explaining
  token injection + auto-refresh and the inbound vs outbound distinction.

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| import smoke | import app + auth_headers | clean | clean | pass |
| test_upstream_auth_injection | 10 cases | all pass | 10 passed | pass |
| full suite | pytest -q | green | 433 passed, 21 skip | pass |
| ruff | changed files | clean | all checks passed | pass |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| - | ModuleNotFoundError concierge | 1 | use .venv/Scripts/python.exe |
| - | admin_e2e fake_oauth_deps missing `oauth` kwarg | 1 | add oauth=None param |
| - | NameError oauth after rename | 2 | rename local to oauth_fake everywhere |
| - | ruff UP037/E501/F401 | 1 | unquote annotations, split long lines, drop unused import |
