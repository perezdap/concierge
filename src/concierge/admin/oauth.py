"""Upstream OAuth: OIDC discovery, PKCE authorization-code, client-credentials (P2-ADMIN-7)."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from ..util.audit import AuditLogger
from ..util.log import get_logger
from .credential_store import OAuthTokenSet, UpstreamCredentialStore
from .redaction import redact_for_audit

_log = get_logger("concierge.admin.oauth")

EVENT_OAUTH_DISCOVER = "admin.oauth.discover"
EVENT_OAUTH_PROBE = "admin.oauth.probe"
EVENT_OAUTH_REGISTER = "admin.oauth.register"
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
    # RFC 7591 Dynamic Client Registration endpoint, when the authorization
    # server advertises one. Presence enables zero-config registration.
    registration_endpoint: str | None = None
    # RFC 8707 resource indicator for the protected MCP resource, when known.
    resource: str | None = None
    token_endpoint_auth_methods_supported: list[str] | None = None


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
    resource: str | None
    token_endpoint_auth_method: str | None
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


def _parse_resource_metadata_url(www_authenticate: str) -> str | None:
    """Extract the ``resource_metadata`` URL from a WWW-Authenticate header.

    Per RFC 9728 Section 5.1, a protected resource's 401 challenge carries a
    ``resource_metadata="<url>"`` parameter pointing at its PRM document.
    """
    match = re.search(r'resource_metadata\s*=\s*"([^"]+)"', www_authenticate)
    if match:
        return match.group(1)
    match = re.search(r"resource_metadata\s*=\s*([^\s,]+)", www_authenticate)
    return match.group(1) if match else None


def _origin_of(url: str) -> str:
    """Return scheme://host[:port] for ``url`` (no path/query)."""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _resource_indicator_for(url: str) -> str:
    """Return the RFC 8707 resource indicator for a protected MCP endpoint.

    Some MCP OAuth providers bind issued access tokens to the protected resource
    origin, not the concrete transport path (e.g. ``https://host`` rather than
    ``https://host/mcp/oauth``).  Prefer the origin form so the token audience
    matches the resource metadata document's ``resource`` value.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _select_dcr_token_endpoint_auth_method(methods: list[str] | None) -> str:
    """Choose a DCR token endpoint auth method Concierge can perform.

    Older tests/providers may omit ``token_endpoint_auth_methods_supported``;
    keep the previous public-client behavior in that case. When an AS explicitly
    advertises supported methods, respect it: prefer public clients when allowed,
    otherwise use a confidential client method that Concierge can satisfy.
    """
    if not methods or "none" in methods:
        return "none"
    if "client_secret_post" in methods:
        return "client_secret_post"
    if "client_secret_basic" in methods:
        return "client_secret_basic"
    raise ValueError(
        "authorization server does not support a compatible token endpoint auth method"
    )


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _same_origin(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` share scheme, host, and (normalized) port.

    An explicit default port (``:443`` for https, ``:80`` for http) is treated
    as equal to an omitted one.
    """

    def _origin(url: str) -> tuple[str, str | None, int | None]:
        p = urlsplit(url)
        port = p.port if p.port is not None else _DEFAULT_PORTS.get(p.scheme)
        return (p.scheme, p.hostname, port)

    return _origin(a) == _origin(b)


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
        # follow_redirects=False is httpx's default, set explicitly here as an
        # SSRF guard: discovery/probe/registration fetch attacker-influenced
        # URLs, and we must not let a redirect bounce them at internal hosts.
        return httpx.AsyncClient(timeout=10.0, follow_redirects=False)

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
            registration_endpoint=doc.get("registration_endpoint"),
        )
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_DISCOVER,
                **redact_for_audit({"issuer": discovery.issuer, "ok": True}),
            )
        return discovery

    async def discover_authorization_server(self, base_url: str) -> OAuthDiscoveryDocument:
        """Discover AS metadata trying OAuth then OIDC well-known endpoints.

        RFC 8414 (oauth-authorization-server) is tried first, then OIDC
        (openid-configuration), per the MCP authorization spec. ``base_url`` is
        the authorization server issuer/base; the well-known path is appended.
        """
        base = base_url.rstrip("/")
        last_err: Exception | None = None
        for suffix in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            client = await self._client()
            try:
                resp = await client.get(base + suffix)
                resp.raise_for_status()
                doc = resp.json()
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
            finally:
                if self._http is None:
                    await client.aclose()
            authz = doc.get("authorization_endpoint")
            token = doc.get("token_endpoint")
            if not authz or not token:
                last_err = ValueError("AS metadata missing authorization or token endpoint")
                continue
            methods = doc.get("token_endpoint_auth_methods_supported")
            return OAuthDiscoveryDocument(
                issuer=str(doc.get("issuer", base)),
                authorization_endpoint=str(authz),
                token_endpoint=str(token),
                revocation_endpoint=doc.get("revocation_endpoint"),
                registration_endpoint=doc.get("registration_endpoint"),
                token_endpoint_auth_methods_supported=(
                    [str(m) for m in methods] if isinstance(methods, list) else None
                ),
            )
        raise ValueError(f"could not discover authorization server at {base}: {last_err}")

    async def probe_resource_metadata(self, resource_url: str) -> list[str]:
        """Probe an MCP server for its authorization server(s) (RFC 9728).

        Per the MCP auth spec, an unauthenticated request to a protected MCP
        server returns ``401`` with a ``WWW-Authenticate`` header carrying a
        ``resource_metadata`` URL. We fetch that Protected Resource Metadata
        document and return its ``authorization_servers`` list.

        Falls back to the conventional ``/.well-known/oauth-protected-resource``
        path when the header is absent. Returns an empty list when the resource
        is not protected (no 401) or advertises no authorization servers.
        """
        client = await self._client()
        try:
            resp = await client.get(resource_url)
            prm_url: str | None = None
            if resp.status_code == 401:
                candidate = _parse_resource_metadata_url(resp.headers.get("WWW-Authenticate", ""))
                # SSRF guard: the resource_metadata URL is attacker-controlled
                # (an upstream response header). RFC 9728 requires the PRM
                # document to live on the protected resource's own origin, so a
                # cross-origin pointer is illegitimate — ignore it and fall back
                # to the well-known path rather than fetching an arbitrary host.
                if candidate and _same_origin(candidate, resource_url):
                    prm_url = candidate
            if prm_url is None:
                # Fall back to the well-known PRM path on the resource origin.
                origin = _origin_of(resource_url)
                prm_url = origin + "/.well-known/oauth-protected-resource"
            meta_resp = await client.get(prm_url)
            if meta_resp.status_code != 200:
                return []
            doc = meta_resp.json()
        except Exception:  # noqa: BLE001
            return []
        finally:
            if self._http is None:
                await client.aclose()
        servers = doc.get("authorization_servers") if isinstance(doc, dict) else None
        result = [str(s) for s in servers] if isinstance(servers, list) else []
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_PROBE,
                **redact_for_audit({"resource": resource_url, "as_count": len(result)}),
            )
        return result

    async def register_client(
        self,
        *,
        registration_endpoint: str,
        redirect_uri: str,
        client_name: str = "Concierge MCP Gateway",
        resource: str | None = None,
        token_endpoint_auth_method: str = "none",
    ) -> tuple[str, str | None]:
        """Dynamically register an OAuth client (RFC 7591).

        Returns ``(client_id, client_secret)``; ``client_secret`` is ``None`` for
        public clients. Registers as a ``native`` application using the
        authorization-code grant with PKCE. If the authorization server does not
        advertise public clients (``token_endpoint_auth_method: none``), callers
        may request a confidential method and persist the returned secret.
        """
        payload = {
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "application_type": "native",
        }
        if resource:
            # Some MCP OAuth providers bind dynamically-registered clients to a
            # protected resource, then mint audience-specific access tokens for
            # that resource during the authorization-code exchange.
            payload["resource"] = resource
        client = await self._client()
        try:
            resp = await client.post(registration_endpoint, json=payload)
            resp.raise_for_status()
            doc = resp.json()
        finally:
            if self._http is None:
                await client.aclose()
        if not isinstance(doc, dict) or not doc.get("client_id"):
            raise ValueError("dynamic client registration returned no client_id")
        client_id = str(doc["client_id"])
        secret = doc.get("client_secret")
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_REGISTER,
                **redact_for_audit(
                    {"registration_endpoint": registration_endpoint, "ok": True}
                ),
            )
        return client_id, (str(secret) if secret else None)

    def begin_authorization_code(
        self,
        *,
        upstream_id: str,
        discovery: OAuthDiscoveryDocument,
        client_id: str,
        redirect_uri: str,
        scopes: str,
        client_secret: str | None = None,
        extra_authorize_params: dict[str, str] | None = None,
        resource: str | None = None,
        token_endpoint_auth_method: str | None = None,
    ) -> tuple[str, str]:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(16)
        now = time.time()
        token_resource = resource or discovery.resource
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
            resource=token_resource,
            token_endpoint_auth_method=token_endpoint_auth_method,
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
        if token_resource:
            params["resource"] = token_resource
        # Provider-specific quirks (e.g. Google access_type=offline, Atlassian
        # audience). Never allowed to override the security-critical PKCE/state
        # params above.
        if extra_authorize_params:
            for key, value in extra_authorize_params.items():
                params.setdefault(key, value)
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
            "code_verifier": pending.code_verifier,
        }
        if pending.token_endpoint_auth_method != "client_secret_basic":
            data["client_id"] = pending.client_id
        if pending.resource:
            data["resource"] = pending.resource
        if (
            pending.client_secret
            and pending.token_endpoint_auth_method != "client_secret_basic"
        ):
            data["client_secret"] = pending.client_secret
        headers = self._token_request_headers(
            client_id=pending.client_id,
            client_secret=pending.client_secret,
            token_endpoint_auth_method=pending.token_endpoint_auth_method,
        )
        tokens, body = await self._exchange_token(
            pending.token_endpoint,
            data,
            headers=headers,
            issuer=pending.issuer,
            client_id=pending.client_id,
            client_secret=pending.client_secret,
            revocation_endpoint=pending.revocation_endpoint,
            resource=pending.resource,
            token_endpoint_auth_method=pending.token_endpoint_auth_method,
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
        resource: str | None = None,
    ) -> OAuthTokenSet:
        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scopes:
            data["scope"] = scopes
        if resource:
            data["resource"] = resource
        tokens, _body = await self._exchange_token(
            token_endpoint,
            data,
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            revocation_endpoint=revocation_endpoint,
            resource=resource,
            token_endpoint_auth_method="client_secret_post",
            flow="client_credentials",
        )
        await self.credentials.save_tokens(upstream_id, tokens)
        if self.audit:
            self.audit.emit(
                EVENT_OAUTH_CLIENT_CREDS,
                **redact_for_audit({"upstream_id": upstream_id, "ok": True}),
            )
        return tokens

    @staticmethod
    def _token_request_headers(
        *,
        client_id: str | None,
        client_secret: str | None,
        token_endpoint_auth_method: str | None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token_endpoint_auth_method == "client_secret_basic" and client_id and client_secret:
            raw = f"{client_id}:{client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    async def _exchange_token(
        self,
        token_endpoint: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        issuer: str | None,
        client_id: str | None,
        client_secret: str | None,
        revocation_endpoint: str | None,
        resource: str | None,
        token_endpoint_auth_method: str | None,
        flow: str,
    ) -> tuple[OAuthTokenSet, dict[str, object]]:
        client = await self._client()
        try:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers=headers or {"Accept": "application/json"},
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
            resource=resource,
            token_endpoint_auth_method=token_endpoint_auth_method,
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
        }
        if current.token_endpoint_auth_method != "client_secret_basic":
            data["client_id"] = client_id
        if current.resource:
            data["resource"] = current.resource
        if client_secret and current.token_endpoint_auth_method != "client_secret_basic":
            data["client_secret"] = client_secret
        headers = self._token_request_headers(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint_auth_method=current.token_endpoint_auth_method,
        )
        refreshed, _body = await self._exchange_token(
            token_endpoint,
            data,
            headers=headers,
            issuer=current.issuer,
            client_id=client_id,
            client_secret=client_secret,
            revocation_endpoint=current.revocation_endpoint,
            resource=current.resource,
            token_endpoint_auth_method=current.token_endpoint_auth_method,
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
        # client-credentials grants typically have no refresh_token; the correct
        # "refresh" is to re-run the client-credentials exchange with the stored
        # endpoint/client credentials.
        if current.flow == "client_credentials":
            if not current.token_endpoint or not current.client_id or not current.client_secret:
                raise ValueError(
                    "client-credentials token expired and cannot be re-minted "
                    "(missing token_endpoint, client_id, or client_secret in stored credentials)"
                )
            return await self.client_credentials(
                upstream_id=upstream_id,
                token_endpoint=current.token_endpoint,
                client_id=current.client_id,
                client_secret=current.client_secret,
                scopes=current.scope or "",
                issuer=current.issuer,
                revocation_endpoint=current.revocation_endpoint,
                resource=current.resource,
            )
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