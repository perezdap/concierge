"""Kubernetes deployment manifest validation (P1-7)."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"
MANIFEST_FILES = ("configmap.yaml", "deployment.yaml", "service.yaml")


def _load_k8s_documents() -> list[dict]:
    """Parse every YAML document in the deploy/k8s manifests."""
    documents: list[dict] = []
    for name in MANIFEST_FILES:
        path = K8S_DIR / name
        assert path.is_file(), f"missing manifest: {path}"
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                documents.append(doc)
    return documents


def _doc_by_kind(documents: list[dict], kind: str) -> dict:
    matches = [d for d in documents if d.get("kind") == kind]
    assert len(matches) == 1, f"expected one {kind}, found {len(matches)}"
    return matches[0]


def test_k8s_manifest_files_parse() -> None:
    docs = _load_k8s_documents()
    kinds = {d["kind"] for d in docs}
    assert kinds == {"ConfigMap", "Deployment", "Service"}


def test_deployment_probes_and_resources() -> None:
    docs = _load_k8s_documents()
    deployment = _doc_by_kind(docs, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    liveness = container["livenessProbe"]["httpGet"]
    readiness = container["readinessProbe"]["httpGet"]
    assert liveness["path"] == "/healthz"
    assert readiness["path"] == "/readyz"
    assert liveness["port"] == readiness["port"] == 8765

    resources = container["resources"]
    assert "limits" in resources and "requests" in resources
    assert resources["limits"]["memory"]
    assert resources["requests"]["cpu"]


def test_deployment_mounts_gateway_configmap() -> None:
    docs = _load_k8s_documents()
    deployment = _doc_by_kind(docs, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    volume_names = {v["name"] for v in deployment["spec"]["template"]["spec"]["volumes"]}
    mounts = container.get("volumeMounts", [])
    config_mounts = [m for m in mounts if m.get("name") in volume_names and "config" in m["name"]]
    assert config_mounts, "expected a ConfigMap volume mount for gateway config"
    assert container["args"] == ["--config", "/etc/concierge/gateway.yaml"]


def test_service_exposes_gateway_port() -> None:
    docs = _load_k8s_documents()
    service = _doc_by_kind(docs, "Service")
    port = service["spec"]["ports"][0]
    assert port["port"] == 8765
    assert port["targetPort"] == 8765


def test_configmap_contains_gateway_yaml() -> None:
    docs = _load_k8s_documents()
    configmap = _doc_by_kind(docs, "ConfigMap")
    gateway_yaml = configmap["data"]["gateway.yaml"]
    parsed = yaml.safe_load(gateway_yaml)
    assert parsed["gateway"]["port"] == 8765
    assert parsed["gateway"]["bind_public"] is True


def test_configmap_uses_bearer_auth_from_secret() -> None:
    """Issue #10: public-facing k8s manifests must not default to localhost auth."""
    docs = _load_k8s_documents()
    configmap = _doc_by_kind(docs, "ConfigMap")
    gateway_yaml = configmap["data"]["gateway.yaml"]
    parsed = yaml.safe_load(gateway_yaml)

    assert parsed["auth"]["type"] == "bearer"
    tokens = parsed["auth"]["bearer_tokens"]
    assert tokens == ["${CONCIERGE_BEARER_TOKEN}"]

    deployment = _doc_by_kind(docs, "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container.get("env", [])}
    assert "CONCIERGE_BEARER_TOKEN" in env
    token_env = env["CONCIERGE_BEARER_TOKEN"]
    assert token_env["valueFrom"]["secretKeyRef"] == {"name": "concierge-auth", "key": "token"}


def test_deployment_drain_grace_and_prestop() -> None:
    """P1-7: rolling-restart settings let the graceful drain finish."""
    docs = _load_k8s_documents()
    deployment = _doc_by_kind(docs, "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    # Termination grace must exceed the app drain window (default 25s).
    assert pod_spec["terminationGracePeriodSeconds"] >= 30
    container = pod_spec["containers"][0]
    prestop = container["lifecycle"]["preStop"]
    assert prestop["exec"]["command"][0] == "sleep"


def test_ingress_terminates_tls_to_gateway_service() -> None:
    """P1-7: TLS terminates at the ingress, routing to the gateway Service."""
    path = K8S_DIR / "ingress.yaml"
    assert path.is_file(), f"missing manifest: {path}"
    ingress = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert ingress["kind"] == "Ingress"
    # TLS block references a Secret by name — never inline cert material.
    tls = ingress["spec"]["tls"][0]
    assert tls["secretName"]
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "concierge-gateway"
    assert backend["port"]["number"] == 8765
