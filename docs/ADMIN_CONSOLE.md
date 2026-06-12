# Admin Console

> **Operator walkthrough:** [`GETTING_STARTED.md`](GETTING_STARTED.md) — boot,
> draft/apply, upstreams, profiles, persistence, MCP client checks.
>
> Quick start: [README.md](../README.md#admin-console) for install commands.

Browser UI for operating a Concierge gateway: health, upstream status, catalog
counts, sessions, and runtime config version. It also ships two config editor
pages — **Upstreams** (`/admin/upstreams`) and **Profiles** (`/admin/profiles`) —
documented under [Configuring upstreams and profiles](#configuring-upstreams-and-profiles).
Served under `/admin/` as a Vite-built React SPA.

## Prerequisites

- Node.js 20+ and npm (local dev / `scripts/build_frontend.py`)
- Concierge gateway with `auth.type: localhost` for loopback dev, or bearer
  tokens for remote access

## Local development

1. Start the gateway (localhost auth example):

   ```powershell
   .venv\Scripts\concierge.exe --config config\starter.yaml
   ```

2. Install and run the admin dev server (proxies API to `:8765`):

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

3. Open `http://localhost:5173/admin/` — the Vite dev server proxies
   `/admin/*`, `/healthz`, and `/readyz` to the gateway.

### Auth modes

| Mode | When | Behavior |
|------|------|----------|
| **localhost** | Host is `localhost` / `127.0.0.1` | No `Authorization` header; gateway `LocalhostAllowAuth` accepts the connection |
| **bearer** | Remote host or manual switch | Prompts once per session; token stored in `sessionStorage` and sent on all `api.ts` requests |

Toggle mode from the header bar. Do not paste production secrets into shared machines.

## Configuring upstreams and profiles

The **Upstreams** and **Profiles** pages edit the runtime config *draft*. Nothing
you change here is live until you apply the draft from the **Pending changes**
card on those pages (**Apply (no restart)** promotes the draft and reloads
adapters in-process). **Rollback config** reverts to the previous active version.

Numbered workflow: [`GETTING_STARTED.md`](GETTING_STARTED.md#step-by-step-configure-everything-in-the-ui).
Field reference below mirrors `frontend/src/pages/Upstreams.tsx` and
`frontend/src/pages/Profiles.tsx`.

### Mental model

An upstream is cataloged into normalized entries; a profile's selectors *filter*
that catalog down to the set published to a session:

```
upstream (id, default_tags, default_categories)
      │   adapter discovers + sanitizes each primitive
      ▼
catalog entry
   canonical_name = "<upstream-id>__<primitive>"   e.g. bridgemind__create_task
   tags       = upstream default_tags
   categories = upstream default_categories
      │
      │   profile selectors filter
      │     • AND within one selector (all non-blank fields must match)
      │     • OR  across selectors    (any selector matching publishes the entry)
      ▼
published set ──► tools/list · resources/list · prompts/list  (this session)
```

Simplest possible profile: a single selector with **Server** = `bridgemind` and
every other field blank publishes *everything* from the `bridgemind` upstream. A
blank field is a wildcard (no constraint) — not an empty set.

### Upstreams page (`/admin/upstreams`)

Each upstream is selected from the left list; the editor shows only the fields
relevant to the chosen transport.

| Field | Type / values | Notes |
|-------|---------------|-------|
| **ID** | string, required | Stable identifier for the upstream. Immutable after creation (the input is disabled when editing). This is the exact string a profile selector's **Server** field matches against, and it prefixes every `canonical_name` (`<id>__<primitive>`). |
| **Transport** | `stdio` · `streamable_http` · `sse_legacy` · `custom` | Selects which connection fields render below. |
| **Command** | space-separated argv | `stdio` only. e.g. `python -m my_server`. |
| **Environment** | textarea, one `NAME: value` per line | `stdio` only. Merged into the child process environment (after inheriting the gateway's own env). Leave blank when the MCP server reads vars already in the gateway `.env`. Values are **redacted** in API responses; preserved on save unless you replace them. |
| **URL** | string | `streamable_http` only. The MCP endpoint. |
| **SSE URL** / **POST URL** | string each | `sse_legacy` only. Event stream URL and the message POST URL. |
| **Custom kind** | string | `custom` only. The key of a registered custom-transport adapter. |
| **Headers** | textarea, one `Name: value` per line | `streamable_http` / `sse_legacy` only. Values may use `secret_ref:VAR` indirection (resolved from the environment/secret store) and are **redacted** in API responses. Existing write-only/redacted values are preserved on save unless you type a new value — a hint appears when redacted values are present. |
| **Default tags** | comma-separated | Propagated onto **every** primitive cataloged from this upstream — they become each entry's `tags`, which a profile selector's **Tags** field matches against. |

Buttons: **Save to draft**, **Test connection** (probes the upstream and reports
latency + discovered tool/resource/prompt counts), **Refresh catalog** (re-reads
the upstream's primitives; disabled for an unsaved new upstream — apply the draft
first), **Delete** (existing upstreams only).

> **Not editable here:** `default_categories`, `default_risk`, `requires_auth`,
> and `isolation` are real upstream settings but the form does not render inputs
> for them — set them in `config/gateway.yaml` (or via the upstreams API). The
> values still take effect; they're just config-only for now.

### Profiles page (`/admin/profiles`)

A profile is a name plus a list of *selectors*. Selectors are resolved against
the **live** catalog at apply time, so a profile keeps working even if an
upstream is reconfigured.

| Field | Type / values | Notes |
|-------|---------------|-------|
| **Name** | string, required | Identifier passed to `gateway_use_profile`. Immutable after creation. |
| **Auto-apply at session init** | checkbox | When on, the profile's published set is applied at session initialize, so its tools appear in the *first* `tools/list` with no discover/enable round-trip. |
| **Description** | string | Free text. |

Each **Selector** (add/remove with the buttons; a profile may have many):

| Selector field | Type / values | Match semantics |
|----------------|---------------|-----------------|
| **Server** | string | Exact, **case-sensitive** match against an upstream **ID**. Blank = any server. |
| **Primitive type** | `any` · `tool` · `resource` · `prompt` | `any` (blank) applies no type filter. |
| **Tags** | comma-separated | Entry must carry **all** listed tags (case-insensitive subset). Matches against the entry's `tags` (i.e. the upstream's **Default tags**). Blank = no tag filter. |
| **Categories** | comma-separated | Same subset semantics as Tags, against the entry's `categories`. Blank = no category filter. |
| **Names** | comma-separated | A **whitelist of primitive names** matched against either the canonical form (`<upstream-id>__<primitive>`, e.g. `bridgemind__create_task`) **or** the bare upstream name (`create_task`). Either form works. Blank = any name. **This is not a server/upstream id** — see the trap below. |

Buttons: **Add selector**, **Save to draft**, **Preview**, **Duplicate**
(existing), **Delete** (existing). **Preview** resolves the selectors against the
live catalog and shows the match count, per-selector warnings (e.g. *"selector 0
matched no catalog entries"* with the offending fields), and a table of the first
50 matched primitives (Name / Server / Kind).

#### Selector matching semantics

- **Within one selector, all non-blank fields AND together** — a primitive must
  satisfy every field you filled in to match that selector.
- **Across selectors, results OR together** — a primitive is published if *any*
  selector in the profile matches it.
- **A blank field is a wildcard**, never an empty set.
- **Server** is an exact, case-sensitive comparison against the upstream ID.
- **Tags / Categories** require the entry to contain *all* listed values
  (case-insensitive subset), matched against the values the upstream contributes
  via its Default tags / categories.
- **Names** matches against either the entry's `canonical_name` (the `<id>__<primitive>`
  form shown in the Preview table's **Name** column) **or** the bare `upstream_name`
  (e.g. `create_task`). Both forms are accepted — canonical is unambiguous across
  servers; upstream name is shorter but may collide if two servers expose the same
  primitive name.

#### The Names trap (common 0-match cause)

Putting the **upstream's id** into **Names** matches nothing, because Names is a
whitelist of *primitive* names, not a server label. For example:

```
Selector
  Server: bridgemind
  Names:  bridgemind        ← WRONG — this is a primitive-name whitelist, not a server id
```

Preview returns `Matched 0 primitives` with a warning like
`selector 0 matched no catalog entries (server=bridgemind, names=['bridgemind'])`.

**Fix:** clear **Names** to publish everything from that server, or list real
primitive names such as `bridgemind__create_task` (canonical) or `create_task`
(upstream name) — copy canonical names from the Preview table to be safe.

### Choosing profile shapes

- **One auto-apply profile (simplest dev setup).** A single profile with
  `auto_apply` on publishes a fixed set at session init — ideal when every client
  should see the same tools and you don't want a discover/enable round-trip.
- **Several on-demand profiles (per-client scoping).** Leave `auto_apply` off and
  have clients call `gateway_use_profile` to swap published sets mid-session. Each
  switch emits `notifications/tools/list_changed`; the client must re-fetch
  `tools/list` for newly published tools to become callable.
- **Tag-based themed profiles (cross-upstream bundles).** Use a **Tags** selector
  with no **Server** to gather primitives across multiple upstreams that share a
  Default tag — handy for capability bundles like `read-only` or `support`.

Common mistakes to avoid:

- Putting the upstream id/name in **Names** (see the Names trap above).
- Referencing a brand-new upstream before applying the draft — the catalog won't
  contain it yet, so selectors match nothing. Apply, then **Refresh catalog**.
- Case-mismatching **Server** — it is exact, case-sensitive.

## Production build

```powershell
python scripts/build_frontend.py --install
```

Output: `frontend/dist/` (static assets with `base: /admin/`).

### Docker image

The root `Dockerfile` builds the admin SPA in a Node stage and copies
`frontend/dist` into `/app/admin-ui`. The same Node toolchain (`node`, `npm`,
`npx`) is copied into the **runtime** stage so stdio upstreams can launch
npm-published MCP servers (e.g. `npx -y firecrawl-mcp`). **uv** and **uvx**
(0.8.22) are copied from `ghcr.io/astral-sh/uv` for PyPI MCP servers (e.g.
`uvx mcp-server-fetch`). Python stdio commands (`python -m …`) work via the
Python base image. See
[`DEPLOYMENT.md`](DEPLOYMENT.md#stdio-upstream-launchers-in-the-runtime-image)
for other launchers (`pipx`, compiled binaries).

The gateway auto-mounts the built admin UI at `/admin/` when
`frontend/dist/index.html` or `/app/admin-ui/index.html` is present.

For local smoke tests without rebuilding the gateway image, use the Vite dev
server (proxies API to `:8765`):

```powershell
cd frontend
npm run dev
```

Open `http://localhost:5173/admin/` (proxies API to the gateway on `:8765`).

## API surface used by the dashboard

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness + upstream snapshot |
| `GET /admin/health` | Catalog/session counts + per-server health |
| `GET /admin/catalog` | Primitive count |
| `GET /admin/sessions` | Active sessions |
| `GET /admin/config/status` | Active/draft version ids + reload coordinator flag |

OAuth admin flows are intentionally not exposed in the shell (upstream OAuth is
API-only until dedicated UI pages ship).

## Extending the UI

- Shared HTTP client: `frontend/src/api.ts`
- Auth helpers: `frontend/src/auth.ts`
- Add routes in `frontend/src/App.tsx` (e.g. `Upstreams.tsx`, `Profiles.tsx`)

Pending config diff UI: `frontend/components/PendingChanges.tsx` (import from
config pages when ADMIN-5/6 land).