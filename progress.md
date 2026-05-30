# Progress Log — Concierge Optimization

## Session 1 — 2026-05-29

### Done
- Restored context: no prior planning files existed. Created `task_plan.md`,
  `findings.md`, `progress.md`.
- Read core modules: docs (ARCHITECTURE/ROADMAP/REPO_LAYOUT), `config.py`,
  `core/{types,catalog,session,publishing}.py`, `adapters/{base,manager,stdio,
  streamable_http}.py`, `gateway/{service,primitives,profiles}.py`,
  `server/app.py`, `util/audit.py`, `examples/session_flow.py`.
- Captured grounded assessment per objective in `findings.md`.

### Key observations
- One upstream connection per `server_id`, shared across all router sessions
  (no `(upstream, router_session)` keying).
- Retry/backoff-with-jitter is documented but not implemented.
- `SessionManager.gc()` exists but is never scheduled → unbounded session growth.
- `tools/list` ships full input schemas; results returned verbatim (no trimming).
- Discovery primitive is already compact/paginated/schema-free.
- `risk_level` / `requires_approval` groundwork already present.

### Decisions pending (need user input)
1. Upstream session isolation model: pooled registry w/ `shared` (default) vs
   `per_session` opt-in. (Recommended over blind per-session fan-out.)
2. Engagement scope: implement now vs review plan first.

### Next
- Get answers on the two decisions, then start Phase 1.

### Decisions resolved
1. Isolation: **pooled, configurable** (shared default / per_session opt-in).
2. Scope: **implement all phases 1–5**, check in at phase boundaries.

### Test results
- Baseline: `PYTHONPATH=src python -m pytest -q` → **20 passed**.
- NOTE: package is not pip-installed; tests need `PYTHONPATH=src` (src layout).

---

## Session 1 (cont.) — Phase 1: Session & transport

### Plan for Phase 1
- Session registry abstraction keyed by (upstream_id, router_session_id),
  isolation mode per upstream (shared|per_session), lazy create, LRU evict,
  graceful teardown.
- Connect retry/backoff with jitter.
- Schedule SessionManager.gc() in app lifespan; emit session_closed on evict.
- Config knobs + keep defaults backward-compatible.

### Phase 1 DONE
- `manager.py`: split control-plane adapter (catalog/refresh/shared calls) from
  pooled data-plane sessions for `per_session` upstreams. `_resolve_session_adapter`,
  `_connect_with_backoff` (exp backoff + 0.5–1.0x jitter), `_enforce_pool_cap`
  (LRU), `evict_router_session`, `pool_size`. `call_tool/read_resource/get_prompt`
  now take `router_session_id`.
- `service.py`: threads `session.session_id` into all three adapter calls.
- `session.py`: `on_evict` async hook; `aclose()`; `gc()` fires hook per victim.
- `app.py`: adapter factories + isolation/backoff wired; `SessionManager`
  `on_evict=adapters.evict_router_session`; background `_session_gc_loop`.
- `facade.py`: DELETE uses `aclose`.
- config: `isolation`, connect backoff, `SessionPoolConfig`. Example yaml updated
  (notes upstream demos `per_session`).
- Tests: `test_session_registry.py` (8). Full suite **28 passed**.
- Scoping note: backoff applied to on-demand per_session creation (the live-call
  hot path). Startup `start_all` keeps single-attempt + periodic refresh loop to
  avoid slow boot on a down upstream.

### Phase 2 plan — payload & schema
- Slim `tools/list` (optional/trimmed inputSchema; slim vs rich for /admin).
- Cap/normalize heavy upstream results in tools_call.
- Drop null/default fields from discovery `_compact_entry`.
- Config: PayloadConfig knobs. Keep defaults safe + backward compatible.

### Phase 2 DONE
- New `util/payload.py`: `PayloadOptions`, `slim_schema` (drop verbose keys +
  cap descriptions, recursive), `cap_result_text` (opt-in, marks `_meta`).
