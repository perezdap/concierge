# Admin Console QA

ADMIN-10 covers release verification for the admin console epic. The runner is
full-stack HTTP E2E against a real FastAPI app with the built `/admin` SPA
mounted, an in-process fake upstream, and an in-process fake OAuth IdP.

## Local Command

```powershell
.\.venv\Scripts\python.exe scripts\admin_e2e.py
```

The harness uses a workspace-local SQLite file, the built `frontend/dist`
assets, an in-process fake upstream, and an in-process fake OAuth IdP. It does
not require Redis, Postgres, external OAuth providers, or a browser.

## Pytest Coverage

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_admin_e2e.py
```

Automated release-gate coverage:

- Served `/admin/` SPA index, deep-link fallback, `/admin/assets/*`, and JSON API precedence
- Localhost admin auth plus bearer-token accept/reject behavior
- Config validation errors through `/admin/config/draft/validate`
- Upstream add, test-connection, apply, and MCP no-restart tool availability
- Profile preview, profile apply, and auto-published tool availability
- Failed reload apply leaves active config unchanged, then rollback succeeds
- Secret redaction across admin API reads, draft diff, and YAML export
- OAuth discovery, PKCE sign-in callback, encrypted token status, token refresh, and provider revoke
- CLI execution of `scripts/admin_e2e.py`

## Manual Remainder

The automated gate verifies that the SPA is served and that every user journey's
underlying endpoint flow works. It does not drive a real browser DOM; visual
layout, focus behavior, and click-level React interactions remain manual or
Playwright follow-up checks.
