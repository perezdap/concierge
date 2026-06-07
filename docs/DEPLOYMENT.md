# Deployment guide

This document describes how to build, run, and roll out the Concierge MCP gateway
using the sample container and Kubernetes assets in this repository (P1-7).

## Prerequisites

- Docker 24+ (or compatible container runtime)
- Python 3.11+ only required for local dev; the image bundles the runtime
- For Kubernetes: `kubectl` 1.28+ and a cluster with an ingress controller or
  external load balancer in front of the Service
- For regenerating the lockfile: [`uv`](https://docs.astral.sh/uv/) 0.4+

## Dependencies are fully pinned and locked

The app tier is reproducible from a lockfile, not floor pins:

- `pyproject.toml` declares loose, human-edited ranges (e.g. `fastapi>=0.110`).
- `uv.lock` is the authoritative, fully-resolved lock (exact versions + hashes
  for every transitive dependency). It is committed.
- `requirements.txt` is a hash-pinned export of the runtime closure
  (`core + persistence + observability` extras) generated from the lock. The
  Docker image installs **only** from this file with `pip install
  --require-hashes`, so the build fails closed if any pin or hash is missing or
  altered.

Regenerate after changing dependencies:

```bash
uv lock                                    # update uv.lock from pyproject.toml
uv export --no-emit-project --no-dev \
  --extra persistence --extra observability \
  --format requirements-txt -o requirements.txt
```

Commit `uv.lock` and `requirements.txt` together. Never hand-edit
`requirements.txt`.

## Build the image

From the repository root:

```bash
docker build -t concierge:0.1.0 .
```

The Dockerfile uses a **multi-stage** build: a builder stage installs the
hash-locked dependencies and the package; the runtime stage copies the installed
site-packages and runs as the non-root user **UID 10001** (`concierge`). The base
image is pinned to `python:3.12.8-slim-bookworm` by **immutable digest** —
re-pin the digest when upgrading Python patch releases.

No API keys, bearer tokens, or other secrets are ever copied into the image.
Configuration is supplied at run time (see *Secret handling* below).

### Vulnerability scanning

The dependency closure is scanned in CI with `pip-audit` (Python advisories):

```bash
pip-audit -r requirements.txt        # current result: no known vulnerabilities
```

Scan the built **container image** (OS packages + Python deps) with Trivy or
Grype before promoting a tag to production:

```bash
trivy image --severity HIGH,CRITICAL --exit-code 1 concierge:0.1.0
# or
grype concierge:0.1.0 --fail-on high
```

A CycloneDX SBOM of the runtime dependency closure is committed at
[`deploy/sbom.json`](../deploy/sbom.json) and can be regenerated with:

```bash
cyclonedx-py requirements requirements.txt --output-format JSON -o deploy/sbom.json
```

Feed the SBOM to your scanner of choice for air-gapped or registry-side scanning:

```bash
trivy sbom deploy/sbom.json
grype sbom:deploy/sbom.json
```

## Secret handling — secrets never touch disk or git

This is a hard rule, not a guideline:

- **No secret is ever committed to the repository.** The image and manifests
  contain only non-sensitive defaults.
- **No secret is ever written to disk.** Bearer tokens, upstream credentials,
  and database/Redis URLs are injected as **environment variables** sourced from
  a secret manager (Kubernetes Secrets, HashiCorp Vault, AWS/GCP Secrets
  Manager, etc.) and consumed in memory only.
- Config YAML uses `${VAR}` / `${VAR:-default}` placeholders; `load_config`
  expands them from the process environment at startup. A `${VAR}` with no
  default that is unset is a startup error — secrets cannot silently fall back
  to a literal placeholder.

Example: reference an upstream token by env var in the gateway YAML, then map a
Secret key to that env var — the value lives only in the container's
environment, never in the ConfigMap or on a volume:

```yaml
upstream_servers:
  - id: notes
    transport: streamable_http
    url: https://notes.internal/mcp
    headers:
      Authorization: "Bearer ${NOTES_TOKEN}"
```

```yaml
# Deployment patch — inject from a Secret, not a file or ConfigMap.
env:
  - name: NOTES_TOKEN
    valueFrom:
      secretKeyRef:
        name: concierge-upstreams
        key: notes-token
```

Create the Secret out-of-band (or via your secret manager's operator) — never
from a committed manifest:

```bash
kubectl create secret generic concierge-upstreams \
  --from-literal=notes-token="$NOTES_TOKEN"
```

## Run with Docker Compose

```bash
docker compose up --build
```

This starts the gateway with `config/starter.docker.yaml`, publishing **8765**
on the host. Admin-panel state persists in `./data/concierge-runtime-config.db`
(bind mount `./data:/app/data`). The compose health check mirrors the k8s
liveness probe (`GET /healthz`). `stop_grace_period` is set above the app's
drain window so `docker compose down` drains in-flight calls before SIGKILL.

Operator walkthrough: [`GETTING_STARTED.md`](GETTING_STARTED.md).

Point MCP clients at `http://127.0.0.1:8765/mcp`. For a local smoke test of
readiness:

```bash
curl -s http://127.0.0.1:8765/healthz   # {"ok": true}
curl -s http://127.0.0.1:8765/readyz    # {"ok": true} once upstreams connect
```

## Run with plain Docker

```bash
docker run --rm -p 8765:8765 concierge:0.1.0
```

Override configuration by mounting a YAML file (never bake secrets into the
image), and inject secrets via `-e` / `--env-file`:

```bash
docker run --rm -p 8765:8765 \
  -e NOTES_TOKEN \
  -v "$PWD/config/gateway.example.yaml:/app/config/gateway.yaml:ro" \
  concierge:0.1.0 --config /app/config/gateway.yaml
```

## Twelve-Factor compose (no-duplicate operator workflow)

[Twelve-Factor App](https://12factor.net/config) requires config to live in
the **environment**, not the codebase. The compose file in this repo follows
that model so operators can spin up, reconfigure, and roll Concierge without
editing the compose file, rebuilding the image, or duplicating values across
files.

The defaults work out of the box:

```bash
docker compose up --build
```

This starts the gateway with `config/starter.docker.yaml` (minimal admin-first
config; bearer auth via `GATEWAY_TOKEN` in `.env`), exposes **8765**, and
persists runtime config under `./data/`. Switch to `config/gateway.example.yaml`
via a `docker-compose.override.yml` when you want the full example upstreams.
Use bare `${VAR}` (no default) in your own configs when a secret must be present
at startup.

### Customizing the config (no image rebuild)

Edit the YAML directly under `config/` — the compose file bind-mounts
`./config:/app/config:ro`, so any change there is picked up on the next
`docker compose up --force-recreate`:

```bash
$EDITOR config/gateway.example.yaml     # tweak the comprehensive example
docker compose up --force-recreate      # picks up the change
```

To use a separate, gitignored config of your own:

```bash
cp config/gateway.example.yaml config/gateway.yaml   # gitignored
$EDITOR config/gateway.yaml
# Override the gateway's --config argument via a Compose override file:
cat > docker-compose.override.yml <<'YAML'
services:
  concierge:
    command: ["--config", "/app/config/gateway.yaml"]
YAML
docker compose up --force-recreate
```

`config/gateway.yaml` is gitignored (see `.gitignore`) so it never gets
committed — that's the file that holds your environment-specific config.

### Injecting secrets via `.env` (Twelve-Factor §III)

Compose auto-loads `./.env` (next to `docker-compose.yml`) into the gateway's
process environment at startup. `load_config()` then expands `${VAR}` /
`${VAR:-default}` placeholders from that environment into your YAML. **A
`${VAR}` with no default that is unset is a startup error** — secrets cannot
silently fall back to a literal placeholder.

If a `${VAR}` (no default) is set to the *empty string* (e.g. you copied
`.env.example` to `.env` and forgot to fill in `BM_LIVE_TOKEN=`), startup
**fails** with an error naming every offending variable. Use `${VAR:-}`
for an intentional empty substitution (example configs use this for
optional upstream credentials you have not filled in yet).

#### Upgrade note (breaking change)

Previous releases logged a **WARNING** and continued with the empty value.
That warning is now a hard startup error. Migration:

| Before (`.env`)              | After (pick one)                                      |
| ---------------------------- | ----------------------------------------------------- |
| `BM_LIVE_TOKEN=` (blank)     | Set a real token: `BM_LIVE_TOKEN=your-token-here`     |
| Intentionally empty in YAML  | Change `${BM_LIVE_TOKEN}` → `${BM_LIVE_TOKEN:-}`      |

No `CONCIERGE_ALLOW_EMPTY_ENV` escape hatch is provided — the `${VAR:-}`
form is the supported migration path for operators who legitimately want
an empty value.

```bash
cp .env.example .env            # gitignored
$EDITOR .env                    # fill in BM_LIVE_TOKEN, NOTES_TOKEN, etc.
docker compose up --force-recreate
```

`.env.example` (committed) is the template; `.env` (gitignored) is your
local, populated copy. Never commit a populated `.env`.

For Kubernetes, see *Secret handling* above — the same `${VAR}` mechanism
maps onto Secret-backed env vars via the deployment manifest.

### Where each value comes from

| Source          | Examples                              | Notes                                       |
| --------------- | ------------------------------------- | ------------------------------------------- |
| `.env`          | `BM_LIVE_TOKEN`, `NOTES_TOKEN`        | Secrets. Gitignored. Never committed.       |
| `environment:`  | `LOG_LEVEL`                           | Non-secret defaults in `docker-compose.yml`.|
| `config/*.yaml` | `gateway.host`, `upstream_servers[]`  | Committed examples + gitignored `gateway.yaml`. |
| Image           | `/app/config/gateway.example.yaml`    | The committed example baked at build time.  |

If a value needs to be different per environment (dev / staging / prod), it
belongs in `.env` or in your `config/gateway.yaml`, **not** in the compose
file or the image. That's the whole point.

### Why a bind mount, not `image: build`

Rebuilding the image on every config edit would (1) require a Docker daemon
in the deploy pipeline, (2) leak secret values into image layers if you're
not careful, and (3) make `docker compose config` non-reproducible. A
read-only bind mount keeps config out of the image, the same way Kubernetes
keeps it out of the pod spec via a ConfigMap + Secret.

## Kubernetes

Apply manifests in order:

```bash
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/ingress.yaml      # TLS termination (see below)
```

### Probes

| Probe | Path | Port | Purpose |
|-------|------|------|---------|
| Liveness | `/healthz` | 8765 | Process is up; restart if failing |
| Readiness | `/readyz` | 8765 | Ready for traffic; reports 503 while upstreams are disconnected **or while the pod is draining** |

These endpoints are implemented by the gateway observability work (P1-6). During
a graceful drain (P1-7) `/readyz` returns 503 so the endpoints controller stops
routing new connections to the terminating pod.

### Configuration

The ConfigMap ships a minimal gateway config with `bind_public: true` and
`host: 0.0.0.0` so pods accept traffic from the cluster network. The app tier is
**stateless** (Postgres catalog + Redis sessions, P1-2); the image bakes in no
stateful config. Patch upstream URLs and auth via the ConfigMap; inject secrets
via Secret-backed env vars referenced as `${VAR}` in the YAML (see *Secret
handling*). Never place secret values in the ConfigMap.

### Resource limits

The sample Deployment requests `100m` CPU / `256Mi` memory and limits `1` CPU /
`512Mi`. Tune for your catalog size and concurrent session load.

## TLS termination

**Terminate TLS at the edge — never in the gateway pod.** The gateway listens on
plain HTTP `:8765` inside the cluster/host network; do not expose that port
directly on the public internet.

### Kubernetes ingress (nginx)

`deploy/k8s/ingress.yaml` terminates TLS at an nginx ingress and proxies plain
HTTP to the Service. Key points:

- The cert lives in a Kubernetes `Secret` (`concierge-tls`) created by
  cert-manager or your PKI — **never committed**.
- Long-lived SSE GET streams (MCP notifications) require raised proxy read
  timeouts and disabled buffering (annotations are set in the manifest).

```bash
# cert-manager issues/rotates the cert into the referenced Secret, or:
kubectl create secret tls concierge-tls --cert=tls.crt --key=tls.key
kubectl apply -f deploy/k8s/ingress.yaml
```

### Standalone nginx reverse proxy

For non-Kubernetes deployments, `deploy/nginx/concierge.conf` is a ready-to-use
reverse-proxy server block: HTTP→HTTPS redirect, TLS 1.2/1.3, SSE-friendly
timeouts, and `proxy_pass` to the gateway on 8765. Mount the cert/key read-only
from a secret manager at deploy time — do not commit them.

## Rolling restart and graceful drain (P1-7)

On SIGTERM the app drains gracefully (lifespan shutdown in
`src/concierge/server/app.py`):

1. A SIGTERM handler flips the drain flag immediately, so `/readyz` reports 503
   and the load balancer steers new traffic away.
2. The facade refuses brand-new sessions (`initialize` without a session header)
   with HTTP **503** + `Retry-After`, so clients reconnect to a healthy replica.
3. Already-dispatched, in-flight calls run to completion. Shutdown blocks on
   `wait_for_idle` up to `session_pool.drain_grace_period_s` (default **25s**)
   before tearing down upstream adapters, the catalog store, and the Redis
   session client.

Tune the window with `session_pool.drain_grace_period_s`. In Kubernetes it must
be **less than** `terminationGracePeriodSeconds` (set to 40s in the sample
Deployment, with a 5s `preStop` sleep so the endpoints controller observes the
readiness flip before SIGTERM is delivered).

```bash
kubectl rollout restart deployment/concierge-gateway
kubectl rollout status deployment/concierge-gateway
```

With `replicas: 2`, new pods must pass `/readyz` before old pods finish
draining, so in-flight MCP calls are not dropped during the rollout.

For Docker Compose:

```bash
docker compose up -d --build --force-recreate
```

The zero-drop drain behavior is covered by `tests/test_graceful_drain.py`, which
drives a slow in-flight call across the SIGTERM boundary and asserts it completes
while the new-session path returns 503.

## Distributed rate limiting (P1-4)

The policy engine rate-limits every tool call with a token bucket keyed by
`(tenant, session, tool)`. Two backends share one interface
(`src/concierge/policy/ratelimit.py`):

| Backend  | State            | Scope                          | When to use                         |
| -------- | ---------------- | ------------------------------ | ----------------------------------- |
| `memory` | in-process dict  | per-replica (each enforces own)| single replica / dev (**default**)  |
| `redis`  | Redis hash + TTL | **global** across all replicas | multi-replica / stateless app tier  |

In-memory is the default and the fallback whenever no Redis URL is resolvable.
Because each replica keeps its own buckets, a 3-replica deployment under
in-memory effectively allows ~3× the configured limit. Use the Redis backend in
any multi-replica deployment so the limit holds globally.

### Enabling the Redis backend

```yaml
storage:
  redis_url: ${REDIS_URL}          # P1-2 already sets this for sessions; reused here

policy:
  rate_limit_capacity: 30          # default bucket size (tokens)
  rate_limit_refill_per_sec: 0.5   # default refill rate (tokens/sec)
  ratelimit:
    backend: redis                 # "memory" (default) | "redis"
    # redis_url: redis://...       # optional; falls back to storage.redis_url
    tenant_quotas:                 # optional per-tenant overrides
      acme:    { capacity: 120, refill_per_sec: 2.0 }
      free:    { capacity: 10,  refill_per_sec: 0.1 }
```

The same Redis that backs P1-2 sessions backs P1-4 limits — no second instance
is required. Set `policy.ratelimit.backend: memory` (or omit it) to disable the
shared limiter.

### Key schema

Buckets are stored under a **versioned** key so the encoding can evolve without
colliding with old data:

```
rl:v1:{tenant}:{session}:{tool}
```

`v1` is `KEY_SCHEMA_VERSION` in `ratelimit.py`; bump it on any
backward-incompatible change (old keys then simply expire). Each key is a Redis
hash `{tokens, ts}` with a TTL of `ceil(capacity / refill) + 60` seconds, so
abandoned `(tenant, session, tool)` tuples are reclaimed automatically and the
keyspace stays bounded by *active* buckets.

### Atomicity

Refill-and-take runs as a single Lua script (`EVALSHA`, falling back to `EVAL`
on `NOSCRIPT`). Doing the read-modify-write server-side makes it atomic across
every replica targeting the same Redis, so concurrent replicas can never
overspend a bucket — a plain `GET`/`SET` would race and double-spend. This is
verified by `tests/test_ratelimit_redis.py` (two concurrent clients against one
bucket assert total grants == capacity exactly).

### Expected Redis ops/sec impact

Each tool call costs **one** round trip (the Lua script does HMGET + HSET +
EXPIRE atomically server-side, counted as a single client command). So
limiter load ≈ your tool-call rate: at 1,000 tool-calls/sec the limiter adds
~1,000 ops/sec to Redis. The script touches one key per call and stores two
small fields, so memory is `≈ active_bucket_count × ~120 bytes`. A modest Redis
(shared with P1-2 sessions) handles tens of thousands of ops/sec comfortably;
the limiter client uses a bounded blocking connection pool (`max_connections=64`
by default) so bursts queue briefly rather than exhausting connections.

### Sizing per-tenant quotas

- **capacity** = the maximum *burst* a tenant can fire instantly (bucket starts
  full). Set it to the largest reasonable spike you want to absorb.
- **refill_per_sec** = the sustained *steady-state* rate. Sustained throughput
  converges to `refill_per_sec` calls/sec/tool once the burst budget is spent.
- **Retry-After**: on denial the gateway returns the `GW_RATE_LIMITED`
  (`-32003`) JSON-RPC error with `data.retry_after` set to whole seconds until
  the bucket can grant the call (`ceil(deficit / refill_per_sec)`). A
  never-refilling bucket (`refill_per_sec: 0`) reports a large finite backoff.

Start tenants at the default quota and raise `tenant_quotas` overrides for
high-volume customers; lower them for free/abusive tiers. Remember the bucket is
per `(tenant, session, tool)`, so a tenant's effective ceiling scales with its
concurrent sessions and the number of distinct tools it calls.

## Validation

- Manifest structure: `tests/test_deploy_manifests.py` (YAML parse, probe paths,
  resource limits, ConfigMap mount).
- Graceful drain: `tests/test_graceful_drain.py`.
- Rate limiting: `tests/test_ratelimit_memory.py` (unit),
  `tests/test_ratelimit_redis.py` (Redis integration + atomicity + load),
  `tests/test_ratelimit_wiring.py` (error envelope, metrics, backend selection).
  Run the Redis suite against the P1-2 test stack:

  ```bash
  docker compose -f docker-compose.test.yml up -d redis
  pytest -q tests/test_ratelimit_redis.py
  docker compose -f docker-compose.test.yml down
  ```

After deploying, verify:

```bash
curl https://concierge.example.com/healthz
curl https://concierge.example.com/readyz
```

## Security notes

- Run as non-root (enforced in Dockerfile and Deployment `securityContext`).
- Never commit bearer tokens or upstream credentials; inject from a secret
  manager as env vars (see *Secret handling*).
- Restrict `allowed_origins` to real client origins before production exposure.
- Scan images in CI with `pip-audit` (deps) and Trivy/Grype (image), and keep
  `deploy/sbom.json` in sync with the lockfile.

### Authentication providers (P1-1)

Concierge supports a config-selectable provider chain — static bearer, OIDC/OAuth2,
mTLS pass-through, and per-tenant minted tokens — with a shared revocation list
enforced on every request. See **`docs/AUTH.md`** for the full configuration
reference. Deployment-relevant points:

- **Shared state is mandatory in prod.** The revocation list and tenant-token
  store must use `redis` or `postgres`, never `memory` (it does not survive a
  replica restart and is invisible fleet-wide). They fall back to
  `storage.redis_url` / `storage.catalog_postgres_url` when their own URLs are
  unset:

  ```yaml
  auth:
    providers: [oidc, tenant_token, bearer]
    revocation:   { backend: redis }
    tenant_token: { backend: redis }
  ```

- **mTLS pass-through trusts forwarded headers — gate the network.** When the
  `mtls` provider is enabled, the gateway reads the client-cert identity from a
  proxy-supplied header (`X-Forwarded-Client-Cert` / nginx `ssl-client-*`). These
  headers are forgeable by any direct client, so the provider honors them **only**
  when the peer IP is inside `mtls.trusted_proxy_cidrs` (empty = trust nobody).
  You must therefore (1) ensure the gateway is reachable **only** from the
  terminating proxy — keep it off public networks behind a NetworkPolicy / the
  loopback `bind_public=false` default — and (2) configure the proxy to **strip**
  any client-supplied copy of those headers before injecting its own validated
  values.

- **OIDC needs the `auth` extra** (`pyjwt[crypto]`), already present in the
  published image and `requirements.txt`. id_token bytes are never logged.

- **Revoking a token** is keyed by `token_id` (`jti` for OIDC). Use the admin path
  (`POST /admin/revocations`) or `python -m concierge token … revoke`. With a
  shared backend the revocation propagates to every replica on the next request.