- `service.py`: tools_list slims published schemas (config-gated); tools_call
  caps results. New optional `payload` ctor arg.
- `primitives.py`: `_compact_entry` omits null/empty/false fields.
- `config.py`: `PayloadConfig`; `app.py` wires `PayloadOptions`. Example yaml
  documents the `payload` block.
- Key finding: catalog `validate_input_schema` is an allowlist already stripping
  examples/$schema/$comment/default + capping desc@600. Gateway slim's net win =
  tighter 160-char model-facing desc cap. Tests reflect this honestly.
- Tests: `test_payload.py` (9). Full suite **37 passed**.

### Phase 3 plan — discovery & publishing
- Add selection heuristics: profile-aware discovery (list/preview profiles);
  build on existing catalog filters (server/tags/category/risk already exist).
- Verify list_changed correctness + coalescing (NotificationBus already has
  coalesce_window_s) under rapid enable/disable.

### Phase 3 DONE
- `catalog.py`: `list(names=...)` restriction set.
- `primitives.py`: discovery `profile` arg (scopes to profile.resolve); new
  `gateway_list_profiles` primitive (counts + sample).
- Verified coalescing in `NotificationBus` (by (session,method) window).
- Tests: `test_discovery.py` (7). Full suite **44 passed**.

### Phase 4 plan — policy/metrics groundwork
- Extend AuditLogger: payload sizes on tool.call; session.evicted;
  upstream_session.created/lru_evicted/evicted.
- service computes request/response bytes. manager gets optional audit sink.
- app wires audit into manager + session on_evict. No new heavy deps.

### Phase 4 DONE
- `audit.py`: tool_called gains request_bytes/response_bytes; new
  session_evicted + upstream_session(event,...).
- `service.py`: `_safe_bytes` computes payload sizes for tool.call.
- `manager.py`: optional `audit`; emits upstream_session created/evicted/lru.
- `app.py`: `_on_session_evict` composes pool teardown + session.evicted audit.
- Tests: `test_metrics.py` (4). Full suite **48 passed**.

### Phase 5 DONE — docs + validation
- `pyproject.toml`: `pythonpath=["src"]` → `pytest -q` works w/o install.
- Docs: ARCHITECTURE §12 (session registry) / §13 (payload) / §14 (metrics);
  REPO_LAYOUT new files + config-knob tables + run-tests note.
- Validation:
  * `pytest -q` → **48 passed**.
  * `python -m concierge --config config/gateway.example.yaml` → boots/binds
    127.0.0.1:8765 (needs PYTHONPATH=src or `pip install -e .`).
  * `python examples/session_flow.py` → full flow OK; observed compact
    discovery (null fields dropped), new `gateway_list_profiles`, echo routing,
    list_changed SSE.

## ALL PHASES COMPLETE (0–5)
- Default config preserves MVP behavior (isolation defaults to shared);
  per_session/payload/metrics are opt-in or backward-compatible.
- Net new files: src/concierge/util/payload.py; tests test_session_registry,
  test_payload, test_discovery, test_metrics.
- Pre-existing untracked file `concierge-production-plan.html` is unrelated/not
  mine — left untouched.

---

## Session 2 — 2026-05-29 — BridgeMind P0-2

### Done
- Claimed BridgeMind task `bc258d0f-065d-4821-bd79-55421d82771e` ([P0-2] constant-time token comparison + no token-byte leaks) for Security Engineer and moved it to `in-progress`.
- Verified `src/concierge/server/auth.py` stores SHA-256 token digests, compares presented tokens with `hmac.compare_digest` across all configured digests without early exit, and returns a salted opaque `token:<id>` audit subject instead of raw token bytes.
- Added a regression assertion in `tests/test_auth_providers.py` proving a valid first token still checks every configured digest.

### Validation
- `python -m pytest tests/test_auth_providers.py -q` → **8 passed**, 1 existing Starlette/httpx deprecation warning.
- `python -m pytest -x -vv` currently fails outside this task at `tests/test_app_factory.py::test_build_auth_variants` (`AuthConfig` passed to `_build_auth`, which currently expects the full gateway config). Not addressed for P0-2.

