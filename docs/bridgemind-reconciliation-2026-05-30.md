# BridgeMind Reconciliation — Concierge MCP Gateway Production Readiness

Date: 2026-05-30
Project: `Concierge — MCP Gateway Production Readiness`
BridgeMind project ID: `5ffddea7-910f-494a-8782-ee032f4e84ef`
Repo: `C:\Users\dperez\Documents\github\personal\concierge`

## Executive summary

Local `main` is ahead of `origin/main` by 25 commits. The repo has substantial P0/P1 implementation work that is not fully reflected by the BridgeMind board, but several tasks currently marked `in-review` do **not** yet satisfy their acceptance criteria.

Current local validation:

- `python -m pytest -q` → **213 passed, 1 warning**
- `python -m pytest --cov --cov-report=term-missing -q` → **213 passed, coverage 83.95%, gate >=80 reached**
- Exact CI subset:
  - `ruff check tests` → **fails** with 6 lint errors
  - CI-scoped `mypy ...` → **passes**
  - `bandit -r src/concierge -ll` → **passes**
  - local `pip-audit` → **fails** because current local `pip 25.0.1` has advisories; CI upgrades pip first, so this may be environment-specific but should be verified
- `python -m concierge --config config/gateway.example.yaml` fails unless `NOTES_TOKEN` and `JIRA_TOKEN` are set, because env templating is now strict.
- With dummy `NOTES_TOKEN`/`JIRA_TOKEN` and `PYTHONPATH=src`, transient server + `examples/session_flow.py` → **passes**.

## Board/task reconciliation

| Task | BridgeMind status | Local evidence | Reconciliation |
|---|---:|---|---|
| P0-1 Origin allow-list bypass | in-review | Exact origin helpers and global ASGI middleware added in `src/concierge/server/origin.py` / `server/app.py`; tests in `tests/test_origin.py` and `tests/test_server_app.py`. | Likely implemented. Keep in review for QA signoff. |
| P0-2 Constant-time token comparison/no token leaks | in-review | `StaticBearerAuth` stores digests, checks all configured digests with `hmac.compare_digest`, emits opaque `token:<id>`; tests cover no early exit and no raw bytes. | Likely implemented. Keep in review for QA signoff. |
| P0-3 Interim approval broker | in-review | `AllowListApprovalBroker`, config `approval_mode` / `approval_allow_list`, and policy tests exist. | Likely implemented. Keep in review for QA signoff. |
| P0-4 MCP protocol conformance | in-review | Minimal negotiation, `MCP-Protocol-Version` rejection, notification `202`, and batch support added. | Partially/likely implemented, but no official conformance-suite evidence. Keep in review pending conformance/E2E verification. |
| P0-5 Resource safety | in-review | Bounded notification queues, scheduled session GC, stderr drain, and SSE chunk buffering exist with tests. | Implementation present, but acceptance requires soak (`1k sessions + chatty stderr + fragmented SSE`) not observed. Keep in review pending soak. |
| P0-6 Docs/code drift/config gaps | in-review | `${VAR}` templating implemented; dead-code cleanup/tests present; docs updated. | Mostly implemented. Note strict env templating makes example config fail without dummy env vars. |
| P0-7 Quality gate CI/tests | in-review | CI workflow exists; coverage gate passes locally. | **Not ready:** `ruff check tests` currently fails. CI is not green as-is. Should stay in review only if this is treated as a blocker to fix before signoff. |
| QA-E2E standing task | todo | `examples/session_flow.py` passes only when server is started with required env vars; no `make e2e` or full negative/soak harness found. | Still todo. This is now the main milestone gate. |
| P1-1 Real authentication | todo | Local commit adds `JwtBearerAuth` and a standalone `AuthProviderConfig`, but `config.AuthConfig` still only supports `none|bearer|localhost`, and `server/app.py` does not wire JWT/OIDC provider factory. No mTLS, real IdP integration, tenant issuance, or durable revocation flow. | Board status is correct: todo. Local JWT work is partial/prototype only. |
| P1-2 Persistent/shared state | todo | `InMemoryCatalogStore` remains the app store; no SQLite/Postgres store or Redis session store found. | Todo. |
| P1-3 Real approval workflow | todo | Only P0 allow-list broker exists. No queue-backed broker/resources/operator path/webhooks. | Todo; depends on P1-2. |
| P1-4 Distributed rate limiting | todo | Existing in-memory token bucket remains. No Redis limiter or Retry-After behavior. | Todo; depends on P1-2. |
| P1-5 Upstream resilience | in-review | `callable` flag, catalog helper, manager reconnect/backoff/down marking, and tests in `tests/test_resilience.py` exist. | Implementation present, but manual upstream-kill/E2E verification still needed before signoff. |
| P1-6 Observability | todo | Audit hooks exist, but no OpenTelemetry, Prometheus `/metrics`, `/healthz`, `/readyz`, external audit sink, or dashboard evidence. | Todo. |
| P1-7 Deployment/lifecycle | todo | Dockerfile, compose, k8s manifests, and docs exist. | Partial local work only. Health checks point at `/healthz`/`/readyz`, but those endpoints do not exist yet; deps remain floor-pinned in `pyproject.toml`; no graceful drain or vuln scan evidence. Keep todo or split partial artifact task. |
| P1-8 Output filtering/caching | in-review | `OutputFilter` and `CachingAdapter` unit code/tests exist. | **Not ready:** `build_app()` never passes an output filter into `GatewayService`, and adapters are not wrapped with `CachingAdapter`, so configured filtering/caching will not run in the real app. Should move out of in-review or wire it before QA. |

## Local commits ahead of origin grouped by task

- P0-1/P0-4/P0-5/P0-6/P0-7: commits `f55f4c8` through `81a5cf7` cover protocol header handling, SSE buffering, stderr drain, bounded queues, CI/tests, auth, approval, protocol hardening, and config templating.
- P1-7 partial: `bd7343d`, `bf6ba90`, `85f2314` add deploy manifests, Dockerfile/compose, and deployment docs.
- P1-5: `5578c0c` adds upstream callable=false/backoff behavior.
- P1-8 partial: `aa5749d` adds output filter/cache primitives but does not wire them into the app.
- P1-1 partial: `5c7a490` adds direct JWT provider tests/implementation but does not wire it into production config/app and does not meet the task acceptance criteria.

## Immediate recommended actions

1. Fix the P0-7 CI blockers first:
   - `ruff check tests` currently has 6 errors.
   - Re-run exact workflow commands after fixes.
2. Decide how to handle P1-8:
   - Either wire `OutputFilter` and `CachingAdapter` into `build_app()` and add an app-level regression test, or move the BridgeMind task out of `in-review`.
3. Treat P1-7 as partial only until P1-6 endpoints exist:
   - Current Docker/k8s health probes target missing `/healthz` and `/readyz` endpoints.
4. Keep P1-1 as todo:
   - Current JWT work is useful scaffolding but not real OIDC/mTLS/tenant issuance/revocation.
5. Run the real QA gate for all P0 in-review tasks:
   - Official/proxy MCP conformance checks for P0-4.
   - Resource soak for P0-5.
   - Negative-path E2E for origin/auth/notification/gated tools/upstream-down.
6. Only after P0 is verified, start P1 in dependency order:
   - P1-2 shared state first.
   - Then P1-3 approval queue and P1-4 distributed rate limit.
   - Then P1-6 observability and P1-7 lifecycle polish.
