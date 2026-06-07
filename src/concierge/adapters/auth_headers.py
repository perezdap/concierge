"""Dynamic auth-header injection for HTTP/SSE upstream adapters.

HTTP-based adapters send a static set of headers from config. When an upstream
is connected via the admin OAuth flows (authorization-code+PKCE or
client-credentials, see :mod:`concierge.admin.oauth`), the resulting token is
persisted in the :class:`UpstreamCredentialStore` but was never read back into
the adapter. This module bridges that gap: an :class:`AuthHeaderProvider` is
consulted before every upstream request and returns the ``Authorization`` header
to merge over the adapter's static base headers, refreshing the token on expiry.

Invariants:
* No stored credential for the server id ⇒ empty dict ⇒ adapter behavior is
  unchanged (static config headers only). Fully backwards compatible.
* Raw tokens are never logged here; only the returned header dict is used.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..admin.oauth import UpstreamOAuthService
from ..util.log import get_logger

_log = get_logger("concierge.adapter.auth_headers")


@runtime_checkable
class AuthHeaderProvider(Protocol):
    """Returns per-request auth headers for a given upstream server id."""

    async def headers(self, server_id: str) -> dict[str, str]:
        """Return headers to merge over the adapter's base headers.

        Must return an empty dict (never raise) when no credential applies, so
        adapters can call this unconditionally.
        """
        ...


class OAuthAuthHeaderProvider:
    """Injects ``Authorization`` from stored upstream OAuth tokens.

    Keys credentials by ``server_id`` (treated as the upstream id used by the
    admin OAuth flows). Refreshes expired tokens via the stored token-set's own
    endpoint/client credentials.
    """

    def __init__(self, oauth: UpstreamOAuthService) -> None:
        self._oauth = oauth

    async def headers(self, server_id: str) -> dict[str, str]:
        try:
            tokens = await self._oauth.valid_token_set(server_id)
        except Exception as e:  # noqa: BLE001
            # Refresh failed (e.g. revoked refresh_token). Do not block the
            # request with a stale/invalid header; let the upstream reject it.
            _log.warning("oauth header injection skipped for %s: %s", server_id, e)
            return {}
        if tokens is None:
            return {}
        token_type = tokens.token_type or "Bearer"
        return {"Authorization": f"{token_type} {tokens.access_token}"}
