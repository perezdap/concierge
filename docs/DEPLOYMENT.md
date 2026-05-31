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

This starts the gateway with `config/gateway.example.yaml`, publishing **8765**
on the host. The compose health check mirrors the k8s liveness probe
(`GET /healthz`). `stop_grace_period` is set above the app's drain window so
`docker compose down` drains in-flight calls before SIGKILL.

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

## Validation

- Manifest structure: `tests/test_deploy_manifests.py` (YAML parse, probe paths,
  resource limits, ConfigMap mount).
- Graceful drain: `tests/test_graceful_drain.py`.

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
