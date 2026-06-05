"""FastAPI integration tests for P2-ADMIN-3 /admin/config endpoints."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from concierge.adapters.manager import AdapterManager
from concierge.admin import SqliteConfigStore
from concierge.admin.reload import ReloadCoordinator
from concierge.config import GatewayConfig, load_config
from concierge.core.catalog import Catalog, InMemoryCatalogStore
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.gateway.profiles import ProfileRegistry
from concierge.gateway.service import GatewayService
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import InMemoryTokenBucketRateLimiter
from concierge.server.admin import build_admin_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger


def _minimal_dict() -> dict:
    return load_config(Path("config/minimal.yaml")).model_dump(mode="json")


def _secret_dict() -> dict:
    cfg = _minimal_dict()
    cfg["auth"] = {
        "type": "bearer",
        "bearer_tokens": ["api-test-secret-value"],
        "providers": [],
    }
    return cfg


@pytest.fixture
def config_client() -> TestClient:
    store = SqliteConfigStore(":memory:")
    catalog = Catalog(InMemoryCatalogStore())
    sessions = SessionManager()
    bus = NotificationBus()
    publishing = PublishingService(catalog=catalog, bus=bus)
    profiles = ProfileRegistry()
    adapters = AdapterManager(catalog)
    audit = AuditLogger()
    policy = PolicyEngine(
        InMemoryTokenBucketRateLimiter(),
        DenyByDefaultApprovalBroker(),
        audit,
    )
    service = GatewayService(
        catalog=catalog,
        publishing=publishing,
        sessions=sessions,
        adapters=adapters,
        policy=policy,
        audit=audit,
        bus=bus,
        profiles=profiles,
    )
    app = FastAPI()
    app.include_router(
        build_admin_router(
            auth=NoAuth(),
            catalog=catalog,
            adapters=adapters,
            sessions=sessions,
            service=service,
            config_store=store,
        )
    )
    app.state.config_store = store
    with TestClient(app) as client:
        yield client


def test_build_app_wires_config_store_and_reload_coordinator(
    gateway_config: GatewayConfig,
) -> None:
    from concierge.server.app import build_app

    with TestClient(build_app(gateway_config)) as client:
        status = client.get("/admin/config/status")
        assert status.status_code == 200
        body = status.json()
        assert body["config_store"] is True
        assert body["reload_coordinator"] is True
        assert client.app.state.config_store is not None
        assert client.app.state.reload_coordinator is not None


def test_get_active_empty(config_client: TestClient) -> None:
    resp = config_client.get("/admin/config/active")
    assert resp.status_code == 200
    assert resp.json()["config"] is None


def test_draft_update_validate_and_apply(config_client: TestClient) -> None:
    put = config_client.put("/admin/config/draft", json={"config": _minimal_dict()})
    assert put.status_code == 200
    assert put.json()["version"]["status"] == "draft"

    val = config_client.post("/admin/config/draft/validate")
    assert val.status_code == 200
    assert val.json()["ok"] is True

    apply = config_client.post("/admin/config/apply")
    assert apply.status_code == 200
    assert apply.json()["version"]["status"] == "active"

    active = config_client.get("/admin/config/active")
    assert active.json()["config"] is not None
    assert active.json()["version"]["status"] == "active"


def test_active_read_redacts_secrets(config_client: TestClient) -> None:
    config_client.put("/admin/config/draft", json={"config": _secret_dict()})
    config_client.post("/admin/config/apply")
    active = config_client.get("/admin/config/active").json()
    text = str(active)
    assert "api-test-secret-value" not in text


def test_preview_diff(config_client: TestClient) -> None:
    config_client.put("/admin/config/draft", json={"config": _minimal_dict()})
    config_client.post("/admin/config/apply")
    draft = _minimal_dict()
    draft["log_level"] = "DEBUG"
    config_client.put("/admin/config/draft", json={"config": draft})
    diff = config_client.get("/admin/config/draft/diff")
    assert diff.status_code == 200
    assert "log_level" in diff.json()["diff"]


def test_validate_returns_structured_errors(config_client: TestClient) -> None:
    bad = _minimal_dict()
    bad["session_pool"] = {"idle_ttl_s": 0}
    config_client.put("/admin/config/draft", json={"config": bad})
    val = config_client.post("/admin/config/draft/validate")
    assert val.status_code == 200
    body = val.json()
    assert body["ok"] is False
    assert any("idle_ttl_s" in i["path"] for i in body["issues"])


def test_apply_rejects_invalid_draft(config_client: TestClient) -> None:
    bad = _minimal_dict()
    bad["session_pool"] = {"idle_ttl_s": -1}
    config_client.put("/admin/config/draft", json={"config": bad})
    resp = config_client.post("/admin/config/apply")
    assert resp.status_code == 400


def test_import_export_yaml(config_client: TestClient) -> None:
    raw = Path("config/minimal.yaml").read_text()
    imp = config_client.post(
        "/admin/config/import",
        json={"yaml": raw, "expand_environment": False},
    )
    assert imp.status_code == 200
    config_client.post("/admin/config/apply")
    exp = config_client.get("/admin/config/export")
    assert exp.status_code == 200
    assert "yaml" in exp.json()
    assert "NOTES_TOKEN" not in exp.json()["yaml"]


def test_rollback_via_api(config_client: TestClient) -> None:
    v1 = _minimal_dict()
    v1["log_level"] = "INFO"
    config_client.put("/admin/config/draft", json={"config": v1})
    config_client.post("/admin/config/apply")

    v2 = _minimal_dict()
    v2["log_level"] = "ERROR"
    config_client.put("/admin/config/draft", json={"config": v2})
    config_client.post("/admin/config/apply")

    rb = config_client.post("/admin/config/rollback")
    assert rb.status_code == 200
    active = config_client.get("/admin/config/active").json()
    assert active["config"]["log_level"] == "INFO"


def test_reload_without_coordinator_is_501(config_client: TestClient) -> None:
    config_client.put("/admin/config/draft", json={"config": _minimal_dict()})
    config_client.post("/admin/config/apply")
    resp = config_client.post("/admin/config/reload")
    assert resp.status_code == 501


def test_list_versions(config_client: TestClient) -> None:
    config_client.put("/admin/config/draft", json={"config": _minimal_dict()})
    config_client.post("/admin/config/apply")
    versions = config_client.get("/admin/config/versions")
    assert versions.status_code == 200
    assert len(versions.json()["versions"]) >= 1


def _reload_apply_client(
    *,
    store: SqliteConfigStore | None = None,
) -> TestClient:
    store = store or SqliteConfigStore(":memory:")
    catalog = Catalog(InMemoryCatalogStore())
    sessions = SessionManager()
    bus = NotificationBus()
    publishing = PublishingService(catalog=catalog, bus=bus)
    profiles = ProfileRegistry()
    adapters = AdapterManager(catalog)
    audit = AuditLogger()

    async def build_runtime(_cfg: GatewayConfig) -> object:
        raise ConnectionError("upstream connect failed")

    coord = ReloadCoordinator(audit=audit, build_runtime=build_runtime)  # type: ignore[arg-type]
    policy = PolicyEngine(
        InMemoryTokenBucketRateLimiter(),
        DenyByDefaultApprovalBroker(),
        audit,
    )
    service = GatewayService(
        catalog=catalog,
        publishing=publishing,
        sessions=sessions,
        adapters=adapters,
        policy=policy,
        audit=audit,
        bus=bus,
        profiles=profiles,
    )
    app = FastAPI()
    app.include_router(
        build_admin_router(
            auth=NoAuth(),
            catalog=catalog,
            adapters=adapters,
            sessions=sessions,
            service=service,
            config_store=store,
            reload_coordinator=coord,
        )
    )
    return TestClient(app)


def test_apply_failed_reload_leaves_active_unchanged() -> None:
    store = SqliteConfigStore(":memory:")
    v1 = _minimal_dict()
    v1["log_level"] = "INFO"
    asyncio.run(store.create_or_update_draft(v1))
    asyncio.run(store.promote_draft_to_active())

    with _reload_apply_client(store=store) as client:
        v2 = _minimal_dict()
        v2["log_level"] = "ERROR"
        client.put("/admin/config/draft", json={"config": v2})
        resp = client.post("/admin/config/apply")
        assert resp.status_code == 400

        active = client.get("/admin/config/active").json()
        assert active["config"]["log_level"] == "INFO"
        draft = client.get("/admin/config/draft").json()
        assert draft["config"]["log_level"] == "ERROR"