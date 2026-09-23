"""In-process mount for the built admin SPA (P2-ADMIN-9 / gateway integration)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.responses import Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from ..util.log import get_logger

_log = get_logger("concierge.admin_static")

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

# Note: the previous _SPA_SHELL_SEGMENTS allow-list was removed in favour of a
# permissive catch-all: any GET under /admin/ that asks for text/html now
# receives the SPA shell, which is the standard SPA behavior (React Router
# owns the rest of the path). API segments are still excluded.


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
    """True when the request should receive SPA index.html instead of an API handler.

    Rules:
    - GET or HEAD only (POST/DELETE/PATCH go to the API or fail). HEAD is
      treated like GET so ``curl -I`` / cache probes against SPA routes get the
      same answer as a GET would.
    - ``Accept: text/html`` required — JSON / image / font requests pass through.
    - The relative path under ``/admin/`` must not be a known API segment and
      must not be a hashed asset (``/admin/assets/...``).
    - Unknown first segments (e.g. ``/admin/favicon.ico``, ``/admin/logo.svg``,
      ``/admin/anything-else``) ALSO serve the SPA shell when the browser asked
      for HTML. This is the SPA-catch-all behavior that prevents the auth dep
      from ever running on browser asset probes like ``/favicon.ico``.
    """
    if request.method not in ("GET", "HEAD"):
        return False
    # The bare prefix (no trailing slash) belongs to the 308 redirect route.
    # The SPA fallback middleware is registered last and so runs outermost; if
    # it served the shell here it would preempt canonicalization to /admin/ for
    # real browser (Accept: text/html) requests — the common entry case.
    if request.url.path == "/admin":
        return False
    # Case-insensitive: ``Accept`` is a token-list and a proxy/client may send
    # ``Text/HTML``; a literal substring check would wrongly route it to the API.
    accept = request.headers.get("accept", "").lower()
    if "text/html" not in accept:
        return False
    rel = _admin_rel_path(request.url.path)
    if rel.startswith("assets/"):
        return False
    if rel == "":
        return True
    head = rel.split("/", 1)[0]
    if head in _API_SEGMENTS:
        return False
    # Anything else under /admin/ that the browser asked for as HTML is a SPA
    # route (React Router) or an unknown asset — serve the shell in both cases.
    return True


def install_admin_ui(app: FastAPI) -> Path | None:
    """Mount static assets, the ``/admin`` → ``/admin/`` redirect, and the SPA
    index fallback.

    The redirect and the SPA catch-all are registered as real Starlette routes
    (not just middleware) so they win the route-matching race against the API
    routers mounted at the same ``/admin`` prefix. This is the structural fix
    for ``GET /admin`` returning ``{"detail": "Not Found"}`` and for asset
    requests like ``/admin/favicon.ico`` leaking into the auth-protected API
    router.
    """
    dist = resolve_admin_ui_dist()
    if dist is None:
        # Without this, GET /admin/ is a bare 404 with no hint why.
        _log.warning(
            "admin UI not built: /admin/ will return 404. "
            "Run `python scripts/build_frontend.py --install` or set CONCIERGE_ADMIN_UI_DIST."
        )
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

    def _admin_redirect(request: Request) -> RedirectResponse:
        # Permanent (308) is semantically right: the canonical URL is /admin/.
        # Preserve the query string so ``/admin?foo=bar`` lands on
        # ``/admin/?foo=bar`` (deep links / OAuth callbacks read it).
        target = "/admin/"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=308)

    def _admin_spa_catchall(request: Request) -> Response:
        # Catch-all for unknown /admin/* paths. Only serves the shell for
        # GET/HEAD that asked for HTML. Because this Route is appended last, the
        # path it sees was claimed by no API router; a non-HTML request here is
        # deliberately answered with a bare 404 rather than handed to an
        # auth-protected router (that is the whole point — keep browser asset
        # probes like /admin/favicon.ico off the auth path). Known API-segment
        # paths still match their own routers earlier and never reach here.
        if wants_admin_spa_index(request):
            # FileResponse detects HEAD from the request scope and sends
            # headers only, so the same call serves both GET and HEAD.
            return FileResponse(index_path, headers=dict(_SHELL_CACHE_HEADERS))
        return Response(status_code=404)

    # The redirect is inserted at the FRONT so ``GET /admin`` wins the route
    # race (it doesn't match any /admin/<x> path). The SPA catch-all is
    # APPENDED last so the API routers (mounted at /admin/health,
    # /admin/catalog, ...) match first; the catch-all only ever sees paths
    # no API router claimed, and serves the SPA shell for them.
    app.router.routes.insert(
        0,
        Route("/admin", endpoint=_admin_redirect, methods=["GET", "HEAD"]),
    )
    app.router.routes.append(
        Route(
            "/admin/{rest_of_path:path}",
            endpoint=_admin_spa_catchall,
            methods=["GET", "HEAD"],
        ),
    )

    @app.middleware("http")
    async def _admin_spa_index_fallback(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Defensive: the routes above should already cover this, but keep the
        # middleware as a belt-and-braces guard for any future code path that
        # re-orders the router.
        ui_index: Path | None = getattr(request.app.state, "admin_ui_index", None)
        if ui_index is not None and wants_admin_spa_index(request):
            return FileResponse(ui_index, headers=dict(_SHELL_CACHE_HEADERS))
        return await call_next(request)

    return dist