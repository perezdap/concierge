"""Upstream OAuth token injection into HTTP/SSE adapters.

Covers the bridge that feeds tokens stored by the admin OAuth flows
(`UpstreamOAuthService` / `UpstreamCredentialStore`) into the per-request headers
of `StreamableHttpAdapter` / `LegacySseAdapter`, including auto-refresh on expiry
and backwards-compatible behavior when no credential is stored.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from concierge.adapters.auth_headers import AuthHeaderProvider, OAuthAuthHeaderProvider
from concierge.adapters.sse_legacy import LegacySseAdapter
from concierge.adapters.streamable_http import StreamableHttpAdapter
from concierge.admin.credential_store import OAuthTokenSet, UpstreamCredentialStore
from concierge.admin.oauth import OAuthPendingStore, UpstreamOAuthService
from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key


def _oauth_service() -> UpstreamOAuthService:
    backend = InMemoryCredentialStore(key=resolve_fernet_key("test-key"))
    return UpstreamOAuthService(
        credential_store=UpstreamCredentialStore(backend=backend),
        pending=OAuthPendingStore(),
    )


# ---------------------------------------------------------------------------
# OAuthAuthHeaderProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_is_runtime_protocol():
    provider = OAuthAuthHeaderProvider(_oauth_service())
    assert isinstance(provider, AuthHeaderProvider)


@pytest.mark.asyncio
async def test_no_credential_returns_empty_headers():
    provider = OAuthAuthHeaderProvider(_oauth_service())
    assert await provider.headers("github") == {}


@pytest.mark.asyncio
async def test_valid_token_injected_with_token_type():
    svc = _oauth_service()
    await svc.credentials.save_tokens(
        "github",
        OAuthTokenSet(access_token="tok-123", token_type="Bearer"),
    )
    provider = OAuthAuthHeaderProvider(svc)
    assert await provider.headers("github") == {"Authorization": "Bearer tok-123"}


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_before_injection(monkeypatch):
    svc = _oauth_service()
    await svc.credentials.save_tokens(
        "github",
        OAuthTokenSet(
            access_token="old",
            refresh_token="refresh-1",
            expires_at=time.time() - 10,  # already expired
            token_endpoint="https://idp.test/token",
            client_id="client-x",
        ),
    )

    async def fake_exchange(token_endpoint, data, **kwargs):
        assert data["grant_type"] == "refresh_token"
        new = OAuthTokenSet(access_token="new", refresh_token="refresh-2")
        return new, {}

    monkeypatch.setattr(svc, "_exchange_token", fake_exchange)

    provider = OAuthAuthHeaderProvider(svc)
    assert await provider.headers("github") == {"Authorization": "Bearer new"}


@pytest.mark.asyncio
async def test_refresh_failure_falls_back_to_empty_headers(monkeypatch):
    svc = _oauth_service()
    await svc.credentials.save_tokens(
        "github",
        OAuthTokenSet(
            access_token="old",
            refresh_token="refresh-1",
            expires_at=time.time() - 10,
            token_endpoint="https://idp.test/token",
            client_id="client-x",
        ),
    )

    async def boom(*a, **k):
        raise RuntimeError("revoked")

    monkeypatch.setattr(svc, "_exchange_token", boom)

    provider = OAuthAuthHeaderProvider(svc)
    # Must not raise; upstream gets no stale header and rejects on its own.
    assert await provider.headers("github") == {}


@pytest.mark.asyncio
async def test_expired_without_refresh_token_returns_empty():
    svc = _oauth_service()
    await svc.credentials.save_tokens(
        "github",
        OAuthTokenSet(access_token="old", expires_at=time.time() - 10),
    )
    provider = OAuthAuthHeaderProvider(svc)
    # valid_token_set raises ValueError -> provider swallows -> {}
    assert await provider.headers("github") == {}


# ---------------------------------------------------------------------------
# Adapter integration
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers
        self.calls: list[str] = []

    async def headers(self, server_id: str) -> dict[str, str]:
        self.calls.append(server_id)
        return dict(self._headers)


@pytest.mark.asyncio
async def test_streamable_http_merges_dynamic_auth_header():
    provider = _FakeProvider({"Authorization": "Bearer injected"})
    adapter = StreamableHttpAdapter(
        "github",
        "http://upstream/mcp",
        headers={"X-Static": "1"},
        listen_for_notifications=False,
        auth_header_provider=provider,
    )
    await adapter.connect()
    captured: dict[str, dict[str, str]] = {}

    async def fake_post(url, json=None, headers=None):
        captured["headers"] = headers
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        return resp

    adapter._client.post = fake_post
    await adapter.list_tools()
    assert captured["headers"]["Authorization"] == "Bearer injected"
    assert captured["headers"]["X-Static"] == "1"
    assert provider.calls == ["github"]


@pytest.mark.asyncio
async def test_streamable_http_no_provider_unchanged():
    adapter = StreamableHttpAdapter(
        "srv", "http://upstream/mcp", headers={"X-Static": "1"},
        listen_for_notifications=False,
    )
    await adapter.connect()
    captured: dict[str, dict[str, str]] = {}

    async def fake_post(url, json=None, headers=None):
        captured["headers"] = headers
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        return resp

    adapter._client.post = fake_post
    await adapter.list_tools()
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["X-Static"] == "1"


@pytest.mark.asyncio
async def test_streamable_http_dynamic_auth_overrides_static():
    provider = _FakeProvider({"Authorization": "Bearer dynamic"})
    adapter = StreamableHttpAdapter(
        "srv",
        "http://upstream/mcp",
        headers={"Authorization": "Bearer static"},
        listen_for_notifications=False,
        auth_header_provider=provider,
    )
    await adapter.connect()
    captured: dict[str, dict[str, str]] = {}

    async def fake_post(url, json=None, headers=None):
        captured["headers"] = headers
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        return resp

    adapter._client.post = fake_post
    await adapter.list_tools()
    assert captured["headers"]["Authorization"] == "Bearer dynamic"


@pytest.mark.asyncio
async def test_legacy_sse_post_merges_dynamic_auth_header():
    provider = _FakeProvider({"Authorization": "Bearer injected"})
    adapter = LegacySseAdapter(
        "github",
        "http://upstream/sse",
        headers={"X-Static": "1"},
        auth_header_provider=provider,
    )
    headers = await adapter._auth_headers()
    assert headers == {"X-Static": "1", "Authorization": "Bearer injected"}
    assert provider.calls == ["github"]
