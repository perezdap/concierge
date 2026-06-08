"""Tests for in-process admin SPA static mount."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from concierge.config import GatewayConfig
from concierge.server.admin_static import resolve_admin_ui_dist
from concierge.server.app import build_app


def test_resolve_admin_ui_dist_finds_repo_build() -> None:
    dist = resolve_admin_ui_dist()
    if (Path(__file__).resolve().parent.parent / "frontend" / "dist" / "index.html").is_file():
        assert dist is not None
        assert (dist / "index.html").is_file()
    else:
        assert dist is None or (dist / "index.html").is_file()


def test_admin_spa_index_and_api_precedence(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist = tmp_path / "ui"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")
    (dist / "index.html").write_text("<html><body>Admin UI</body></html>", encoding="utf-8")
    monkeypatch.setenv("CONCIERGE_ADMIN_UI_DIST", str(dist))

    with TestClient(build_app(gateway_config)) as client:
        spa = client.get("/admin/", headers={"Accept": "text/html"})
        assert spa.status_code == 200
        assert "Admin UI" in spa.text

        shell = client.get(
            "/admin/upstreams",
            headers={"Accept": "text/html"},
        )
        assert shell.status_code == 200
        assert "Admin UI" in shell.text

        api = client.get("/admin/health")
        assert api.status_code == 200
        assert api.json()["ok"] is True

        api_list = client.get(
            "/admin/upstreams",
            headers={"Accept": "application/json"},
        )
        assert api_list.status_code == 200
        assert "upstreams" in api_list.json()

        asset = client.get("/admin/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text


def test_admin_static_cache_headers(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Hashed assets are immutably cacheable; the SPA shell is never cached.

    Regression for the blank-page-on-hard-refresh bug: without explicit
    Cache-Control on hashed assets, browsers heuristically cache Vite bundles
    and can serve a corrupt/partial response, leaving the React tree unmounted.
    """
    dist = tmp_path / "ui"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")
    (dist / "index.html").write_text(
        "<html><body>Admin UI</body></html>", encoding="utf-8"
    )
    monkeypatch.setenv("CONCIERGE_ADMIN_UI_DIST", str(dist))

    with TestClient(build_app(gateway_config)) as client:
        asset = client.get("/admin/assets/app.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"

        for path in ("/admin/", "/admin/upstreams", "/admin/profiles"):
            shell = client.get(path, headers={"Accept": "text/html"})
            assert shell.status_code == 200, path
            cache_control = shell.headers["cache-control"]
            assert "no-cache" in cache_control, (path, cache_control)
            assert "no-store" in cache_control, (path, cache_control)


def _make_dist(tmp_path: Path, monkeypatch) -> Path:
    """Write a minimal Vite-style dist tree and point the gateway at it."""
    dist = tmp_path / "ui"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")
    (dist / "index.html").write_text(
        "<html><body>Admin UI</body></html>", encoding="utf-8"
    )
    monkeypatch.setenv("CONCIERGE_ADMIN_UI_DIST", str(dist))
    return dist


def test_admin_root_redirects_to_slash(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """GET /admin (no trailing slash) is a 308 to the canonical /admin/.

    Regression for GET /admin returning {"detail": "Not Found"} because no
    route claimed the bare prefix.
    """
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        resp = client.get("/admin", follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/"


def test_admin_root_redirect_preserves_query_string(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """GET /admin?foo=bar keeps the query so deep links / OAuth callbacks survive."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        resp = client.get("/admin?foo=bar&baz=1", follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/?foo=bar&baz=1"


def test_admin_unknown_html_path_serves_spa_shell(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Any HTML GET under /admin/ that no API segment claims serves the shell —
    deep React Router routes and stray asset probes alike."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        for path in ("/admin/favicon.ico", "/admin/some/deep/route"):
            resp = client.get(path, headers={"Accept": "text/html"})
            assert resp.status_code == 200, path
            assert "Admin UI" in resp.text, path


def test_admin_asset_probe_does_not_trigger_auth(
    bearer_gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Under bearer auth, a browser HTML probe to an unknown /admin path gets the
    SPA shell, not a 401 — the catch-all route has no auth dependency, so probes
    like /admin/favicon.ico never reach the auth-protected API router."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(bearer_gateway_config)) as client:
        resp = client.get("/admin/favicon.ico", headers={"Accept": "text/html"})
        assert resp.status_code == 200
        assert "Admin UI" in resp.text


def test_admin_ui_absent_is_noop(gateway_config: GatewayConfig, monkeypatch) -> None:
    monkeypatch.setattr(
        "concierge.server.admin_static.resolve_admin_ui_dist",
        lambda: None,
    )
    with TestClient(build_app(gateway_config)) as client:
        assert client.get("/admin/health").status_code == 200
        assert not hasattr(client.app.state, "admin_ui_index") or (
            client.app.state.admin_ui_index is None
        )