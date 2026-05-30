# Deployment guide

This document describes how to build, run, and roll out the Concierge MCP gateway using the sample container and Kubernetes assets in this repository.

## Prerequisites

- Docker 24+ (or compatible container runtime)
- Python 3.11+ only required for local dev; the image bundles the runtime
- For Kubernetes: `kubectl` 1.28+ and a cluster with an ingress or load balancer in front of the Service

## Build the image

From the repository root:

```powershell
docker build -t concierge:0.1.0 .
```

The Dockerfile uses a **multi-stage** build: dependencies install in a builder stage; the runtime stage copies the installed package and runs as UID **10001** (`concierge`). No API keys or bearer tokens are copied into the image — configure secrets via environment variables at run time.

The base image is pinned to `python:3.12.8-slim-bookworm` with an immutable digest. Re-pin the digest when upgrading Python patch releases.

## Run with Docker Compose

```powershell
docker compose up --build
```

This starts the gateway with `config/gateway.example.yaml`, publishing **8765** on the host. The compose health check calls `GET http://127.0.0.1:8765/healthz` inside the container.

Point MCP clients at `http://127.0.0.1:8765/mcp`.

## Run with plain Docker

```powershell
docker run --rm -p 8765:8765 concierge:0.1.0
```

Override configuration by mounting a YAML file (never bake secrets into the image):

```powershell
docker run --rm -p 8765:8765 `
  -v ${PWD}/config/gateway.example.yaml:/app/config/gateway.yaml:ro `
  concierge:0.1.0 --config /app/config/gateway.yaml
```

Use `${VAR}` / `${VAR:-default}` placeholders in YAML; `load_config` expands them from the container environment. Map Kubernetes Secrets or Docker env files to those variables.

## Kubernetes

Apply manifests in order:

```powershell
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

### Probes

| Probe | Path | Port |
|-------|------|------|
| Liveness | `/healthz` | 8765 |
| Readiness | `/readyz` | 8765 |

These endpoints are implemented by the gateway observability work (P1-6). Until they exist in your build, probes will fail — deploy the health endpoints before promoting to production.

### Configuration

The ConfigMap ships a minimal gateway config with `bind_public: true` and `host: 0.0.0.0` so pods accept traffic from the cluster network. Patch upstream URLs, auth, and secrets via:

1. Editing the ConfigMap and rolling the Deployment, or
2. Mounting additional Secret keys as env vars referenced in the YAML (`${NOTES_TOKEN}`, etc.)

### Resource limits

The sample Deployment requests `100m` CPU / `256Mi` memory and limits `1` CPU / `512Mi`. Tune for your catalog size and concurrent session load.

### TLS termination

Terminate TLS at an Ingress or external load balancer. The gateway listens plain HTTP on 8765 inside the pod; do not expose that port directly on the public internet without a proxy.

## Rolling restart

Kubernetes performs a rolling update when the pod template changes:

```powershell
kubectl rollout restart deployment/concierge-gateway
kubectl rollout status deployment/concierge-gateway
```

With `replicas: 2`, new pods must pass `/readyz` before old pods terminate, giving in-flight MCP sessions time to drain on SIGTERM (uvicorn lifespan shutdown).

For Docker Compose:

```powershell
docker compose up -d --build --force-recreate
```

## Validation

Manifest structure is covered by `tests/test_deploy_manifests.py` (YAML parse, probe paths, resource limits, ConfigMap mount).

After deploying, verify:

```powershell
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/readyz
```

## Security notes

- Run as non-root (enforced in Dockerfile and Deployment `securityContext`).
- Never commit bearer tokens; use secret managers or cluster Secrets.
- Restrict `allowed_origins` to real client origins before production exposure.
- Scan images in CI with `pip-audit` and a container scanner (Trivy, Grype, etc.).
