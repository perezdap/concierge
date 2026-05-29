# Repository layout

```
concierge/
├── pyproject.toml
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── REPO_LAYOUT.md
│   └── ROADMAP.md
├── config/
│   └── gateway.example.yaml
├── examples/
│   ├── session_flow.py            # end-to-end example client
│   └── upstream_echo_server.py    # tiny stdio MCP for demo
├── src/concierge/
│   ├── __init__.py
│   ├── __main__.py                # `python -m concierge`
│   ├── config.py                  # config loading + validation (pydantic)
│   ├── errors.py                  # GatewayError hierarchy + JSON-RPC mapping
│   │
│   ├── core/
│   │   ├── types.py               # CatalogEntry, Session, PublishedState, etc.
│   │   ├── catalog.py             # in-memory CatalogStore + interface
│   │   ├── publishing.py          # PublishingService
│   │   ├── session.py             # SessionManager
│   │   └── notifications.py       # per-session event queue
│   │
│   ├── adapters/
│   │   ├── base.py                # UpstreamAdapter ABC + AdapterStatus
│   │   ├── manager.py             # AdapterManager (lifecycle, circuit breaker)
│   │   ├── stdio.py               # StdioAdapter
│   │   ├── streamable_http.py     # StreamableHttpAdapter
│   │   ├── sse_legacy.py          # LegacySseAdapter
│   │   └── custom.py              # placeholder
│   │
│   ├── policy/
│   │   ├── engine.py              # PolicyEngine
│   │   ├── ratelimit.py           # token bucket
│   │   └── approval.py            # ApprovalBroker interface
│   │
│   ├── gateway/
│   │   ├── service.py             # GatewayService — business logic
│   │   ├── primitives.py          # gateway-native tools (discover/enable/...)
│   │   └── profiles.py            # named bundles
│   │
│   ├── server/
│   │   ├── app.py                 # FastAPI app factory
│   │   ├── auth.py                # AuthProvider implementations
│   │   ├── facade.py              # JSON-RPC + Streamable HTTP handler
│   │   └── admin.py               # debug/admin endpoints
│   │
│   └── util/
│       ├── audit.py
│       ├── log.py
│       ├── payload.py            # slim_schema / cap_result_text (token reduction)
│       ├── redact.py
│       └── sanitize.py
│
└── tests/
    ├── test_catalog.py
    ├── test_publishing.py
    ├── test_sanitize.py
    ├── test_primitives.py
    ├── test_session_registry.py # per-session pool, LRU, backoff, GC
    ├── test_payload.py          # schema slimming + result caps + discovery compaction
    ├── test_discovery.py        # profile-scoped discovery + list_changed coalescing
    ├── test_metrics.py          # audit payload sizes + pool/session lifecycle events
    └── test_config.py           # config defaults + validation (session_pool/payload)
```

## Configuration knobs

Loaded by `config.py` from a single YAML file (see `config/gateway.example.yaml`).
Beyond the MVP fields, optimization adds:

**Per upstream (`upstream_servers[]`)**

| Field | Default | Purpose |
|---|---|---|
| `isolation` | `shared` | `shared` = one upstream session for all router sessions; `per_session` = isolated, pooled session per router session. |
| `connect_max_retries` | `3` | Retries for on-demand (per_session) connect. |
| `connect_backoff_base_s` / `connect_backoff_max_s` | `0.5` / `10.0` | Exponential backoff bounds (with jitter). |

**`session_pool`**

| Field | Default | Purpose |
|---|---|---|
| `idle_ttl_s` | `3600` | Idle router sessions GC'd after this. |
| `gc_interval_s` | `60` | GC sweep cadence. |
| `max_upstream_sessions` | `256` | Global LRU cap on pooled per_session upstream sessions. |

**`payload`**

| Field | Default | Purpose |
|---|---|---|
| `slim_tools_list` | `false` | Opt-in: slim published-tool schemas on the model-facing `tools/list`. |
| `max_schema_description_chars` | `160` | Cap inline schema descriptions there. |
| `drop_schema_examples` | `true` | Drop verbose `examples`/`$comment` from schemas. |
| `max_result_bytes` | `0` | Cap heavy result text (`0` = never truncate). |

**Per profile (`profiles[]`)**

| Field | Default | Purpose |
|---|---|---|
| `auto_apply` | `false` | Opt-in: publish this profile's tools at session init so they appear in the first `tools/list` — for clients that don't react to `notifications/tools/list_changed`. |

## Running the tests

Set up a virtual environment first (see the **Setup** section in the top-level
`README.md`), then `pip install -e ".[test]"` inside it.

`pyproject.toml` sets `pythonpath = ["src"]`, so `pytest -q` works from the repo
root without an editable install. To run the server module directly
(`python -m concierge ...`) either `pip install -e .` or set `PYTHONPATH=src`.
