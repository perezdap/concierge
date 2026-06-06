"""In-process mount for the built admin SPA (P2-ADMIN-9 / gateway integration)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# Vite emits content-hashed asset filenames (e.g. index-DQ4TiwWc.js), so the
# bytes behind a given URL never change -- they are safe to cache forever.
_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# The SPA shell (index.html) references the current asset hashes, so it must
# never be cached -- otherwise a client traps itself on stale asset names.
_SHELL_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class AdminAssetsStatic(StaticFiles):
    """StaticFiles that marks hashed admin assets as immutably cacheable."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = _ASSET_CACHE_CONTROL
        return response

# First path segment under /admin reserved for JSON API routers (not SPA shell).
_API_SEGMENTS = frozenset({
    "health",
    "catalog",
    "sessions",
    "refresh",
    "tokens",
    "revocations",
    "approvals",
    "config",
    "oauth",
})

# SPA shell pages (React Router); HTML navigations only — JSON API keeps precedence.
_SPA_SHELL_SEGMENTS = frozenset({"upstreams", "profiles"})


def resolve_admin_ui_dist() -> Path | None:
    """Return the admin UI build directory if index.html exists, else None."""
    candidates: list[Path] = []
    env = os.environ.get("CONCIERGE_ADMIN_UI_DIST", "").strip()
    if env:
        candidates.append(Path(env))
    repo_root = Path(__file__).resolve().parents[3]
    candidates.extend([
        repo_root / "frontend" / "dist",
        Path("/app/admin-ui"),
    ])
    for path in candidates:
        index = path / "index.html"
        if index.is_file():
            return path.resolve()
    return None


def _admin_rel_path(url_path: str) -> str:
    path = url_path.rstrip("/")
    if path == "/admin":
        return ""
    prefix = "/admin/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return ""


def wants_admin_spa_index(request: Request) -> bool:
    """True when the request should receive SPA index.html instead of an API handler."""
    if request.method != "GET":
        return False
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return False
    rel = _admin_rel_path(request.url.path)
    if rel.startswith("assets/"):
        return False
    if rel == "":
        return True
    parts = rel.split("/")
    head = parts[0]
    if head in _API_SEGMENTS:
        return False
    if head in _SPA_SHELL_SEGMENTS and len(parts) == 1:
        return True
    if len(parts) == 1:
        return True
    return head not in _API_SEGMENTS


def install_admin_ui(app: FastAPI) -> Path | None:
    """Mount static assets and SPA index fallback after admin JSON routes are registered."""
    dist = resolve_admin_ui_dist()
    if dist is None:
        app.state.admin_ui_dist = None
        return None

    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/admin/assets",
            AdminAssetsStatic(directory=str(assets_dir)),
            name="admin-ui-assets",
        )

    index_path = dist / "index.html"
    app.state.admin_ui_dist = dist
    app.state.admin_ui_index = index_path

    @app.middleware("http")
    async def _admin_spa_index_fallback(request: Request, call_next):  # type: ignore[no-untyped-def]
        ui_index: Path | None = getattr(request.app.state, "admin_ui_index", None)
        if ui_index is not None and wants_admin_spa_index(request):
            return FileResponse(ui_index, headers=dict(_SHELL_CACHE_HEADERS))
        return await call_next(request)

    return dist