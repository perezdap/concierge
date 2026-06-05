"""Tests for P2-ADMIN-1 runtime ConfigStore."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from concierge.admin import SqliteConfigStore, validate_gateway_config
from concierge.admin.models import ConfigVersionStatus
from concierge.config import GatewayConfig, load_config


def _minimal_config_dict() -> dict:
    return load_config(Path("config/minimal.yaml")).model_dump(mode="json")


def _config_with_secrets() -> dict:
    cfg = _minimal_config_dict()
    cfg["auth"] = {
        "type": "bearer",
        "bearer_tokens": ["super-secret-token-12345"],
        "providers": [],
    }
    cfg["upstream_servers"] = [
        {
            "id": "http-up",
            "transport": "streamable_http",
            "url": "http://127.0.0.1:9999/mcp",
            "headers": {"Authorization": "Bearer upstream-secret"},
        }
    ]
    return cfg


@pytest.fixture
def store() -> SqliteConfigStore:
    s = SqliteConfigStore(":memory:")
    yield s
    s.close()


@pytest.mark.asyncio
async def test_no_active_until_promoted(store: SqliteConfigStore) -> None:
    assert await store.get_active_redacted() is None
    assert await store.get_draft() is None


@pytest.mark.asyncio
async def test_draft_create_update_and_validate(store: SqliteConfigStore) -> None:
    cfg = _minimal_config_dict()
    draft = await store.create_or_update_draft(cfg, created_by="operator-a")
    assert draft.status == ConfigVersionStatus.DRAFT
    assert draft.created_by == "operator-a"

    got = await store.get_draft()
    assert got is not None
    assert got.id == draft.id

    result = await store.validate_draft()
    assert result.ok is True

    cfg["log_level"] = "DEBUG"
    updated = await store.create_or_update_draft(cfg, created_by="operator-b")
    assert updated.id == draft.id
    assert updated.config["log_level"] == "DEBUG"


@pytest.mark.asyncio
async def test_validate_draft_reports_pydantic_errors(store: SqliteConfigStore) -> None:
    bad = _minimal_config_dict()
    bad["session_pool"] = {"idle_ttl_s": 0}
    await store.create_or_update_draft(bad)
    result = await store.validate_draft()
    assert result.ok is False
    assert any("idle_ttl_s" in issue.path for issue in result.issues)


@pytest.mark.asyncio
async def test_promote_requires_valid_draft(store: SqliteConfigStore) -> None:
    bad = _minimal_config_dict()
    bad["session_pool"] = {"idle_ttl_s": -1}
    await store.create_or_update_draft(bad)
    with pytest.raises(ValueError, match="validation failed"):
        await store.promote_draft_to_active()


@pytest.mark.asyncio
async def test_promote_and_redacted_active_read(store: SqliteConfigStore) -> None:
    cfg = _config_with_secrets()
    await store.create_or_update_draft(cfg)
    active = await store.promote_draft_to_active(created_by="promoter")
    assert active.status == ConfigVersionStatus.ACTIVE

    redacted = await store.get_active_redacted()
    assert redacted is not None
    assert "super-secret-token" not in str(redacted)
    assert redacted["upstream_servers"][0]["headers"]["Authorization"] == "***"
    assert await store.get_draft() is None


@pytest.mark.asyncio
async def test_version_history_after_promotions(store: SqliteConfigStore) -> None:
    v1 = _minimal_config_dict()
    await store.create_or_update_draft(v1)
    first = await store.promote_draft_to_active()

    v2 = _minimal_config_dict()
    v2["log_level"] = "WARNING"
    await store.create_or_update_draft(v2)
    second = await store.promote_draft_to_active()

    history = await store.list_versions()
    assert len(history) >= 2
    statuses = {h.id: h.status for h in history}
    assert statuses[second.id] == ConfigVersionStatus.ACTIVE
    assert statuses[first.id] == ConfigVersionStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_rollback_restores_last_known_good(store: SqliteConfigStore) -> None:
    v1 = _minimal_config_dict()
    v1["log_level"] = "INFO"
    await store.create_or_update_draft(v1)
    first = await store.promote_draft_to_active()

    v2 = _minimal_config_dict()
    v2["log_level"] = "ERROR"
    await store.create_or_update_draft(v2)
    await store.promote_draft_to_active()

    rolled = await store.rollback()
    assert rolled.id == first.id
    active = await store.get_active_version()
    assert active is not None
    assert active.config["log_level"] == "INFO"


@pytest.mark.asyncio
async def test_import_yaml_and_export_redacted(store: SqliteConfigStore) -> None:
    raw = Path("config/minimal.yaml").read_text()
    draft = await store.import_yaml(raw, expand_environment=False)
    assert draft.status == ConfigVersionStatus.DRAFT
    await store.promote_draft_to_active()
    exported = await store.export_active_yaml_redacted()
    assert exported is not None
    parsed = yaml.safe_load(exported)
    GatewayConfig.model_validate(parsed)


@pytest.mark.asyncio
async def test_export_yaml_strips_secrets(store: SqliteConfigStore) -> None:
    await store.create_or_update_draft(_config_with_secrets())
    await store.promote_draft_to_active()
    exported = await store.export_active_yaml_redacted()
    assert exported is not None
    assert "super-secret-token" not in exported
    assert "upstream-secret" not in exported
    assert "***" in exported


@pytest.mark.asyncio
async def test_validate_gateway_config_unit() -> None:
    ok = validate_gateway_config(_minimal_config_dict())
    assert ok.ok is True


@pytest.mark.asyncio
async def test_sqlite_persistence_across_instances() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        s1 = SqliteConfigStore(path)
        await s1.create_or_update_draft(_minimal_config_dict())
        await s1.promote_draft_to_active()
        s1.close()

        s2 = SqliteConfigStore(path)
        active = await s2.get_active_redacted()
        assert active is not None
        s2.close()
    finally:
        Path(path).unlink(missing_ok=True)