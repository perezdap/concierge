# Concierge

A brokered, session-scoped capability router for MCP.

It exposes a single remote MCP endpoint (Streamable HTTP) to downstream
clients, while fanning out internally to many upstream MCP servers across
heterogeneous transports (stdio, Streamable HTTP, legacy HTTP+SSE, custom).

The gateway **catalogs everything** but **publishes little by default**.
Downstream clients discover capabilities through a compact `gateway_discover_catalog`
primitive and enable a curated subset per session. Active tools mutate at
runtime via `notifications/tools/list_changed`.

## What you get

- One outward `/mcp` endpoint, Streamable HTTP.
- Adapters for stdio, Streamable HTTP, and legacy HTTP+SSE upstreams (plus a
  registration hook for custom transports).
- Central catalog with sanitized, namespaced primitive names (`<server>__<tool>`,
  within the function-calling tool-name charset).
- Per-session publishing engine with `list_changed` notifications.
- Six gateway-native primitives:
  `gateway_discover_catalog`, `gateway_enable_tools`, `gateway_disable_tools`,
  `gateway_list_active_tools`, `gateway_list_servers`, `gateway_use_profile`.
- Profiles — named bundles of selectors that publish curated capability sets.
- Policy engine (rate limit + approval gating + risk-level enforcement).
- Audit logger with secret redaction.
- Pluggable auth (`localhost` / `bearer` / `none`; OIDC/mTLS in roadmap).
- Admin endpoints (`/admin/health`, `/admin/catalog`, `/admin/sessions`,
  `/admin/refresh/{server}`).
- Tests covering catalog, publishing, sanitization, and gateway primitives.
- Working end-to-end client demo (`examples/session_flow.py`).

## Quick Start (Docker) — Recommended

The fastest way to try Concierge with **zero local Python setup**:

```bash
git clone https://github.com/perezdap/concierge.git
cd concierge

# Start the gateway (includes a demo echo server)
docker compose up --build
```

In another terminal, verify it's running:

```bash
curl http://localhost:8765/healthz
curl http://localhost:8765/readyz
```

Point any MCP client at `http://localhost:8765/mcp`.

> **Tip**: Use `config/minimal.yaml` for the simplest possible setup (only the echo server, no environment variables required).

## Local Development Setup

## Setup (virtual environment)

Work inside a project-local virtual environment so the dependencies never touch
your global Python. Requires **Python ≥ 3.11**.

**Windows (PowerShell):**

```powershell
py -3.12 -m venv .venv          # create once (any 3.11+; py -0p lists installed)
.\.venv\Scripts\Activate.ps1    # activate (run in each new shell)
```

If activation is blocked by execution policy, allow signed scripts for your user
once: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

**macOS / Linux:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Your prompt shows `(.venv)` while it's active. Leave it with `deactivate`.
The `.venv/` folder is git-ignored — never commit it.

## Install

Inside the activated venv (the editable install also registers the `concierge`
command and lets `python -m concierge ...` run without setting `PYTHONPATH`):

```bash
python -m pip install --upgrade pip
pip install -e ".[test]"
```

## Run

```bash
python -m concierge --config config/gateway.example.yaml
```

Then point any Streamable HTTP MCP client at `http://127.0.0.1:8765/mcp`.

## Demo (no client required)

In one terminal:

```bash
python -m concierge --config config/gateway.example.yaml
```

In another:

```bash
python examples/session_flow.py
```

You'll see initialize → discover → enable → call → notification → disable.

The checked-in example config uses strict `${VAR}` env templating for placeholder
remote upstream secrets. For a local-only demo, set harmless dummy values before
starting the gateway:

```powershell
$env:NOTES_TOKEN = "demo"
$env:JIRA_TOKEN = "demo"
python -m concierge --config config/gateway.example.yaml
```

## Documentation

- `docs/ARCHITECTURE.md` — goals, non-goals, decisions, request/session/discovery flows, security model.
- `docs/REPO_LAYOUT.md` — directory map.
- `docs/ROADMAP.md` — phase 2 / phase 3 plans.

## Behavior the operator should remember

- Default bind is **127.0.0.1**. Public bind requires `gateway.bind_public: true`.
- Default auth is **localhost-only**. Use `bearer` for non-loopback.
- All upstream metadata is sanitized — control bytes stripped, length capped,
  names normalized — before it ever reaches a downstream model.
- The gateway emits `notifications/tools/list_changed` itself; it doesn't
  require upstream servers to support change notifications. Clients must
  re-fetch `tools/list` on that notification for newly enabled tools to become
  callable. For clients that can't react to it, flag a profile `auto_apply: true`
  so its tools are published at session init and appear in the first `tools/list`.
- Tools flagged `requires_approval` or `risk: dangerous` are denied by
  default until you wire in a real `ApprovalBroker` (phase 2).

## Tests

```bash
pytest -q
```

Full local E2E gate:

```powershell
.\.venv\Scripts\python.exe scripts/e2e.py
```

If you have `make` available, this is equivalent:

```bash
make e2e
```
