"""Encrypted upstream OAuth credential storage (P2-ADMIN-7)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .redaction import redact_for_audit
from .secrets import (
    EncryptedCredentialStore,
    InMemoryCredentialStore,
    is_secret_ref,
    new_credential_id,
    parse_secret_ref,
)


@dataclass
class OAuthTokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str | None = None
    expires_at: float | None = None
    scope: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_endpoint: str | None = None
    revocation_endpoint: str | None = None
    resource: str | None = None
    token_endpoint_auth_method: str | None = None
    flow: str = "authorization_code"

    def is_expired(self, *, skew_s: float = 30.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - skew_s)

    def to_payload(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "resource": self.resource,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "flow": self.flow,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> OAuthTokenSet:
        return cls(
            access_token=str(data["access_token"]),
            token_type=str(data.get("token_type", "Bearer")),
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            scope=data.get("scope"),
            issuer=data.get("issuer"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            token_endpoint=data.get("token_endpoint"),
            revocation_endpoint=data.get("revocation_endpoint"),
            resource=data.get("resource"),
            token_endpoint_auth_method=data.get("token_endpoint_auth_method"),
            flow=str(data.get("flow", "authorization_code")),
        )


def redact_token_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Public inspection view — never includes raw token bytes."""
    out = dict(payload)
    out.pop("access_token", None)
    out.pop("refresh_token", None)
    out.pop("client_secret", None)
    out["access_present"] = True
    out["refresh_present"] = bool(payload.get("refresh_token"))
    return redact_for_audit(out)


class UpstreamCredentialStore:
    """Maps upstream server id → encrypted OAuth token blob (secret_ref in config)."""

    def __init__(self, backend: EncryptedCredentialStore | None = None) -> None:
        self._backend = backend or InMemoryCredentialStore()
        self._upstream_refs: dict[str, str] = {}
        # Dynamically-registered (RFC 7591) OAuth clients, keyed by
        # (upstream_id, authorization-server issuer). Persisted so repeated
        # "Connect" clicks reuse one client_id instead of registering a new one
        # with the authorization server every time.
        self._registration_refs: dict[str, str] = {}

    async def save_tokens(self, upstream_id: str, tokens: OAuthTokenSet) -> str:
        ref = self._upstream_refs.get(upstream_id)
        cred_id = parse_secret_ref(ref) if ref else new_credential_id()
        ref = await self._backend.store(credential_id=cred_id, payload=tokens.to_payload())
        self._upstream_refs[upstream_id] = ref
        return ref

    async def load_tokens(self, upstream_id: str) -> OAuthTokenSet | None:
        ref = self._upstream_refs.get(upstream_id)
        if not ref:
            return None
        try:
            payload = await self._backend.retrieve(ref)
        except KeyError:
            return None
        return OAuthTokenSet.from_payload(payload)

    async def delete_upstream(self, upstream_id: str) -> None:
        ref = self._upstream_refs.pop(upstream_id, None)
        if ref:
            await self._backend.delete(ref)

    @staticmethod
    def _registration_key(upstream_id: str, issuer: str) -> str:
        return f"{upstream_id}\x00{issuer}"

    async def save_client_registration(
        self,
        upstream_id: str,
        issuer: str,
        *,
        client_id: str,
        client_secret: str | None,
        token_endpoint_auth_method: str | None = None,
    ) -> None:
        """Persist a dynamically-registered client for (upstream, issuer)."""
        key = self._registration_key(upstream_id, issuer)
        ref = self._registration_refs.get(key)
        cred_id = parse_secret_ref(ref) if ref else new_credential_id()
        ref = await self._backend.store(
            credential_id=cred_id,
            payload={
                "client_id": client_id,
                "client_secret": client_secret,
                "issuer": issuer,
                "token_endpoint_auth_method": token_endpoint_auth_method,
            },
        )
        self._registration_refs[key] = ref

    async def load_client_registration(
        self, upstream_id: str, issuer: str
    ) -> tuple[str, str | None, str | None] | None:
        """Return ``(client_id, client_secret, token_auth_method)`` for prior DCR."""
        ref = self._registration_refs.get(self._registration_key(upstream_id, issuer))
        if not ref:
            return None
        try:
            payload = await self._backend.retrieve(ref)
        except KeyError:
            return None
        client_id = payload.get("client_id")
        if not client_id:
            return None
        secret = payload.get("client_secret")
        method = payload.get("token_endpoint_auth_method")
        return str(client_id), (str(secret) if secret else None), (str(method) if method else None)

    def get_secret_ref(self, upstream_id: str) -> str | None:
        return self._upstream_refs.get(upstream_id)

    def bind_secret_ref(self, upstream_id: str, ref: str) -> None:
        if not is_secret_ref(ref):
            raise ValueError("expected secret_ref")
        self._upstream_refs[upstream_id] = ref

    async def status(self, upstream_id: str) -> dict[str, Any] | None:
        tokens = await self.load_tokens(upstream_id)
        if tokens is None:
            return None
        return redact_token_status(tokens.to_payload())