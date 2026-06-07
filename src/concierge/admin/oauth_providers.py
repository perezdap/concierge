"""Built-in OAuth provider presets for one-click upstream connection.

End users should never have to know a provider's authorization/token endpoints,
required scopes, or refresh-token quirks. This module hardcodes those details for
the common SaaS providers (GitHub, Google, Microsoft 365, Atlassian) so the admin
console can offer a "Connect with <provider>" button.

Two operator-supplied secrets per provider are required to register the OAuth app
with the provider (this is the standard "bring-your-own OAuth app" model used by
self-hosted tools like GitLab, Grafana, and Mattermost):

* ``CONCIERGE_OAUTH_<PROVIDER>_CLIENT_ID``
* ``CONCIERGE_OAUTH_<PROVIDER>_CLIENT_SECRET``

When both are present the provider is ``ready`` and the UI shows a one-click
button; otherwise the UI falls back to asking the operator to paste credentials.

Provider quirks captured here are what make auto-refresh actually work:

* **Google** — requires ``access_type=offline`` + ``prompt=consent`` or no refresh
  token is issued.
* **Atlassian** — requires ``audience=api.atlassian.com`` + ``prompt=consent``;
  refresh tokens require the ``offline_access`` scope. No OIDC discovery for the
  3LO token endpoint, so endpoints are hardcoded.
* **Microsoft** — requires the ``offline_access`` scope for refresh; uses the
  ``/common`` tenant for multi-org sign-in.
* **GitHub** — has no OIDC discovery document, so endpoints must be hardcoded.

Secrets are read lazily at request time and are NEVER serialized into the public
provider catalog returned to the browser.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OAuthProviderPreset:
    """Static, non-secret configuration for a built-in OAuth provider."""

    id: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    default_scopes: str
    issuer: str | None = None
    revocation_endpoint: str | None = None
    # Extra query params appended to the authorization URL (provider quirks).
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    # True when the provider exposes an OIDC discovery document at
    # ``<issuer>/.well-known/openid-configuration``. GitHub/Atlassian do not.
    supports_discovery: bool = False

    @property
    def env_client_id(self) -> str:
        return f"CONCIERGE_OAUTH_{self.id.upper()}_CLIENT_ID"

    @property
    def env_client_secret(self) -> str:
        return f"CONCIERGE_OAUTH_{self.id.upper()}_CLIENT_SECRET"

    def client_id(self) -> str | None:
        value = os.environ.get(self.env_client_id, "").strip()
        return value or None

    def client_secret(self) -> str | None:
        value = os.environ.get(self.env_client_secret, "").strip()
        return value or None

    def is_ready(self) -> bool:
        """True when operator-configured app credentials are present."""
        return bool(self.client_id() and self.client_secret())


# prompt=consent is forced on every provider that gates refresh-token issuance on
# the first consent, so a reconnect always re-issues a refresh token (otherwise
# silent refresh-token loss on the second sign-in). See module docstring.
_PRESETS: dict[str, OAuthProviderPreset] = {
    "github": OAuthProviderPreset(
        id="github",
        display_name="GitHub",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        default_scopes="read:user",
        supports_discovery=False,
    ),
    "google": OAuthProviderPreset(
        id="google",
        display_name="Google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        default_scopes="openid email profile",
        issuer="https://accounts.google.com",
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        supports_discovery=True,
    ),
    "microsoft": OAuthProviderPreset(
        id="microsoft",
        display_name="Microsoft 365",
        authorization_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        default_scopes="openid email profile offline_access",
        issuer="https://login.microsoftonline.com/common/v2.0",
        extra_authorize_params={"prompt": "consent"},
        supports_discovery=False,
    ),
    "atlassian": OAuthProviderPreset(
        id="atlassian",
        display_name="Atlassian",
        authorization_endpoint="https://auth.atlassian.com/authorize",
        token_endpoint="https://auth.atlassian.com/oauth/token",
        default_scopes="read:me offline_access",
        extra_authorize_params={
            "audience": "api.atlassian.com",
            "prompt": "consent",
        },
        supports_discovery=False,
    ),
}


def get_provider(provider_id: str) -> OAuthProviderPreset | None:
    """Return the preset for ``provider_id`` (case-insensitive) or ``None``."""
    return _PRESETS.get(provider_id.strip().lower())


def list_providers() -> list[OAuthProviderPreset]:
    """Return all built-in provider presets in display order."""
    return list(_PRESETS.values())


def public_catalog() -> list[dict[str, object]]:
    """Browser-safe provider catalog. Never includes client secrets.

    ``ready`` reflects whether operator app credentials are configured, which the
    UI uses to choose between a one-click button and a credential-entry fallback.
    """
    catalog: list[dict[str, object]] = []
    for preset in _PRESETS.values():
        catalog.append(
            {
                "id": preset.id,
                "display_name": preset.display_name,
                "default_scopes": preset.default_scopes,
                "ready": preset.is_ready(),
                "env_client_id": preset.env_client_id,
                "env_client_secret": preset.env_client_secret,
            }
        )
    return catalog
