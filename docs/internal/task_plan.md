# Task Plan — Concierge Optimization

## Goal

Evolve the `concierge` MCP gateway for (1) low latency & strong concurrency on
routed tool calls under load, (2) reduced token/payload size to downstream LLM
clients, (3) better per-session control of the active tool surface. **Keep the
brokered, session-scoped router concept intact** — improve implementation,
extension points, and operator ergonomics only.

Priority order (from handoff): **1) session & transport → 2) payload trimming →
3) discovery/publishing**. Policy/metrics = groundwork only (no full Phase-2
engine). See `findings.md` for the grounded codebase assessment.

## Success criteria (validation)

- `python -m concierge --config config/gateway.example.yaml` starts clean.
- `python examples/session_flow.py` completes the discover→enable→call→disable flow.
- `pytest -q` passes, including new targeted tests.
- Observable: upstream sessions reused across calls in a router session;
  `tools/list` payload measurably slimmer while semantically correct.

## Key decisions / open questions (resolve before/with Phase 1)

1. **Upstream session isolation model.** ✅ DECIDED: **pooled session registry
   with configurable `isolation` per upstream** — `shared` (default, current
   behavior, best for stdio) vs `per_session` (opt-in, for HTTP multi-tenant
   isolation). Lazy create + LRU evict.
2. **Scope of this engagement.** ✅ DECIDED: implement all phases (1–5),
   checking in at phase boundaries.
3. **Backward compat.** Default config must preserve current behavior so
   `session_flow.py` keeps working unchanged. (Constraint, kept in force.)

## Phases

### Phase 0 — Planning & baseline  ·  Status: complete
- [x] Read all core modules; capture findings → `findings.md`
- [x] Confirm isolation model + engagement scope with user (decisions 1–2)
- [x] Establish baseline: `pytest -q` green starting point
- **Verify:** decisions logged in `progress.md`; baseline green.

### Phase 1 — Session & transport optimization  ·  Status: complete
- [x] Session registry keyed by `(upstream_id, router_session_id)` with
      configurable isolation (shared|per_session). `manager.py` control-plane
      adapter vs pooled data-plane sessions.
- [x] Lazy creation on first use; LRU eviction with cap; graceful teardown
      (`_resolve_session_adapter`, `_enforce_pool_cap`, `evict_router_session`).
- [x] Connect retry/backoff with jitter (`_connect_with_backoff`).
- [x] Wired `SessionManager.gc()` into a background loop in app lifespan;
      `on_evict` callback tears down pooled upstream sessions; facade DELETE
      uses `aclose`.
- [x] Config knobs: `UpstreamServerConfig.isolation` + connect backoff;
      new `SessionPoolConfig` (idle_ttl/gc_interval/max_upstream_sessions).
- **Verify:** ✅ 8 new tests in `test_session_registry.py` prove reuse,
  per-session isolation, LRU evict, router-session teardown, backoff
  retry/give-up, GC on_evict. `pytest -q` → 28 passed. App builds from example
  config; defaults preserve shared behavior.

### Phase 2 — Payload & schema optimization  ·  Status: complete
- [x] Slim `tools/list`: `util/payload.slim_schema` tightens schema description
      cap + drops verbose keys on the model-facing surface; `/admin/catalog`
      keeps the full (rich) schema. Config-gated (`slim_tools_list`), opt-in
      (default off for backward compatibility).
- [x] Opt-in result text cap in `tools_call` (`cap_result_text`, default OFF;
      explicit `_meta.gateway_truncated` marker — never silent).
- [x] Drop null/default fields from discovery `_compact_entry` (omit
      `when_to_use`/empty arrays/false flags; `args` use `exclude_none`).
- [x] Config knobs: new `PayloadConfig` + `PayloadOptions`; wired via app.py.
- **Finding:** the catalog allowlist (`validate_input_schema`) already strips
  examples/$schema/$comment/default + caps desc at 600, so gateway slim's net
  win is the tighter 160-char model-facing desc cap (+ defense for unvalidated
  schemas). Documented honestly in tests.
- **Verify:** ✅ 9 new tests in `test_payload.py` (slim_schema unit, result cap
  on/off, tools/list slim default + toggle, discovery omits null/empty).
  `pytest -q` → 37 passed. Example config loads with `payload` section.

### Phase 3 — Discovery & publishing behavior  ·  Status: complete
- [x] Profile-aware discovery: `gateway_discover_catalog` gains a `profile` arg
      (scopes results to what a profile would enable); new `Catalog.list(names=)`.
