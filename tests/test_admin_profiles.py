"""Integration tests for P2-ADMIN-6 /admin/profiles endpoints."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from concierge.adapters.manager import AdapterManager
from concierge.admin import SqliteConfigStore
from concierge.core.catalog import Catalog, InMemoryCatalogStore
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType
from concierge.gateway.profiles import ProfileRegistry
from concierge.gateway.service import GatewayService
from concierge.policy.approval import DenyByDefaultApprovalBroker
from concierge.policy.engine import PolicyEngine
from concierge.policy.ratelimit import InMemoryTokenBucketRateLimiter
from concierge.server.admin import build_admin_router
from concierge.server.admin_profiles import AdminProfilesDeps, build_admin_profiles_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger


def _catalog_entry(
    name: str,
    *,
    server: str = "echo",
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    ptype: PrimitiveType = PrimitiveType.TOOL,
) -> CatalogEntry:
    return CatalogEntry(
        canonical_name=f"{server}__{name}",
        upstream_name=name,
        server_id=server,
        transport=TransportType.STDIO,
        primitive_type=ptype,
        display_label=name,
        short_description=f"tool {name}",
        tags=tags or [],
        categories=categories or [],
        risk_level=RiskLevel.LOW,
    )


@pytest.fixture
def profile_client() -> TestClient:
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
    app.include_router(
        build_admin_profiles_router(
            AdminProfilesDeps(
                auth=NoAuth(),
                config_store=store,
                catalog=catalog,
            )
        )
    )
    app.state.catalog = catalog
    with TestClient(app) as client:
        yield client


@pytest.mark.asyncio
async def _seed_catalog(catalog: Catalog) -> None:
    await catalog.store.upsert(
        _catalog_entry("echo_tool", server="echo", tags=["demo"])
    )
    await catalog.store.upsert(
        _catalog_entry("gh_search", server="github", tags=["read"])
    )
    await catalog.store.upsert(
        _catalog_entry("dns_lookup", server="dns", tags=["dns"], categories=["network"])
    )


def test_list_profiles_empty(profile_client: TestClient) -> None:
    resp = profile_client.get("/admin/profiles")
    assert resp.status_code == 200
    assert resp.json()["profiles"] == []


def test_create_update_delete_and_duplicate(profile_client: TestClient) -> None:
    body = {
        "profile": {
            "name": "demo",
            "description": "demo profile",
            "auto_apply": True,
            "selectors": [{"server": "echo", "tags": ["demo"]}],
        }
    }
    create = profile_client.post("/admin/profiles", json=body)
    assert create.status_code == 200

    get_one = profile_client.get("/admin/profiles/demo")
    assert get_one.json()["profile"]["auto_apply"] is True

    updated = dict(body["profile"])
    updated["description"] = "updated"
    put = profile_client.put("/admin/profiles/demo", json={"profile": updated})
    assert put.status_code == 200
    assert put.json()["profile"]["description"] == "updated"

    dup = profile_client.post(
        "/admin/profiles/demo/duplicate",
        json={"new_name": "demo-copy"},
    )
    assert dup.status_code == 200
    assert dup.json()["profile"]["name"] == "demo-copy"

    delete = profile_client.delete("/admin/profiles/demo")
    assert delete.status_code == 200
    names = [p["name"] for p in profile_client.get("/admin/profiles").json()["profiles"]]
    assert names == ["demo-copy"]


def test_preview_matches_server_and_tags(profile_client: TestClient) -> None:
    catalog = profile_client.app.state.catalog
    asyncio.run(_seed_catalog(catalog))

    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "echo-only",
                "selectors": [{"server": "echo"}],
            }
        },
    )
    preview = profile_client.post("/admin/profiles/echo-only/preview")
    assert preview.status_code == 200
    body = preview.json()
    assert body["matched_count"] == 1
    assert body["canonical_names"] == ["echo__echo_tool"]
    assert body["warnings"] == []

    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "github-read",
                "selectors": [
                    {"server": "github", "tags": ["read"], "primitive_type": "tool"}
                ],
            }
        },
    )
    gh = profile_client.post("/admin/profiles/github-read/preview").json()
    assert gh["matched_count"] == 1
    assert gh["primitives"][0]["server_id"] == "github"


def test_preview_no_match_emits_warnings(profile_client: TestClient) -> None:
    catalog = profile_client.app.state.catalog
    asyncio.run(_seed_catalog(catalog))

    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "missing",
                "selectors": [{"server": "nonexistent"}],
            }
        },
    )
    preview = profile_client.post("/admin/profiles/missing/preview").json()
    assert preview["matched_count"] == 0
    assert preview["primitives"] == []
    assert any("matched no catalog" in w for w in preview["warnings"])


def test_preview_selector_combinations(profile_client: TestClient) -> None:
    catalog = profile_client.app.state.catalog
    asyncio.run(_seed_catalog(catalog))

    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "by-name",
                "selectors": [{"names": ["dns__dns_lookup"]}],
            }
        },
    )
    by_name = profile_client.post("/admin/profiles/by-name/preview").json()
    assert by_name["matched_count"] == 1

    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "by-category",
                "selectors": [{"categories": ["network"]}],
            }
        },
    )
    by_cat = profile_client.post("/admin/profiles/by-category/preview").json()
    assert by_cat["matched_count"] == 1
    assert by_cat["primitives"][0]["categories"] == ["network"]


def test_apply_promotes_profile_draft(profile_client: TestClient) -> None:
    profile_client.post(
        "/admin/profiles",
        json={
            "profile": {
                "name": "to-apply",
                "selectors": [{"server": "echo"}],
            }
        },
    )
    apply = profile_client.post("/admin/profiles/apply")
    assert apply.status_code == 200
    assert apply.json()["applied"] is True
    active = profile_client.get("/admin/config/active").json()
    names = [p["name"] for p in active["config"]["profiles"]]
    assert "to-apply" in names