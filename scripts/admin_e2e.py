"""Admin Console release-gate E2E harness.

The harness is full-stack HTTP-first: it serves the built /admin SPA through the
FastAPI app and drives the admin and MCP endpoints with a fake upstream plus a
fake OAuth IdP. It avoids browser automation so it stays deterministic in local
and sandboxed environments.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ISSUER = "https://admin-e2e-idp.test"
DEFAULT_WORK_ROOT = ROOT / ".bridgespace" / "admin-e2e-runs"
ADMIN_TOKEN = "admin-e2e-bearer-token"
RAW_HEADER_SECRET = "Bearer admin-e2e-raw-header-secret-token"
REFRESHED_ACCESS = "admin-e2e-refreshed-access-token"
INIT_RPC = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "admin-e2e", "version": "1.0.0"},
    },
}


class FakeAdminE2EAdapter:
    """Custom in-process upstream used by the admin E2E harness."""

    transport = "custom"

    def __init__(self, *, server_id: str, **_params: Any) -> None:
        self.server_id = server_id
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "admin-e2e-upstream", "version": "0.0.1"},
            "capabilities": {"tools": {}},
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "echo",
                "description": "Echoes text for admin E2E.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            }
        ]

    async def list_resources(self) -> list[dict[str, Any]]:
        return []

    async def list_prompts(self) -> list[dict[str, Any]]:
        return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "echo":
            raise KeyError(name)
        return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        raise KeyError(uri)

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise KeyError(name)

    async def list_changed_events(self):  # type: ignore[no-untyped-def]
        if False:
            yield "tools"

    def health(self) -> Any:
        from concierge.core.types import AdapterHealth, TransportType

        return AdapterHealth(
            server_id=self.server_id,
            transport=TransportType.CUSTOM,
            connected=self.connected,
            last_connected_at=datetime.now(UTC) if self.connected else None,
        )


def register_fake_upstream() -> None:
    from concierge.adapters.custom import register_custom_adapter

    register_custom_adapter("admin-e2e", FakeAdminE2EAdapter)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fake_id_token(*, nonce: str) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"nonce": nonce, "sub": "admin-e2e"}).encode())
    return f"{header}.{payload}.signature"


def build_fake_oauth_idp() -> Starlette:
    """Return a fake OIDC provider with discovery, token, and revoke endpoints."""

    last_nonce: list[str] = []
    revoke_calls: list[str] = []
    refresh_calls: list[str] = []

    async def discovery(_req: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "revocation_endpoint": f"{ISSUER}/revoke",
            }
        )

    async def authorize(req: Request) -> PlainTextResponse:
        nonce = req.query_params.get("nonce")
        if nonce:
            last_nonce.clear()
            last_nonce.append(nonce)
        return PlainTextResponse("ok")

    async def token(req: Request) -> JSONResponse:
        form = dict(parse_qsl((await req.body()).decode(), keep_blank_values=True))
        grant = form.get("grant_type")
        if grant == "authorization_code":
            if "code_verifier" not in form:
                return JSONResponse({"error": "missing_verifier"}, status_code=400)
            body: dict[str, Any] = {
                "access_token": "admin-e2e-access-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "admin-e2e-refresh-token",
            }
            if last_nonce:
                body["id_token"] = _fake_id_token(nonce=last_nonce[0])
            return JSONResponse(body)
        if grant == "refresh_token":
            token_value = form.get("refresh_token", "")
            refresh_calls.append(token_value)
            return JSONResponse(
                {
                    "access_token": REFRESHED_ACCESS,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )
        if grant == "client_credentials":
            return JSONResponse(
                {
                    "access_token": "admin-e2e-client-token",
                    "token_type": "Bearer",
                    "expires_in": 600,
                }
            )
        return JSONResponse({"error": "unsupported_grant"}, status_code=400)

    async def revoke(req: Request) -> PlainTextResponse:
        form = dict(parse_qsl((await req.body()).decode(), keep_blank_values=True))
        token_value = form.get("token")
        if token_value:
            revoke_calls.append(token_value)
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/.well-known/openid-configuration", discovery, methods=["GET"]),
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
            Route("/revoke", revoke, methods=["POST"]),
        ]
    )
    app.state.last_nonce = last_nonce
    app.state.revoke_calls = revoke_calls
    app.state.refresh_calls = refresh_calls
    return app


def _echo_upstream() -> dict[str, Any]:
    register_fake_upstream()
    return {
        "id": "admin-e2e-echo",
        "transport": "custom",
        "custom_kind": "admin-e2e",
        "custom_params": {},
        "headers": {"Authorization": RAW_HEADER_SECRET},
        "default_tags": ["admin-e2e"],
    }


def _gateway_config_dict(work_dir: Path) -> dict[str, Any]:
    from concierge.config import AuthConfig, GatewayConfig, StorageConfig

    config = GatewayConfig(
        auth=AuthConfig(type="localhost"),
        storage=StorageConfig(
            catalog_store="sqlite",
            catalog_sqlite_path=str(work_dir / "admin-e2e.sqlite"),
        ),
        upstream_servers=[_echo_upstream()],
    )
    return config.model_dump(mode="json")


def _assert_no_secret(obj: Any, *needles: str) -> None:
    dumped = json.dumps(obj, sort_keys=True)
    for needle in needles:
        assert needle not in dumped, f"secret leaked in response: {needle}"


def _assert_status(resp: Any, status_code: int, label: str) -> None:
    assert resp.status_code == status_code, f"{label}: {resp.status_code} {resp.text}"


def _mcp(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session_id: str,
) -> dict:
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params or {}},
        headers={"MCP-Session-Id": session_id},
    )
    _assert_status(resp, 200, f"mcp {method}")
    body = resp.json()
    assert "error" not in body, f"mcp {method} failed: {body}"
    return body["result"]


def _initialize(client: TestClient) -> str:
    resp = client.post("/mcp", json=INIT_RPC)
    _assert_status(resp, 200, "mcp initialize")
    assert "error" not in resp.json(), resp.text
    session_id = resp.headers.get("MCP-Session-Id")
    assert session_id, "initialize did not return MCP-Session-Id"
    return session_id


def _tool_names(client: TestClient, session_id: str) -> set[str]:
    listed = _mcp(client, "tools/list", session_id=session_id)
    return {t["name"] for t in listed["tools"]}


def _build_admin_app(work_dir: Path) -> tuple[Any, Any, Any]:
    """Build the app with fake OAuth HTTP injected into the real admin router."""

    from concierge.admin.credential_store import UpstreamCredentialStore
    from concierge.admin.oauth import OAuthPendingStore, UpstreamOAuthService
    from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key
    from concierge.config import AuthConfig, GatewayConfig, StorageConfig
    from concierge.server import app as app_mod
    from concierge.server.admin_oauth import AdminOAuthDeps

    idp = build_fake_oauth_idp()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp), base_url=ISSUER)
    backend = InMemoryCredentialStore(key=resolve_fernet_key("admin-e2e-oauth-key"))
    credentials = UpstreamCredentialStore(backend=backend)
    oauth_fake = UpstreamOAuthService(
        credential_store=credentials,
        pending=OAuthPendingStore(ttl_s=120.0),
        http_client=http,
    )

    def fake_oauth_deps(
        *, auth: Any, audit: Any, config: Any, oauth: Any = None
    ) -> AdminOAuthDeps:
        # Ignore the app-built ``oauth`` and inject the fake IDP-backed service.
        del oauth
        host = config.gateway.host if config.gateway.host not in ("0.0.0.0", "::") else "127.0.0.1"
        return AdminOAuthDeps(
            auth=auth,
            oauth=oauth_fake,
            credentials=credentials,
            audit=audit,
            public_base_url=f"http://{host}:{config.gateway.port}",
        )

    db_path = work_dir / "admin-e2e.sqlite"
    config = GatewayConfig(
        auth=AuthConfig(type="localhost"),
        storage=StorageConfig(catalog_store="sqlite", catalog_sqlite_path=str(db_path)),
    )
    original = app_mod._build_oauth_deps
    app_mod._build_oauth_deps = fake_oauth_deps
    try:
        app = app_mod.build_app(config)
    finally:
        app_mod._build_oauth_deps = original
    app.state.admin_e2e_oauth = oauth_fake
    app.state.admin_e2e_credentials = credentials
    app.state.admin_e2e_oauth_http = http
    app.state.admin_e2e_idp = idp
    return app, oauth_fake, idp


def _verify_spa_mount(client: TestClient) -> dict[str, Any]:
    index = client.get("/admin/", headers={"Accept": "text/html"})
    _assert_status(index, 200, "admin SPA index")
    assert "text/html" in index.headers.get("content-type", "")
    assert "<!doctype html" in index.text.lower() or "<html" in index.text.lower()

    deep_link = client.get("/admin/upstreams", headers={"Accept": "text/html"})
    _assert_status(deep_link, 200, "admin SPA deep link")
    assert deep_link.text == index.text

    api = client.get("/admin/upstreams", headers={"Accept": "application/json"})
    _assert_status(api, 200, "admin API precedence")
    assert "upstreams" in api.json()

    asset_paths = [
        line.split('src="/admin/', 1)[1].split('"', 1)[0]
        for line in index.text.splitlines()
        if 'src="/admin/assets/' in line
    ]
    if not asset_paths:
        asset_paths = [
            line.split('href="/admin/', 1)[1].split('"', 1)[0]
            for line in index.text.splitlines()
            if 'href="/admin/assets/' in line
        ]
    assert asset_paths, "admin SPA index did not reference /admin/assets"
    asset = client.get(f"/admin/{asset_paths[0]}")
    _assert_status(asset, 200, "admin SPA asset")
    return {"index_bytes": len(index.content), "asset": asset_paths[0]}


def _verify_auth_modes(work_dir: Path) -> dict[str, Any]:
    from concierge.config import AuthConfig, GatewayConfig, StorageConfig
    from concierge.server.app import build_app

    config = GatewayConfig(
        auth=AuthConfig(type="bearer", bearer_tokens=[ADMIN_TOKEN]),
        storage=StorageConfig(
            catalog_store="sqlite",
            catalog_sqlite_path=str(work_dir / "admin-e2e-bearer.sqlite"),
        ),
    )
    with TestClient(build_app(config), client=("127.0.0.1", 50001)) as client:
        denied = client.post("/mcp", json=INIT_RPC)
        _assert_status(denied, 200, "bearer denied without token")
        assert "error" in denied.json(), denied.text
        allowed = client.get("/admin/health", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        _assert_status(allowed, 200, "bearer accepted with token")
    return {"localhost": True, "bearer": True}


def run_admin_release_gate(work_dir: Path) -> dict[str, Any]:
    """Run the single-command admin release gate."""

    register_fake_upstream()
    app, oauth, idp = _build_admin_app(work_dir)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        spa = _verify_spa_mount(client)
        auth = _verify_auth_modes(work_dir)

        health = client.get("/admin/health")
        _assert_status(health, 200, "localhost admin health")
        status = client.get("/admin/config/status")
        _assert_status(status, 200, "config status")
        assert status.json()["config_store"] is True
        assert status.json()["reload_coordinator"] is True

        upstream = _echo_upstream()
        created = client.post("/admin/upstreams", json={"upstream": upstream})
        _assert_status(created, 200, "create upstream draft")
        listed = client.get("/admin/upstreams")
        _assert_status(listed, 200, "list upstreams")
        assert any(u["id"] == upstream["id"] for u in listed.json()["upstreams"])
        _assert_no_secret(listed.json(), RAW_HEADER_SECRET)

        tested = client.post(
            f"/admin/upstreams/{upstream['id']}/test-connection",
            json={"upstream": upstream},
        )
        _assert_status(tested, 200, "test upstream connection")
        test_body = tested.json()
        assert test_body["ok"] is True
        assert test_body["tools_discovered"] >= 1

        apply = client.post("/admin/upstreams/apply")
        _assert_status(apply, 200, "apply upstream draft")
        assert apply.json()["applied"] is True
        active_v1 = client.get("/admin/config/active").json()
        _assert_no_secret(active_v1, RAW_HEADER_SECRET)
        export = client.get("/admin/config/export")
        _assert_status(export, 200, "redacted YAML export")
        _assert_no_secret(export.json(), RAW_HEADER_SECRET)
        assert "***" in export.json()["yaml"]

        session_id = _initialize(client)
        discovered = _mcp(
            client,
            "tools/call",
            {
                "name": "gateway_discover_catalog",
                "arguments": {"server": upstream["id"], "primitive_type": "tool"},
            },
            session_id=session_id,
        )
        entries = discovered["structuredContent"]["entries"]
        assert entries, "no-restart catalog discovery returned no upstream tools"
        tool_name = entries[0]["name"]
        assert tool_name == "admin-e2e-echo__echo"
        _mcp(
            client,
            "tools/call",
            {"name": "gateway_enable_tools", "arguments": {"names": [tool_name]}},
            session_id=session_id,
        )
        assert tool_name in _tool_names(client, session_id), (
            "new tool absent after no-restart apply"
        )

        profile = {
            "name": "admin-e2e-profile",
            "description": "Admin E2E profile",
            "selectors": [{"server": upstream["id"], "primitive_type": "tool"}],
            "auto_apply": True,
        }
        created_profile = client.post("/admin/profiles", json={"profile": profile})
        _assert_status(created_profile, 200, "create profile draft")
        preview = client.post("/admin/profiles/admin-e2e-profile/preview")
        _assert_status(preview, 200, "profile preview")
        assert preview.json()["matched_count"] >= 1
        assert preview.json()["primitives"][0]["canonical_name"] == tool_name
        apply_profile = client.post("/admin/profiles/apply")
        _assert_status(apply_profile, 200, "apply profile draft")
        profile_session = _initialize(client)
        assert tool_name in _tool_names(client, profile_session), (
            "auto profile did not publish tool"
        )

        active_before_failure = client.get("/admin/config/active").json()["version"]["id"]
        bad_config = _gateway_config_dict(work_dir)
        bad_config["upstream_servers"] = [
            {
                "id": "bad-custom",
                "transport": "custom",
                "custom_kind": "missing-admin-e2e-kind",
            }
        ]
        bad_draft = client.put("/admin/config/draft", json={"config": bad_config})
        _assert_status(bad_draft, 200, "create bad reload draft")
        diff = client.get("/admin/config/draft/diff")
        _assert_status(diff, 200, "redacted config diff")
        _assert_no_secret(diff.json(), RAW_HEADER_SECRET)
        failed_apply = client.post("/admin/config/apply")
        _assert_status(failed_apply, 400, "failed reload apply")
        assert client.get("/admin/config/active").json()["version"]["id"] == active_before_failure
        rollback = client.post("/admin/config/rollback")
        _assert_status(rollback, 200, "rollback after failed reload")
        assert rollback.json()["rolled_back"] is True

        invalid = _gateway_config_dict(work_dir)
        invalid["auth"] = {"type": "invalid-auth-mode"}
        invalid_draft = client.put("/admin/config/draft", json={"config": invalid})
        _assert_status(invalid_draft, 200, "create invalid validation draft")
        validation = client.post("/admin/config/draft/validate")
        _assert_status(validation, 200, "config validation errors")
        assert validation.json()["ok"] is False

        discover = client.post("/admin/oauth/admin-e2e-oauth/discover", json={"issuer": ISSUER})
        _assert_status(discover, 200, "oauth discover")
        signin = client.post(
            "/admin/oauth/admin-e2e-oauth/sign-in/start",
            json={
                "issuer": ISSUER,
                "client_id": "admin-e2e-client",
                "client_secret": "admin-e2e-client-secret",
                "scopes": "openid",
                "redirect_uri": "http://testserver/admin/oauth/callback",
            },
        )
        _assert_status(signin, 200, "oauth sign-in start")
        auth_query = signin.json()["authorization_url"].split("?", 1)[1]
        auth_params = dict(parse_qsl(auth_query, keep_blank_values=True))
        idp.state.last_nonce[:] = [auth_params["nonce"]]
        callback = client.get(
            "/admin/oauth/callback",
            params={"code": "admin-e2e-code", "state": signin.json()["state"]},
        )
        _assert_status(callback, 200, "oauth callback")
        oauth_status = client.get("/admin/oauth/admin-e2e-oauth/status")
        _assert_status(oauth_status, 200, "oauth status")
        assert oauth_status.json()["connected"] is True
        _assert_no_secret(
            oauth_status.json(),
            "admin-e2e-access-token",
            "admin-e2e-refresh-token",
            "admin-e2e-client-secret",
        )
        tokens = asyncio.run(app.state.admin_e2e_credentials.load_tokens("admin-e2e-oauth"))
        assert tokens is not None
        tokens.expires_at = time.time() - 60
        asyncio.run(app.state.admin_e2e_credentials.save_tokens("admin-e2e-oauth", tokens))
        refreshed = asyncio.run(
            oauth.refresh_if_needed(
                "admin-e2e-oauth",
                token_endpoint=f"{ISSUER}/token",
                client_id="admin-e2e-client",
                client_secret="admin-e2e-client-secret",
            )
        )
        assert refreshed == REFRESHED_ACCESS
        assert idp.state.refresh_calls == ["admin-e2e-refresh-token"]

    asyncio.run(app.state.admin_e2e_oauth_http.aclose())
    return {
        "spa": spa,
        "auth": auth,
        "upstream": {
            "id": upstream["id"],
            "tools_discovered": test_body["tools_discovered"],
            "tool_available_without_restart": tool_name,
        },
        "profile": {"name": profile["name"], "matched": preview.json()["matched_count"]},
        "rollback_after_failed_reload": True,
        "redaction": {"api_diff_export": True},
        "oauth": {"pkce": True, "refresh_calls": len(idp.state.refresh_calls)},
    }


def run_admin_backend_smoke(work_dir: Path) -> dict[str, Any]:
    """Backward-compatible alias for older tests."""

    return run_admin_release_gate(work_dir)


async def run_fake_oauth_idp_smoke() -> dict[str, Any]:
    """Exercise OAuth discovery, PKCE callback nonce check, and provider revoke."""

    from concierge.admin.credential_store import UpstreamCredentialStore
    from concierge.admin.oauth import OAuthPendingStore, UpstreamOAuthService
    from concierge.admin.secrets import InMemoryCredentialStore, resolve_fernet_key

    idp = build_fake_oauth_idp()
    transport = httpx.ASGITransport(app=idp)
    http = httpx.AsyncClient(transport=transport, base_url=ISSUER)
    backend = InMemoryCredentialStore(key=resolve_fernet_key("admin-e2e-oauth-key"))
    store = UpstreamCredentialStore(backend=backend)
    service = UpstreamOAuthService(
        credential_store=store,
        pending=OAuthPendingStore(ttl_s=120.0),
        http_client=http,
    )
    try:
        discovery = await service.discover(ISSUER)
        auth_url, state = service.begin_authorization_code(
            upstream_id="admin-e2e-oauth",
            discovery=discovery,
            client_id="admin-e2e-client",
            client_secret="admin-e2e-client-secret",
            redirect_uri="http://127.0.0.1:8765/admin/oauth/callback",
            scopes="openid",
        )
        await http.get(auth_url)
        await service.complete_callback(code="admin-e2e-code", state=state)
        status = await store.status("admin-e2e-oauth")
        assert status is not None
        assert status["access_present"] is True

        await service.disconnect("admin-e2e-oauth")
        assert idp.state.revoke_calls == ["admin-e2e-refresh-token"]
        assert await store.status("admin-e2e-oauth") is None
        return {"oauth_revoke_calls": len(idp.state.revoke_calls), "connected": False}
    finally:
        await http.aclose()


def run_all(work_dir: Path) -> dict[str, Any]:
    backend = run_admin_release_gate(work_dir)
    oauth = asyncio.run(run_fake_oauth_idp_smoke())
    return {"backend": backend, "oauth": oauth}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Concierge admin E2E smoke checks.")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Scratch directory for runtime SQLite files.",
    )
    args = parser.parse_args(argv)

    if args.work_dir is None:
        work_dir = DEFAULT_WORK_ROOT / uuid.uuid4().hex
    else:
        work_dir = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    result = run_all(work_dir)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
