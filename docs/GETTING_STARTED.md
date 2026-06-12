# Getting Started — Admin UI First

Configure Concierge entirely from the browser: add upstreams, wire profiles,
apply changes live, and have them survive restarts. This is the operator
walkthrough; field-by-field reference lives in
[`ADMIN_CONSOLE.md`](ADMIN_CONSOLE.md).

## Pick your startup config

| Goal | Config | Command |
|------|--------|---------|
| Local Python, no token | `config/starter.yaml` | `python -m concierge --config config/starter.yaml` |
| Docker, bearer token from `.env` | `config/starter.docker.yaml` | `docker compose up --build` |
| Local echo-server smoke test | `config/minimal.yaml` | `python -m concierge --config config/minimal.yaml` |

**Local (recommended):**

```powershell
python -m concierge init
.\.venv\Scripts\concierge.exe --config config\starter.yaml
# open http://localhost:8765/admin
```

**Docker:**

```bash
docker compose up --build
# paste GATEWAY_TOKEN from .env when the admin UI prompts
# open http://localhost:8765/admin
```

Build the admin SPA into the gateway image path when using Docker without the
Vite dev server:

```bash
python scripts/build_frontend.py --install
```

For frontend development, run the gateway plus `cd frontend && npm run dev` and
open `http://localhost:5173/admin/` (see
[`ADMIN_CONSOLE.md`](ADMIN_CONSOLE.md#local-development)).

## Admin pages

| Page | Path | Purpose |
|------|------|---------|
| **Dashboard** | `/admin/` | Health, readiness, catalog/session counts, active/draft version ids, upstream connection issues |
| **Upstreams** | `/admin/upstreams` | Add/edit MCP servers on the config **draft** |
| **Profiles** | `/admin/profiles` | Name + selectors that publish a curated tool/resource/prompt set |

OAuth admin flows are API-only for now; upstream OAuth is not in the shell UI yet.

## The draft → apply workflow

Nothing you edit in Upstreams or Profiles is live until you **apply** the draft.

1. **Save to draft** — writes your edit into the runtime config store's draft
   version. The gateway keeps serving the previous **active** config.
2. **Pending changes** card — shows the draft id. Click **Apply (no restart)**
   to promote the draft to active and reload adapters in-process (no container
   or process restart).
3. **Rollback config** — reverts active to the previous stored version if an
   apply misbehaves.

Common mistake: creating a profile that references a brand-new upstream before
applying — the catalog does not contain that server yet, so **Preview** shows
0 matches. Apply the upstream draft first, then **Refresh catalog**, then
preview the profile.

## Step-by-step: configure everything in the UI

### 1. Add an upstream

1. Open **Upstreams** → **New upstream**.
2. Set **ID** (stable, case-sensitive — profiles match this exactly).
3. Pick **Transport** and fill connection fields (`URL` for Streamable HTTP,
   `Command` for stdio, etc.). For stdio in Docker, use commands the container
   can run — the default image includes **Python**, **Node/npx**, and
   **uv/uvx**; see
   [`DEPLOYMENT.md`](DEPLOYMENT.md#stdio-upstream-launchers-in-the-runtime-image)
   for `pipx`, compiled binaries, and other launchers.
4. For stdio upstreams, set per-server env in the **Environment** field
   (`NAME: value` per line) or put vars in `.env` (inherited by child processes).
   For remote HTTP upstreams, add headers such as
   `Authorization: Bearer secret_ref:MY_TOKEN`. Put the real secret in `.env`
   as `MY_TOKEN=...` and restart the gateway (or `docker compose restart`) so
   the env var is visible to the process.
5. Click **Test connection** — confirms reachability and reports discovered
   tool/resource/prompt counts.
6. Click **Save to draft**.

### 2. Apply upstream changes

1. In **Pending changes**, click **Apply (no restart)**.
2. Click **Refresh catalog** on the upstream (enabled after the upstream exists
   in active config).

### 3. Create a profile

1. Open **Profiles** → **New profile**.
2. Set **Name** (used by MCP clients as `gateway_use_profile` argument).
3. Optionally enable **Auto-apply at session init** so tools appear in the first
   `tools/list` without a client round-trip.
4. Add a **Selector**:
   - Simplest: **Server** = your upstream ID, leave other fields blank →
     publishes everything from that server.
   - Do **not** put the upstream ID in **Names** — Names is a whitelist of
     *primitive* canonical names (`<id>__<tool>`, e.g. `bridgemind__create_task`).
     See the [Names trap](ADMIN_CONSOLE.md#the-names-trap-common-0-match-cause).
5. Click **Preview** — verify match count and copy canonical names from the table
   if you need a **Names** whitelist.
6. Click **Save to draft** → **Apply (no restart)** in Pending changes.

### 4. Verify on the Dashboard

Refresh the Dashboard. Expect:

- Upstream **connected** under health/readyz issues (none if healthy).
- **Catalog count** > 0 after apply + catalog refresh.
- **Active version** id updated after apply; draft cleared or promoted.

### 5. Confirm from an MCP client

- `gateway_list_servers` — configured upstream ids.
- `gateway_list_profiles` — profile names you created.
- `gateway_list_active_tools` — non-empty when a profile has `auto_apply: true`
  or after the client calls `gateway_use_profile`.
- If the client does not listen for `notifications/tools/list_changed`, use
  **auto-apply** or have the client re-fetch `tools/list` after profile changes.

## Persistence

Admin-panel changes (upstreams, profiles, policy, payload, session pool tuning)
are stored in SQLite, not written back to your starter YAML.

| Environment | Database path | Survives… |
|-------------|---------------|-----------|
| Local `starter.yaml` | `./concierge-runtime-config.db` (process working directory) | Process restart; gitignored |
| Docker `starter.docker.yaml` | `./data/concierge-runtime-config.db` on the **host** | `docker compose down`, image rebuild, container recreate |

**Always read from YAML on startup** (not from the store):

`auth`, `gateway` (host/port/origins), `storage` backend URLs, `log_level`,
`cache`, `output`, `observability`.

**Restored from the store** when a prior active version exists:

`upstream_servers`, `profiles`, `policy`, `payload`, `session_pool`,
`catalog_refresh_interval_s`.

Rotate `GATEWAY_TOKEN` or change bind address in YAML + restart — upstreams and
profiles from the store are preserved.

### Why Docker did not mount the DB originally

The compose file was designed around Twelve-Factor **config** bind-mounts
(`./config` → `/app/config`) so operators could edit YAML without rebuilding.
The runtime config store was added later; it defaulted to a relative path inside
the container working directory (`/app/concierge-runtime-config.db`), which lives
on the **container writable layer**. That layer is discarded when the container
is removed, so admin edits appeared to persist across `restart` but vanished on
`docker compose down` + `up`. The default stack now mounts `./data:/app/data` and
`starter.docker.yaml` sets `storage.catalog_sqlite_path` there.

### Backup

Copy the SQLite file while the gateway is stopped (or accept SQLite WAL
copy semantics):

```bash
# Docker
cp data/concierge-runtime-config.db data/concierge-runtime-config.db.bak

# Local
cp concierge-runtime-config.db concierge-runtime-config.db.bak
```

To reset admin state, stop the gateway and delete the file; the next start uses
YAML-only config until you edit via the UI again.

## Fields not in the UI (YAML only)

The Upstreams form does not expose every upstream knob. Set these in YAML (or
via the upstreams API) if you need them:

- `default_categories`, `default_risk`, `requires_auth`, `isolation`

See [`ADMIN_CONSOLE.md`](ADMIN_CONSOLE.md#upstreams-page-adminupstreams) for the
full field matrix.

## Secrets

- **Gateway admin token** — `GATEWAY_TOKEN` in `.env` (auto-generated by
  `concierge init` / compose `init` service). Required for Docker admin UI.
- **Upstream credentials** — use `secret_ref:ENV_VAR` in header values; define
  `ENV_VAR` in `.env`. Redacted in API responses; preserved on save unless you
  type a new value.

After changing `.env`, restart the gateway process or `docker compose restart
concierge`.

## Further reading

- [`mcp-clients.md`](mcp-clients.md) — MCP session lifecycle, stale-session recovery
- [`ADMIN_CONSOLE.md`](ADMIN_CONSOLE.md) — selector semantics, profile shapes,
  API surface, Vite dev setup
- [`ARCHITECTURE.md`](ARCHITECTURE.md) §14 — merge model at startup
- [`README.md`](../README.md) — install, Docker quickstart, troubleshooting