- [x] New `gateway_list_profiles` primitive (name/description/enables_count/sample)
      so a model can pick a workflow bundle without scanning the catalog.
      (Existing server/tags/category/risk filters already cover the other
      heuristics from the brief.)
- [x] Verified `notifications/*_list_changed` coalescing (by (session,method)
      window) + distinct-method correctness; idempotent so coalescing is safe.
- **Verify:** ✅ 7 new tests in `test_discovery.py` (profile scope, unknown
  profile → InvalidParams, list_profiles counts/sample + native primitive, bus
  coalesce same-method / keep distinct, rapid enable+disable coalesces).
  `pytest -q` → 44 passed.

### Phase 4 — Policy/metrics groundwork  ·  Status: complete
- [x] Metrics hooks via audit (no new deps): `tool.call` now carries
      request/response bytes (+ existing latency); `upstream_session.{created,
      evicted,lru_evicted}`; `session.evicted` (with upstream sessions freed).
      Upstream error rate derivable from `tool.call ok=false`.
- [x] Clean seams kept: `AdapterManager(audit=…)` optional; app composes the
      session→pool teardown+audit in one place. risk/approval groundwork already
      present (untouched), policy engine still the injection point.
- **Verify:** ✅ 4 new tests in `test_metrics.py`. `pytest -q` → 48 passed.

### Phase 5 — Docs, tests, full validation  ·  Status: complete
- [x] `docs/ARCHITECTURE.md`: added §12 session registry, §13 payload
      optimization, §14 metrics/audit hooks.
- [x] `docs/REPO_LAYOUT.md`: new files + full config-knob reference + how to run
      tests. Added `pythonpath=["src"]` to pyproject so `pytest -q` works as the
      brief assumes.
- [x] Tests cover the three required areas: session reuse
      (test_session_registry), slimmer-yet-correct payloads (test_payload),
      curated discovery + enable/disable (test_discovery, test_primitives).
- **Verify:** ✅ all validation commands pass —
  `pytest -q` → 48 passed;
  `python -m concierge --config config/gateway.example.yaml` boots + binds
  (with `PYTHONPATH=src`/editable install);
  `python examples/session_flow.py` completes discover→enable→call→disable,
  observing compact discovery, the new `gateway_list_profiles`, and the
  list_changed SSE notification.

## Active Addendum — BridgeMind P0-2 (2026-05-29)

### Goal
Harden `StaticBearerAuth` so static bearer tokens are validated without timing-oracle membership tests and no raw token bytes reach audit/log subjects.

### Status: in-review-ready
- [x] Confirmed implementation in `src/concierge/server/auth.py`: tokens stored as SHA-256 digests; presented token hashed; `hmac.compare_digest` checks every configured digest without early exit; subject is salted opaque `token:<id>`.
- [x] Regression coverage in `tests/test_auth_providers.py`: accept/reject, no raw token in subject, stable/distinct opaque ids, and no early exit across configured digests.
- [x] Targeted validation: `python -m pytest tests/test_auth_providers.py -q` → 8 passed.

### Follow-up / unrelated blocker
- Full suite currently stops at `tests/test_app_factory.py::test_build_auth_variants` because that untracked test passes `AuthConfig` directly to `_build_auth`, while `_build_auth` currently expects the full gateway config. This is outside P0-2.

## Active Addendum — BridgeMind P0 QA verification (2026-05-30)

### Goal
Verify all BridgeMind P0 in-review work against the production-readiness acceptance criteria, with emphasis on the previously risky facade/auth/origin/protocol/resource-safety layers.

### Status: in-review-ready for human sign-off
- [x] Local quality gate is green: ruff, mypy, bandit, pip-audit, and pytest coverage.
- [x] Targeted P0 regression suite passed for facade/auth/origin/protocol, approval policy, adapter framing/resource-safety, resilience, and config env templating.
- [x] Real-server session flow passed using `config/gateway.example.yaml` with dummy secret env vars.
- [x] P0-5 focused soak passed for 1k sessions, bounded queues, GC eviction hooks, chatty stderr, and fragmented SSE parsing.
- [x] Reusable E2E command added: `.venv/Scripts/python.exe scripts/e2e.py`; `make e2e` delegates to the same harness where make is available.

### Follow-up
- Keep P0 tasks in `in-review` for human approval; do not mark complete automatically.
- QA-E2E standing task is ready to move to `in-review` after BridgeMind is updated with the reusable E2E command evidence.

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| Full pytest failure in unrelated app factory helper test | `python -m pytest -x -vv` | Logged as unrelated follow-up; targeted P0-2 auth tests pass. |
