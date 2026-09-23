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
    route claimed the bare prefix. The text/html case is the real browser entry
    path — the SPA fallback middleware must NOT preempt the redirect there.
    """
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        for accept in (None, "text/html", "text/html,application/xhtml+xml"):
            headers = {"Accept": accept} if accept else {}
            resp = client.get("/admin", headers=headers, follow_redirects=False)
            assert resp.status_code == 308, accept
            assert resp.headers["location"] == "/admin/", accept


def test_admin_root_redirect_preserves_query_string(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """GET /admin?foo=bar keeps the query so deep links / OAuth callbacks survive,
    including the real-browser text/html case (not preempted by the middleware)."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        resp = client.get(
            "/admin?foo=bar&baz=1",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
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


def test_admin_head_unknown_html_path_serves_shell(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HEAD is treated like GET: an HTML probe to an unknown /admin route gets a
    200 with shell headers (and no body), not a 404 from the catch-all."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        resp = client.head("/admin/some/deep/route", headers={"Accept": "text/html"})
        assert resp.status_code == 200
        assert resp.content == b""  # HEAD: headers only, no body


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

        # Image-style favicon probe (browsers send Accept: image/*): it does not
        # want the HTML shell, so the catch-all returns a bare 404 — crucially
        # NOT a 401, i.e. it still never reaches the auth-protected API router.
        img = client.get("/admin/favicon.ico", headers={"Accept": "image/*"})
        assert img.status_code == 404


def test_admin_spa_index_accept_header_is_case_insensitive(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An unusual client sending ``Text/HTML`` still gets the shell, not the
    bare-404 / auth path."""
    _make_dist(tmp_path, monkeypatch)
    with TestClient(build_app(gateway_config)) as client:
        resp = client.get("/admin/some/route", headers={"Accept": "Text/HTML"})
        assert resp.status_code == 200
        assert "Admin UI" in resp.text


def test_admin_route_ordering_contract(
    gateway_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Guard the manual app.router.routes surgery: the /admin redirect must be
    matched before the API routers, and the SPA catch-all after them. Detects
    regressions from Starlette/FastAPI upgrades or future route re-ordering."""
    _make_dist(tmp_path, monkeypatch)
    app = build_app(gateway_config)
    paths = [getattr(r, "path", None) for r in app.router.routes]
    redirect_idx = paths.index("/admin")
    catchall_idx = paths.index("/admin/{rest_of_path:path}")
    assert redirect_idx < catchall_idx, "redirect must win over catch-all"

    # Newer FastAPI/Starlette versions may store include_router routes behind
    # internal grouped route objects with no concrete top-level path, so direct
    # path introspection no longer reliably sees /admin/health. Assert the same
    # precedence contract behaviorally instead: the API route must still beat the
    # SPA catch-all, while unknown HTML routes still fall through to the shell.
    with TestClient(app) as client:
        api = client.get("/admin/health", headers={"Accept": "text/html"})
        assert api.status_code == 200
        assert api.headers["content-type"].startswith("application/json")
        shell = client.get("/admin/some/deep/route", headers={"Accept": "text/html"})
        assert shell.status_code == 200
        assert "Admin UI" in shell.text


def test_admin_ui_absent_is_noop(gateway_config: GatewayConfig, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "concierge.server.admin_static.resolve_admin_ui_dist",
        lambda: None,
    )
    # build_app's configure_logging replaces root handlers (dropping caplog's),
    # so assert on the stderr stream it installs instead.
    app = build_app(gateway_config)
    assert "admin UI not built" in capsys.readouterr().err
    with TestClient(app) as client:
        assert client.get("/admin/health").status_code == 200
        assert not hasattr(client.app.state, "admin_ui_index") or (
            client.app.state.admin_ui_index is None
        )