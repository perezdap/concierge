"""Built-in OAuth provider presets + provider-driven sign-in.

Covers the one-click "Connect with <provider>" UX layer: the preset registry,
env-based credential resolution, the browser-safe catalog, and the
provider-resolution path through the admin OAuth router.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from concierge.admin.credential_store import UpstreamCredentialStore
from concierge.admin.oauth import OAuthPendingStore, UpstreamOAuthService
from concierge.admin.oauth_providers import (
    get_provider,
    list_providers,
    public_catalog,
)
from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key
from concierge.server.admin_oauth import AdminOAuthDeps, build_admin_oauth_router
from concierge.server.auth import NoAuth
from concierge.util.audit import AuditLogger


def _router_client() -> TestClient:
    store = UpstreamCredentialStore(
        backend=InMemoryCredentialStore(key=resolve_fernet_key("k"))
    )
    svc = UpstreamOAuthService(credential_store=store, pending=OAuthPendingStore())
    app = FastAPI()
    app.include_router(
        build_admin_oauth_router(
            AdminOAuthDeps(
                auth=NoAuth(),
                oauth=svc,
                credentials=store,
                audit=AuditLogger(),
                public_base_url="http://testserver",
            )
        )
    )
    return TestClient(app)


def test_providers_endpoint_lists_four(monkeypatch):
    monkeypatch.delenv("CONCIERGE_OAUTH_GITHUB_CLIENT_ID", raising=False)
    client = _router_client()
    r = client.get("/admin/oauth/providers")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["providers"]}
    assert ids == {"github", "google", "microsoft", "atlassian"}


def test_provider_sign_in_uses_preset_endpoints(monkeypatch):
    monkeypatch.setenv("CONCIERGE_OAUTH_GITHUB_CLIENT_ID", "gh-id")
    monkeypatch.setenv("CONCIERGE_OAUTH_GITHUB_CLIENT_SECRET", "gh-secret")
    client = _router_client()
    r = client.post("/admin/oauth/myrepo/sign-in/start", json={"provider": "github"})
    assert r.status_code == 200
    url = urlparse(r.json()["authorization_url"])
    assert url.netloc == "github.com"
    qs = parse_qs(url.query)
    assert qs["client_id"] == ["gh-id"]
    assert qs["code_challenge_method"] == ["S256"]


def test_provider_sign_in_requires_configured_credentials(monkeypatch):
    monkeypatch.delenv("CONCIERGE_OAUTH_ATLASSIAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONCIERGE_OAUTH_ATLASSIAN_CLIENT_SECRET", raising=False)
    client = _router_client()
    r = client.post("/admin/oauth/jira/sign-in/start", json={"provider": "atlassian"})
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_provider_sign_in_byo_credentials_fallback(monkeypatch):
    monkeypatch.delenv("CONCIERGE_OAUTH_ATLASSIAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONCIERGE_OAUTH_ATLASSIAN_CLIENT_SECRET", raising=False)
    client = _router_client()
    r = client.post(
        "/admin/oauth/jira/sign-in/start",
        json={"provider": "atlassian", "client_id": "byo", "client_secret": "byo-s"},
    )
    assert r.status_code == 200
    qs = parse_qs(urlparse(r.json()["authorization_url"]).query)
    assert qs["audience"] == ["api.atlassian.com"]
    assert qs["client_id"] == ["byo"]


def test_unknown_provider_rejected():
    client = _router_client()
    r = client.post("/admin/oauth/x/sign-in/start", json={"provider": "dropbox"})
    assert r.status_code == 400
    assert "unknown provider" in r.json()["detail"]


def test_callback_returns_friendly_html_on_error():
    client = _router_client()
    r = client.get("/admin/oauth/callback", params={"code": "x", "state": "bogus"})
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert "Sign-in failed" in r.text


def test_all_four_providers_present():
    ids = {p.id for p in list_providers()}
    assert ids == {"github", "google", "microsoft", "atlassian"}


def test_get_provider_is_case_insensitive():
    assert get_provider("GitHub") is not None
    assert get_provider("  google ") is not None
    assert get_provider("nope") is None


def test_refresh_quirks_are_encoded():
    google = get_provider("google")
    assert google is not None
    assert google.extra_authorize_params["access_type"] == "offline"
    assert google.extra_authorize_params["prompt"] == "consent"

    atlassian = get_provider("atlassian")
    assert atlassian is not None
    assert atlassian.extra_authorize_params["audience"] == "api.atlassian.com"
    assert "offline_access" in atlassian.default_scopes

    microsoft = get_provider("microsoft")
    assert microsoft is not None
    assert "offline_access" in microsoft.default_scopes


def test_github_has_no_discovery():
    github = get_provider("github")
    assert github is not None
    assert github.supports_discovery is False


def test_env_credential_resolution(monkeypatch):
    github = get_provider("github")
    assert github is not None
    monkeypatch.delenv("CONCIERGE_OAUTH_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONCIERGE_OAUTH_GITHUB_CLIENT_SECRET", raising=False)
    assert github.is_ready() is False

    monkeypatch.setenv("CONCIERGE_OAUTH_GITHUB_CLIENT_ID", "id-x")
    monkeypatch.setenv("CONCIERGE_OAUTH_GITHUB_CLIENT_SECRET", "secret-x")
    assert github.is_ready() is True
    assert github.client_id() == "id-x"
    assert github.client_secret() == "secret-x"


def test_public_catalog_never_leaks_secrets(monkeypatch):
    monkeypatch.setenv("CONCIERGE_OAUTH_GOOGLE_CLIENT_ID", "id-x")
    monkeypatch.setenv("CONCIERGE_OAUTH_GOOGLE_CLIENT_SECRET", "super-secret")
    catalog = public_catalog()
    blob = repr(catalog)
    assert "super-secret" not in blob
    google = next(c for c in catalog if c["id"] == "google")
    assert google["ready"] is True
    assert google["env_client_id"] == "CONCIERGE_OAUTH_GOOGLE_CLIENT_ID"


def test_extra_authorize_params_cannot_override_pkce(monkeypatch):
    from concierge.admin.credential_store import (
        OAuthTokenSet,  # noqa: F401  (import sanity)
    )
    from concierge.admin.oauth import (
        OAuthDiscoveryDocument,
        OAuthPendingStore,
        UpstreamCredentialStore,
        UpstreamOAuthService,
    )
    from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key

    svc = UpstreamOAuthService(
        credential_store=UpstreamCredentialStore(
            backend=InMemoryCredentialStore(key=resolve_fernet_key("k"))
        ),
        pending=OAuthPendingStore(),
    )
    discovery = OAuthDiscoveryDocument(
        issuer="x",
        authorization_endpoint="https://idp.test/authorize",
        token_endpoint="https://idp.test/token",
    )
    auth_url, _state = svc.begin_authorization_code(
        upstream_id="u1",
        discovery=discovery,
        client_id="c1",
        redirect_uri="https://app/cb",
        scopes="openid",
        # Malicious attempt to override security params must be ignored.
        extra_authorize_params={
            "code_challenge_method": "plain",
            "state": "attacker",
            "access_type": "offline",
        },
    )
    assert "code_challenge_method=S256" in auth_url
    assert "state=attacker" not in auth_url
    assert "access_type=offline" in auth_url
