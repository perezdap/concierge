# Admin Console

Browser UI for operating a Concierge gateway: health, upstream status, catalog
counts, sessions, and runtime config version. Served under `/admin/` as a
Vite-built React SPA.

## Prerequisites

- Node.js 20+ and npm (local dev / `scripts/build_frontend.py`)
- Concierge gateway with `auth.type: localhost` for loopback dev, or bearer
  tokens for remote access

## Local development

1. Start the gateway (localhost auth example):

   ```powershell
   .venv\Scripts\concierge.exe --config config\minimal.yaml
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

## Production build

```powershell
python scripts/build_frontend.py --install
```

Output: `frontend/dist/` (static assets with `base: /admin/`).

### Docker image

The root `Dockerfile` includes an optional `admin-ui` stage that runs
`build_frontend.py` and copies `frontend/dist` into `/app/admin-ui` in the
runtime image. **Serving** those files still requires either:

- FastAPI static mount at `/admin` (gateway `app.py` wiring), or
- Reverse proxy `location /admin/` → `alias` / `root` to the dist directory

Until in-process static mount lands, use the Vite preview server behind your
ingress for smoke tests:

```powershell
cd frontend
npm run preview -- --host 127.0.0.1 --port 5173
```

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