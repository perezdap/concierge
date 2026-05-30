# Concierge deployment assets

Sample manifests and container definitions for running the MCP gateway in production-like environments.

## Layout

| Path | Purpose |
|------|---------|
| `../Dockerfile` | Multi-stage image (non-root, no baked-in secrets) |
| `../docker-compose.yml` | Local single-node stack on port 8765 |
| `k8s/configmap.yaml` | Gateway YAML mounted at `/etc/concierge/gateway.yaml` |
| `k8s/deployment.yaml` | Deployment with `/healthz` + `/readyz` probes |
| `k8s/service.yaml` | ClusterIP service on port 8765 |

## Kubernetes (quick start)

```powershell
docker build -t concierge:0.1.0 .
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

Probes target `GET /healthz` (liveness) and `GET /readyz` (readiness) on port 8765. Wire bearer tokens and upstream credentials through Kubernetes Secrets and `${VAR}` placeholders in the ConfigMap — never commit secrets to git.

See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) for build, run, and rolling-restart guidance.
