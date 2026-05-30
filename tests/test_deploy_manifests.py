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
