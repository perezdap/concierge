"""Upstream OAuth: OIDC discovery, PKCE authorization-code, client-credentials (P2-ADMIN-7)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..util.audit import AuditLogger
from ..util.log import get_logger
from .credential_store import OAuthTokenSet, UpstreamCredentialStore
from .redaction import redact_for_audit

_log = get_logger("concierge.admin.oauth")

EVENT_OAUTH_DISCOVER = "admin.oauth.discover"
EVENT_OAUTH_SIGNIN = "admin.oauth.sign_in"
EVENT_OAUTH_CALLBACK = "admin.oauth.callback"
EVENT_OAUTH_REFRESH = "admin.oauth.refresh"
EVENT_OAUTH_DISCONNECT = "admin.oauth.disconnect"
EVENT_OAUTH_CLIENT_CREDS = "admin.oauth.client_credentials"
EVENT_OAUTH_REVOKE = "admin.oauth.revoke"


@dataclass
class OAuthDiscoveryDocument:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None


@dataclass
class PendingAuthorization:
    upstream_id: str
    state: str
    code_verifier: str
    nonce: str
    redirect_uri: str
    client_id: str
    client_secret: str | None
    token_endpoint: str
    authorization_endpoint: str
    revocation_endpoint: str | None
    issuer: str
    scopes: str
    created_at: float
    expires_at: float


class OAuthPendingStore:
    """Short-TTL server-side storage for PKCE state/nonce/verifier."""

    def __init__(self, *, ttl_s: float = 600.0) -> None:
        self.ttl_s = ttl_s
        self._pending: dict[str, PendingAuthorization] = {}

    def put(self, pending: PendingAuthorization) -> None:
        self._purge()
        self._pending[pending.state] = pending

    def pop(self, state: str) -> PendingAuthorization | None:
        self._purge()
        rec = self._pending.pop(state, None)
        if rec is None:
            return None
        if time.time() > rec.expires_at:
            return None
        return rec

    def _purge(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items() if now > v.expires_at]
        for key in expired:
            self._pending.pop(key, None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return _b64url(digest)


def scopes_require_nonce(scopes: str) -> bool:
    return "openid" in scopes.split()


def decode_jwt_payload_unverified(id_token: str) -> dict[str, object]:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid id_token format")
    payload = json.loads(_b64url_decode(parts[1]).decode())
    if not isinstance(payload, dict):
        raise ValueError("id_token payload must be a JSON object")
    return payload


def validate_id_token_nonce(id_token: str, expected_nonce: str) -> None:
    claims = decode_jwt_payload_unverified(id_token)
    nonce = claims.get("nonce")
    if nonce != expected_nonce:
        raise ValueError("id_token nonce mismatch")


class UpstreamOAuthService:
    def __init__(
        self,
        *,
        credential_store: UpstreamCredentialStore,
        pending: OAuthPendingStore | None = None,
        audit: AuditLogger | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.credentials = credential_store
        self.pending = pending or OAuthPendingStore()
        self.audit = audit
        self._http = http_client

    async def _client(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        return httpx.AsyncClient(timeout=10.0)

    async def discover(self, issuer: str) -> OAuthDiscoveryDocument:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        client = await self._client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
        finally:
            if self._http is None:
                await client.aclose()
        authz = doc.get("authorization_endpoint")
        token = doc.get("token_endpoint")
        if not authz or not token:
            raise ValueError("discovery document missing authorization or token endpoint")
        discovery = OAuthDiscoveryDocument(
            issuer=str(doc.get("issuer", issuer)),
            authorization_endpoint=str(authz),
            token_endpoint=str(token),
            revocation_endpoint=doc.get("revocation_endpoint"),
        )
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_DISCOVER,
                **redact_for_audit({"issuer": discovery.issuer, "ok": True}),
            )
        return discovery

    def begin_authorization_code(
        self,
        *,
        upstream_id: str,
        discovery: OAuthDiscoveryDocument,
        client_id: str,
        redirect_uri: str,
        scopes: str,
        client_secret: str | None = None,
    ) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(16)
        now = time.time()
        pending = PendingAuthorization(
            upstream_id=upstream_id,
            state=state,
            code_verifier=verifier,
            nonce=nonce,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint=discovery.token_endpoint,
            authorization_endpoint=discovery.authorization_endpoint,
            revocation_endpoint=discovery.revocation_endpoint,
            issuer=discovery.issuer,
            scopes=scopes,
            created_at=now,
            expires_at=now + self.pending.ttl_s,
        )
        self.pending.put(pending)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        auth_url = discovery.authorization_endpoint + "?" + urlencode(params)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_SIGNIN,
                **redact_for_audit(
                    {"upstream_id": upstream_id, "state": state, "issuer": discovery.issuer}
                ),
            )
        return auth_url, state

    async def complete_callback(
        self,
        *,
        code: str,
        state: str,
    ) -> OAuthTokenSet:
        pending = self.pending.pop(state)
        if pending is None:
            raise ValueError("invalid or expired OAuth state")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": pending.client_id,
            "code_verifier": pending.code_verifier,
        }
        if pending.client_secret:
            data["client_secret"] = pending.client_secret
        tokens, body = await self._exchange_token(
            pending.token_endpoint,
            data,
            issuer=pending.issuer,
            client_id=pending.client_id,
            client_secret=pending.client_secret,
            revocation_endpoint=pending.revocation_endpoint,
            flow="authorization_code",
        )
        if scopes_require_nonce(pending.scopes):
            id_token = body.get("id_token")
            if not id_token:
                raise ValueError("openid scope requires id_token in token response")
            validate_id_token_nonce(str(id_token), pending.nonce)
        await self.credentials.save_tokens(pending.upstream_id, tokens)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_CALLBACK,
                **redact_for_audit(
                    {"upstream_id": pending.upstream_id, "state": state, "ok": True}
                ),
            )
        return tokens

    async def client_credentials(
        self,
        *,
        upstream_id: str,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scopes: str = "",
        issuer: str | None = None,
        revocation_endpoint: str | None = None,
    ) -> OAuthTokenSet:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scopes:
            data["scope"] = scopes
        tokens, _body = await self._exchange_token(
            token_endpoint,
            data,
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            revocation_endpoint=revocation_endpoint,
            flow="client_credentials",
        )
        await self.credentials.save_tokens(upstream_id, tokens)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_CLIENT_CREDS,
                **redact_for_audit({"upstream_id": upstream_id, "ok": True}),
            )
        return tokens

    async def _exchange_token(
        self,
        token_endpoint: str,
        data: dict[str, str],
        *,
        issuer: str | None,
        client_id: str | None,
        client_secret: str | None,
        revocation_endpoint: str | None,
        flow: str,
    ) -> tuple[OAuthTokenSet, dict[str, object]]:
        client = await self._client()
        try:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
        finally:
            if self._http is None:
                await client.aclose()
        if not isinstance(body, dict):
            raise ValueError("token endpoint returned non-object JSON")
        expires_in = body.get("expires_in")
        expires_at = None
        if expires_in is not None:
            expires_at = time.time() + float(expires_in)
        tokens = OAuthTokenSet(
            access_token=str(body["access_token"]),
            token_type=str(body.get("token_type", "Bearer")),
            refresh_token=body.get("refresh_token"),
            expires_at=expires_at,
            scope=body.get("scope"),
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
            flow=flow,
        )
        return tokens, body

    async def refresh_if_needed(
        self,
        upstream_id: str,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str | None = None,
    ) -> str:
        """Return a valid access token, refreshing when expired."""
        current = await self.credentials.load_tokens(upstream_id)
        if current is None:
            raise KeyError(f"no credentials for upstream {upstream_id}")
        if not current.is_expired():
            return current.access_token
        if not current.refresh_token:
            raise ValueError("access token expired and no refresh_token available")
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        refreshed, _body = await self._exchange_token(
            token_endpoint,
            data,
            issuer=current.issuer,
            client_id=client_id,
            client_secret=client_secret,
            revocation_endpoint=current.revocation_endpoint,
            flow=current.flow,
        )
        if refreshed.refresh_token is None:
            refreshed.refresh_token = current.refresh_token
        await self.credentials.save_tokens(upstream_id, refreshed)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_REFRESH,
                **redact_for_audit({"upstream_id": upstream_id, "ok": True}),
            )
        return refreshed.access_token

    async def valid_token_set(self, upstream_id: str) -> OAuthTokenSet | None:
        """Return a non-expired token set for ``upstream_id``, refreshing if needed.

        Self-contained: the endpoint and client credentials needed to refresh are
        read from the stored :class:`OAuthTokenSet` (persisted at mint time), so
        callers only supply the upstream id. Returns ``None`` when no credential
        is stored. Raises only when a token is expired and cannot be refreshed.
        """
        current = await self.credentials.load_tokens(upstream_id)
        if current is None:
            return None
        if not current.is_expired():
            return current
        if not current.refresh_token or not current.token_endpoint or not current.client_id:
            raise ValueError(
                "access token expired and cannot be refreshed (missing refresh_token, "
                "token_endpoint, or client_id in stored credentials)"
            )
        await self.refresh_if_needed(
            upstream_id,
            token_endpoint=current.token_endpoint,
            client_id=current.client_id,
            client_secret=current.client_secret,
        )
        return await self.credentials.load_tokens(upstream_id)

    async def _revoke_at_provider(
        self,
        tokens: OAuthTokenSet,
        *,
        upstream_id: str = "",
    ) -> bool:
        endpoint = tokens.revocation_endpoint
        if not endpoint or not tokens.client_id:
            return False
        revoke_token = tokens.refresh_token or tokens.access_token
        if not revoke_token:
            return False
        data: dict[str, str] = {
            "token": revoke_token,
            "client_id": tokens.client_id,
        }
        if tokens.client_secret:
            data["client_secret"] = tokens.client_secret
        client = await self._client()
        try:
            resp = await client.post(endpoint, data=data)
            resp.raise_for_status()
            if self.audit:
                self.audit.emit(
                    EVENT_OAUTH_REVOKE,
                    **redact_for_audit(
                        {
                            "upstream_id": upstream_id,
                            "ok": True,
                            "revoked": True,
                        }
                    ),
                )
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("upstream OAuth revoke failed (best-effort): %s", e)
            if self.audit:
                self.audit.emit(
                    EVENT_OAUTH_REVOKE,
                    **redact_for_audit(
                        {"upstream_id": upstream_id, "ok": False, "error": str(e)}
                    ),
                )
            return False
        finally:
            if self._http is None:
                await client.aclose()

    async def disconnect(self, upstream_id: str) -> None:
        tokens = await self.credentials.load_tokens(upstream_id)
        if tokens is not None:
            await self._revoke_at_provider(tokens, upstream_id=upstream_id)
        await self.credentials.delete_upstream(upstream_id)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_DISCONNECT,
                **redact_for_audit({"upstream_id": upstream_id, "ok": True}),
            )

    def authorization_header(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}