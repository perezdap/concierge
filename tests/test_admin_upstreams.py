"""Integration tests for P2-ADMIN-5 /admin/upstreams endpoints."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

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
from concierge.server.admin_upstreams import AdminUpstreamsDeps, build_admin_upstreams_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger


def _minimal_dict() -> dict:
    return load_config(Path("config/minimal.yaml")).model_dump(mode="json")


def _upstream_client(
    *,
    store: SqliteConfigStore | None = None,
    reload_coordinator: ReloadCoordinator | None = None,
) -> TestClient:
    store = store or SqliteConfigStore(":memory:")
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
    app.include_router(
        build_admin_upstreams_router(
            AdminUpstreamsDeps(
                auth=NoAuth(),
                config_store=store,
                adapters=adapters,
                reload_coordinator=reload_coordinator,
            )
        )
    )
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    with _upstream_client() as c:
        yield c


def test_list_upstreams_empty(client: TestClient) -> None:
    resp = client.get("/admin/upstreams")
    assert resp.status_code == 200
    assert resp.json()["upstreams"] == []


def test_create_update_delete_upstream_draft(client: TestClient) -> None:
    cfg = _minimal_dict()
    upstream = cfg["upstream_servers"][0]
    create = client.post("/admin/upstreams", json={"upstream": upstream})
    assert create.status_code == 200
    upstream_id = upstream["id"]
    listed = client.get("/admin/upstreams").json()["upstreams"]
    assert any(u["id"] == upstream_id for u in listed)

    updated = dict(upstream)
    updated["default_tags"] = ["demo", "edited"]
    put = client.put(f"/admin/upstreams/{upstream_id}", json={"upstream": updated})
    assert put.status_code == 200
    assert "edited" in put.json()["upstream"]["default_tags"]

    delete = client.delete(f"/admin/upstreams/{upstream_id}")
    assert delete.status_code == 200
    assert client.get("/admin/upstreams").json()["upstreams"] == []


def test_validate_rejects_missing_stdio_command(client: TestClient) -> None:
    bad = {
        "id": "bad-stdio",
        "transport": "stdio",
        "command": None,
    }
    resp = client.post("/admin/upstreams/bad-stdio/validate", json={"upstream": bad})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["issues"]


def test_validate_rejects_missing_http_url(client: TestClient) -> None:
    bad = {
        "id": "bad-http",
        "transport": "streamable_http",
        "url": None,
    }
    resp = client.post("/admin/upstreams/bad-http/validate", json={"upstream": bad})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_create_conflict_on_duplicate_id(client: TestClient) -> None:
    upstream = _minimal_dict()["upstream_servers"][0]
    assert client.post("/admin/upstreams", json={"upstream": upstream}).status_code == 200
    dup = client.post("/admin/upstreams", json={"upstream": upstream})
    assert dup.status_code == 409


def test_test_connection_success_mocked(client: TestClient) -> None:
    upstream = _minimal_dict()["upstream_servers"][0]
    client.post("/admin/upstreams", json={"upstream": upstream})

    class _FakeAdapter:
        server_id = "echo"
        transport = "stdio"

        async def connect(self) -> None:
            return None

        async def initialize(self) -> dict:
            return {}

        async def list_tools(self) -> list:
            return [{"name": "t1"}]

        async def list_resources(self) -> list:
            return []

        async def list_prompts(self) -> list:
            return [{"name": "p1"}]

        async def close(self) -> None:
            return None

    with patch("concierge.server.admin_upstreams.build_adapter", return_value=_FakeAdapter()):
        resp = client.post(f"/admin/upstreams/{upstream['id']}/test-connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tools_discovered"] == 1
    assert body["prompts_discovered"] == 1


def test_test_connection_failure_mocked(client: TestClient) -> None:
    upstream = _minimal_dict()["upstream_servers"][0]
    client.post("/admin/upstreams", json={"upstream": upstream})

    class _FailAdapter:
        async def connect(self) -> None:
            raise ConnectionError("handshake failed")

        async def close(self) -> None:
            return None

    with patch(
        "concierge.server.admin_upstreams.build_adapter",
        return_value=_FailAdapter(),
    ):
        resp = client.post(f"/admin/upstreams/{upstream['id']}/test-connection")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "handshake" in (resp.json().get("error") or "")


def test_apply_promotes_draft(client: TestClient) -> None:
    upstream = _minimal_dict()["upstream_servers"][0]
    client.post("/admin/upstreams", json={"upstream": upstream})
    apply = client.post("/admin/upstreams/apply")
    assert apply.status_code == 200
    assert apply.json()["applied"] is True
    active = client.get("/admin/config/active").json()
    assert active["config"] is not None
    ids = [s["id"] for s in active["config"]["upstream_servers"]]
    assert upstream["id"] in ids


def test_apply_failed_reload_leaves_active_unchanged() -> None:
    store = SqliteConfigStore(":memory:")
    base = _minimal_dict()
    asyncio.run(store.create_or_update_draft(base))
    asyncio.run(store.promote_draft_to_active())

    audit = AuditLogger()

    async def failing_build(_cfg: GatewayConfig) -> object:
        raise ConnectionError("upstream connect failed")

    coord = ReloadCoordinator(audit=audit, build_runtime=failing_build)  # type: ignore[arg-type]

    with _upstream_client(store=store, reload_coordinator=coord) as client:
        bad = dict(base["upstream_servers"][0])
        bad["id"] = "broken-echo"
        bad["command"] = ["/nonexistent/binary"]
        client.post("/admin/upstreams", json={"upstream": bad})
        resp = client.post("/admin/upstreams/apply")
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "apply_reload_failed"

        active = client.get("/admin/config/active").json()
        active_ids = [s["id"] for s in active["config"]["upstream_servers"]]
        assert "broken-echo" not in active_ids
        assert "echo" in active_ids