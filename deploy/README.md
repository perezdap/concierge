# Concierge deployment assets

Sample manifests and container definitions for running the MCP gateway in
production-like environments.

## Layout

| Path | Purpose |
|------|---------|
| `../Dockerfile` | Multi-stage image (non-root, hash-locked deps, no baked-in secrets) |
| `../requirements.txt` | Hash-pinned runtime dependency closure (exported from `uv.lock`) |
| `../docker-compose.yml` | Local single-node stack on port 8765 |
| `k8s/configmap.yaml` | Gateway YAML mounted at `/etc/concierge/gateway.yaml` |
| `k8s/deployment.yaml` | Deployment with `/healthz` + `/readyz` probes, drain-aware grace period |
| `k8s/service.yaml` | ClusterIP service on port 8765 |
| `k8s/ingress.yaml` | TLS-terminating nginx ingress (cert from a Secret) |
| `nginx/concierge.conf` | Standalone nginx reverse proxy (TLS termination) |
| `sbom.json` | CycloneDX SBOM of the runtime dependency closure |

## Kubernetes (quick start)

```bash
docker build -t concierge:0.1.0 .
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/ingress.yaml   # TLS terminates here, not in the pod
```

Probes target `GET /healthz` (liveness) and `GET /readyz` (readiness) on port
8765. Wire bearer tokens and upstream credentials through Kubernetes Secrets and
`${VAR}` placeholders — **never commit secrets to git, and never write them to
disk**. TLS is terminated at the ingress / reverse proxy; the gateway speaks
plain HTTP on 8765 inside the cluster network.

See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) for build, lockfile, TLS, secret
handling, vulnerability-scan, and graceful rolling-restart guidance.