---

## Session 3 — 2026-05-30 — QA-E2E / quality gate triage

### Done
- Claimed BridgeMind task `53678898-b349-4881-adcb-2083797e980e` ([QA-E2E] standing release verification) for the QA Engineer and moved it to `in-progress`.
- Ran baseline suite: `.venv/Scripts/python.exe -m pytest -q` → **215 passed**, 1 existing Starlette/httpx deprecation warning.
- Found quality-gate failures in tracked P0/P1 edits: mypy annotation issues and Bandit low-severity findings (`except: pass`, non-crypto jitter RNG, runtime `assert`). Applied surgical cleanup in tracked files only.

### Validation
- Initial cleanup fixed tracked code issues but exposed draft P1-2 storage files in the quality gate.
- Reconciled storage draft with the now-async `CatalogStore` interface: `SqliteCatalogStore` remains async via `asyncio.to_thread()`, `PostgresCatalogStore` implements the async ABC, and storage tests match async catalog semantics.
- Final local quality gate is green:
  - `.venv/Scripts/python.exe -m ruff check src tests` → **PASS**
  - `.venv/Scripts/python.exe -m mypy src` → **PASS**
  - `.venv/Scripts/python.exe -m bandit -q -r src/concierge` → **PASS**
  - `.venv/Scripts/python.exe -m pip_audit` → **PASS**
  - `.venv/Scripts/python.exe -m pytest --cov -q` → **223 passed, 6 skipped**, coverage **83.67%**

### Follow-up
- Workspace now has a clean quality-gate story. Next QA step is to verify in-review BridgeMind P0/P1 tasks against their acceptance criteria, starting with P0 blockers.

---

## Session 4 — 2026-05-30 — BridgeMind P0 QA verification

### Done
- Re-ran the local CI quality gate on branch `production-readiness-observability`.
- Ran targeted P0 regression suite for facade/auth/origin/protocol, approval policy, resource safety, adapter framing, resilience, and config templating.
- Ran transient real-server E2E with `config/gateway.example.yaml`, dummy `NOTES_TOKEN`/`JIRA_TOKEN`, and `examples/session_flow.py`.
- Ran a focused P0-5 soak script covering 1k sessions with bounded queues, GC eviction hooks, chatty stdio stderr, and fragmented Streamable HTTP + legacy SSE parsing.
- Added reusable E2E harness: `scripts/e2e.py`, `make e2e`, and README instructions for the direct Windows-friendly command.

### Validation
- `.venv/Scripts/python.exe -m ruff check src tests` → **PASS**
- `.venv/Scripts/python.exe -m mypy src` → **PASS**
- `.venv/Scripts/python.exe -m bandit -q -r src/concierge` → **PASS**
- `.venv/Scripts/python.exe -m pip_audit` → **PASS** (`No known vulnerabilities found`)
- `.venv/Scripts/python.exe -m pytest --cov -q` → **226 passed, 6 skipped**, coverage **83.66%**
- Targeted P0 suite → **86 passed**, 1 existing Starlette/httpx deprecation warning.
- Real-server `examples/session_flow.py` → **PASS**: initialize, tools/list, discover, echo call, disable, and SSE `notifications/tools/list_changed` observed.
- P0-5 soak → **PASS**: 1000 queues capped at depth 8, 1000 sessions GC-evicted with 1000 eviction hooks, chatty stderr request returned, fragmented SSE parsers succeeded.
- `.venv/Scripts/python.exe scripts/e2e.py` → **PASS** (targeted P0 tests, resource soak, real-server session flow).
- `make e2e` was not runnable in this local shell because `make` is not installed; the target exists and delegates to `python scripts/e2e.py`.

### Follow-up
- BridgeMind P0 tasks should remain `in-review` for human sign-off; QA evidence now supports sign-off for P0-1 through P0-7.
- QA-E2E standing task can move to `in-review` after BridgeMind is updated with the new reusable E2E command evidence.
