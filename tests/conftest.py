"""Shared fixtures for facade and app integration tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concierge.config import AuthConfig, GatewayConfig, GatewayHttpConfig, UpstreamServerConfig
from concierge.server.app import build_app

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ECHO_SERVER = _REPO_ROOT / "examples" / "upstream_echo_server.py"


def _base_gateway_config(
    *,
    auth: AuthConfig | None = None,
    origins: list[str] | None = None,
) -> GatewayConfig:
    return GatewayConfig(
        auth=auth or AuthConfig(type="none"),
        gateway=GatewayHttpConfig(
            allowed_origins=origins
            or ["http://localhost", "http://127.0.0.1"],
        ),
        upstream_servers=[],
        session_pool={"idle_ttl_s": 3600, "gc_interval_s": 3600, "max_upstream_sessions": 8},
    )


@pytest.fixture
def gateway_config() -> GatewayConfig:
    return _base_gateway_config()


@pytest.fixture
def bearer_gateway_config() -> GatewayConfig:
    return _base_gateway_config(
        auth=AuthConfig(type="bearer", bearer_tokens=["test-secret-token"]),
    )


@pytest.fixture
def echo_stdio_config() -> GatewayConfig:
    cfg = _base_gateway_config()
    cfg.upstream_servers = [
        UpstreamServerConfig(
            id="echo",
            transport="stdio",
            command=[sys.executable, str(_ECHO_SERVER)],
        ),
    ]
    return cfg


@pytest.fixture
def client(gateway_config: GatewayConfig) -> TestClient:
    app = build_app(gateway_config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def bearer_client(bearer_gateway_config: GatewayConfig) -> TestClient:
    app = build_app(bearer_gateway_config)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def echo_client(echo_stdio_config: GatewayConfig) -> TestClient:
    app = build_app(echo_stdio_config)
    with TestClient(app) as c:
        yield c
