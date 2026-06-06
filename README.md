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
- Gateway-native primitives:
  `gateway_discover_catalog`, `gateway_enable_tools`, `gateway_disable_tools`,
  `gateway_list_active_tools`, `gateway_list_servers`, `gateway_list_profiles`,
  `gateway_use_profile`.
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

## Run with Docker Compose (Twelve-Factor)

The compose stack follows [Twelve-Factor](https://12factor.net/config) so you
can spin up, reconfigure, and roll the gateway **without rebuilding the image
or editing the compose file**. Secrets stay in `.env` (gitignored), config
lives in `config/`, and the compose file just wires the two together.

**Out of the box** — uses the committed `config/gateway.example.yaml` (the
local `echo` stdio upstream only, no tokens needed):

```bash
docker compose up --build
```

**Adding real upstream tokens** — drop them in `.env`, no rebuild:

```bash
cp .env.example .env                # gitignored
$EDITOR .env                        # set BM_LIVE_TOKEN, NOTES_TOKEN, etc.
docker compose up --force-recreate
```

The compose file bind-mounts `./config:/app/config:ro`, so any edit to a file
in `config/` is picked up on the next `docker compose up --force-recreate` —
no image rebuild. To use a separate, gitignored `config/gateway.yaml` of your
own, copy the example and add a `docker-compose.override.yml` that overrides
the `command:` to point at it.

The Compose file runs Concierge on `http://127.0.0.1:8765/mcp` and checks
`/healthz` for container health.

> **Tip**: For the simplest possible setup (echo server only, no env vars),
> pass `--config /app/config/minimal.yaml` in a `docker-compose.override.yml`.

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full
"Twelve-Factor compose" walkthrough (override patterns, where each value
comes from, why a bind mount instead of `image: build`).

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

## Troubleshooting upstream publishing

If the gateway starts but no upstream tools appear:

1. Check the container is healthy:

   ```powershell
   docker ps --filter "name=concierge" --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"
   ```

2. Check startup logs for upstream connection failures:

   ```powershell
   docker logs concierge-concierge-1 --since 10m
   ```

   Common examples:
   - `Name or service not known` — DNS/URL problem, or the container is still
     using an old baked-in `gateway.yaml`.
   - `401` / `403` — upstream bearer token or header problem.
   - `405 Method Not Allowed` on a `GET` can be harmless for Streamable HTTP MCP;
     the protocol uses `POST /mcp` for JSON-RPC messages.

3. Verify the active catalog through an MCP client:

   - `gateway_list_servers` should show configured upstreams.
   - `gateway_discover_catalog` should return upstream entries.
   - `gateway_list_active_tools` should show published tools if a profile has
     `auto_apply: true`.

4. If a proxied tool fails with `requires an authenticated subject`, the upstream
   entry has `requires_auth: true`. That means the downstream MCP client must be
   authenticated to Concierge. For local development with `auth.type: none`, set
   the upstream entry to `requires_auth: false`. This does **not** disable the
   upstream `Authorization` header Concierge sends to the remote server.

Example local Streamable HTTP upstream:

```yaml
upstream_servers:
  - id: bridgemind
    transport: streamable_http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: "Bearer ${BRIDGEMIND_TOKEN}"
    default_risk: low
    default_tags: ["bridgemind"]
    default_categories: ["utility"]
    requires_auth: false
    isolation: per_session

profiles:
  - name: default
    description: "Publish BridgeMind tools at session init."
    auto_apply: true
    selectors:
      - server: bridgemind
```

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
