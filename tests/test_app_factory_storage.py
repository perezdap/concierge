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
    # redis.asyncio.from_url() is lazy: build_app must wire a RedisSessionManager
    # without opening a socket, so a running Redis is NOT required to build the app.
    from concierge.core.session import RedisSessionManager

    app = build_app(base_config)
    assert isinstance(app.state.sessions, RedisSessionManager)


def test_build_app_redis_sessions_requires_url(base_config: GatewayConfig):
    # session_store=redis with no redis_url is a config error and must fail fast.
    base_config.storage = StorageConfig(session_store="redis", redis_url=None)
    with pytest.raises(ValueError, match="redis_url"):
        build_app(base_config)
