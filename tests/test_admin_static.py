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