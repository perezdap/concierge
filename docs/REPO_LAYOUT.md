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
│       ├── redact.py
│       └── sanitize.py
│
└── tests/
    ├── test_catalog.py
    ├── test_publishing.py
    ├── test_sanitize.py
    └── test_primitives.py
```
