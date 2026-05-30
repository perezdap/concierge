"""Tests for config-driven store selection in build_app (P1-2)."""
from __future__ import annotations

import pytest

from concierge.config import GatewayConfig, StorageConfig
from concierge.core.catalog_store import SqliteCatalogStore
from concierge.server.app import build_app


@pytest.fixture
def base_config():
    return GatewayConfig(
        gateway={"host": "127.0.0.1", "port": 8765, "allowed_origins": ["http://localhost"]},
        auth={"type": "none"},
    )


def test_build_app_defaults_to_memory(base_config: GatewayConfig):
    app = build_app(base_config)
    from concierge.core.catalog import InMemoryCatalogStore
    assert isinstance(app.state.catalog.store, InMemoryCatalogStore)
    from concierge.core.session import SessionManager
    assert isinstance(app.state.sessions, SessionManager)


def test_build_app_selects_sqlite_catalog(tmp_path, base_config: GatewayConfig):
    db_path = str(tmp_path / "catalog.db")
    base_config.storage = StorageConfig(catalog_store="sqlite", catalog_sqlite_path=db_path)
    app = build_app(base_config)
    assert isinstance(app.state.catalog.store, SqliteCatalogStore)


def test_build_app_selects_redis_sessions(base_config: GatewayConfig):
    base_config.storage = StorageConfig(session_store="redis", redis_url="redis://localhost:6379/0")
    # We can't fully instantiate without a running Redis, but the error should be
    # about connectivity, not about missing config.
    with pytest.raises(Exception) as exc_info:
        build_app(base_config)
    # asyncpg / redis will raise on pool creation or first use; either is fine.
    assert "redis" in str(exc_info.value).lower() or "connection" in str(exc_info.value).lower()
