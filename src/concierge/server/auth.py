"""AuthProvider implementations for client→gateway access (P1-1).

Pluggable, config-selectable authentication behind one ABC. Providers:

* :class:`NoAuth`            — open (dev only).
* :class:`LocalhostAllowAuth`— loopback-only (the P0-1 pre-exposure default).
* :class:`StaticBearerAuth`  — the P0-2 static bearer, unchanged: SHA-256 digests,
  constant-time compare, opaque salted audit subject, no raw token bytes anywhere.
* :class:`TenantBearerAuth`  — gateway-minted opaque tokens mapped to a tenant id
  (see :mod:`.tenant_tokens`).
* :class:`JwtBearerAuth`     — offline HS256 JWT against a static JWKS (no network;
  handy for tests / symmetric setups).
* :class:`OidcAuth`          — full OIDC/OAuth2: discovery + JWKS fetch, RS256/ES256
  signature verify, standard-claim verification, allowed issuers + audiences.
* :class:`ProviderChain`     — runs several providers in order; first whose credential
  *shape* matches wins; deny if none match. Enforces the revocation list centrally.

Security invariants (carried from P0-2, extended for P1-1):
  - Deny-by-default. Constant-time secret compares (``hmac.compare_digest``).
  - Exact matching only — never prefix/substring.
  - Never log raw token / id_token / cert bytes. Audit subjects are opaque salted
    digests or non-reversible ids.
  - Every successful auth yields a ``tenant_id`` and an opaque ``token_id``; the
    chain rejects any ``token_id`` present in the revocation list.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from fastapi import Request

from ..errors import Unauthorized
from ..util.log import get_logger
from .revocation import RevocationStore
from .tenant_tokens import TenantTokenStore

try:  # PyJWT is an optional ("auth") extra; only OidcAuth needs it.
    import jwt as _pyjwt
except ImportError:  # pragma: no cover - exercised only when pyjwt is absent
    _pyjwt = None  # type: ignore[assignment]

_log = get_logger("concierge.auth")

# Domain-separation salts for deriving opaque audit ids. Bumping a version
# invalidates previously-emitted ids (intended).
_TOKEN_ID_SALT = b"concierge/auth/token-id/v1"
_JWT_SUBJECT_SALT = b"concierge/auth/jwt-subject/v1"
_CERT_SUBJECT_SALT = b"concierge/auth/cert-subject/v1"


def _token_id(token_digest: bytes) -> str:
    """Stable, non-reversible id for a token, safe to put in logs/audit."""
    return hashlib.sha256(_TOKEN_ID_SALT + token_digest).hexdigest()[:12]


def _jwt_subject_id(*, sub: str, jti: str | None) -> str:
    """Opaque audit subject for a validated JWT — never raw token bytes."""
    material = sub.encode("utf-8") + b"\x00" + (jti or "").encode("utf-8")
    return hashlib.sha256(_JWT_SUBJECT_SALT + material).hexdigest()[:12]


def _cert_subject_id(principal: str) -> str:
    """Opaque audit id derived from a client-cert subject/SAN."""
    return hashlib.sha256(_CERT_SUBJECT_SALT + principal.encode("utf-8")).hexdigest()[:12]


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _bearer_token(request: Request) -> str | None:
    """Extract the bearer credential, or None if no bearer header is present."""
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


# ---------------------------------------------------------------------------
# Result + config shapes
# ---------------------------------------------------------------------------


class AuthResult:
    """Outcome of a successful authentication.

    ``token_id`` is the credential's revocation key — a JWT ``jti`` (or a derived
    id when ``jti`` is absent), or an opaque id for static / tenant tokens. The
    chain rejects any result whose ``token_id`` is on the revocation list.
    """

    __slots__ = ("subject", "tenant_id", "token_id")

    def __init__(
        self,
        subject: str | None = None,
        tenant_id: str = "default",
        token_id: str | None = None,
    ) -> None:
        self.subject = subject
        self.tenant_id = tenant_id
        self.token_id = token_id


@dataclass
class AuthProviderConfig:
    """Programmatic shape mirrored by ``config.AuthConfig`` and ``_build_auth``."""

    provider: Literal["static", "jwt"] = "static"
    bearer_tokens: list[str] = field(default_factory=list)
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks: dict[str, Any] | None = None
    jwt_revoked_jti: set[str] = field(default_factory=set)
    jwt_leeway_s: int = 0


# ---------------------------------------------------------------------------
# ABC + trivial providers
# ---------------------------------------------------------------------------


class AuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> AuthResult: ...

    def matches(self, request: Request) -> bool:
        """Whether this provider claims the request's credential shape.

        Used by :class:`ProviderChain` to route. Default: always attempt (so a
        single configured provider behaves exactly as before).
        """
        return True


class NoAuth(AuthProvider):
    async def authenticate(self, request: Request) -> AuthResult:
        return AuthResult(subject=None)


class StaticBearerAuth(AuthProvider):
    """Validate a static bearer token in constant time (P0-2, unchanged).

    Tokens are kept only as SHA-256 digests. The presented token is hashed and
    compared with ``hmac.compare_digest`` against every configured digest with
    no early exit, so validation time leaks neither how much of the token
    matched nor which token matched. The audit subject is an opaque, salted
    token id — raw token bytes never reach logs or audit records.
    """

    def __init__(self, tokens: list[str]) -> None:
        # Defensive: drop empty strings so a mis-configured ${VAR:-} that expands
        # to "" does not accidentally allow "Authorization: Bearer ".
        self._digests = [hashlib.sha256(t.encode("utf-8")).digest() for t in tokens if t]

    def matches(self, request: Request) -> bool:
        return _bearer_token(request) is not None

    async def authenticate(self, request: Request) -> AuthResult:
        token = _bearer_token(request)
        if token is None:
            raise Unauthorized("missing bearer token")
        if not self._digests:
            raise Unauthorized("invalid bearer token")
        presented = hashlib.sha256(token.encode("utf-8")).digest()
        matched = False
        for digest in self._digests:
            # Accumulate without short-circuiting to keep timing constant.
            if hmac.compare_digest(presented, digest):
                matched = True
        if not matched:
            raise Unauthorized("invalid bearer token")
        tid = _token_id(presented)
        return AuthResult(subject=f"token:{tid}", token_id=f"token:{tid}")


class TenantBearerAuth(AuthProvider):
    """Resolve gateway-minted opaque tokens to a tenant id via a token store.

    The presented token is matched (constant time) against the stored digests;
    a hit yields the bound ``tenant_id`` and the token's opaque ``token_id`` (the
    revocation key). Raw tokens are never stored or logged — see
    :mod:`.tenant_tokens`.
    """

    def __init__(self, store: TenantTokenStore) -> None:
        self._store = store

    def matches(self, request: Request) -> bool:
        return _bearer_token(request) is not None

    async def authenticate(self, request: Request) -> AuthResult:
        token = _bearer_token(request)
        if token is None:
            raise Unauthorized("missing bearer token")
        lookup = await self._store.resolve(token)
        if lookup is None:
            raise Unauthorized("invalid bearer token")
        return AuthResult(
            subject=f"tenant-token:{lookup.token_id}",
            tenant_id=lookup.tenant_id,
            token_id=lookup.token_id,
        )


class LocalhostAllowAuth(AuthProvider):
    """Allow connections that arrive from the loopback interface only."""

    async def authenticate(self, request: Request) -> AuthResult:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return AuthResult(subject="localhost")
        raise Unauthorized("non-localhost client refused")


# ---------------------------------------------------------------------------
# Offline HS256 JWT (no network) — kept for tests / symmetric setups.
# ---------------------------------------------------------------------------


class JwtBearerAuth(AuthProvider):
    """Validate bearer JWTs offline against a configured JWKS (HS256 oct keys).

    Pure-stdlib HMAC verification, no PyJWT and no network. For RS256/ES256 from
    a real IdP, use :class:`OidcAuth`.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: dict[str, Any],
        revoked_jti: set[str] | None = None,
        leeway_s: int = 0,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._keys = _load_jwks(jwks)
        self._revoked_jti = revoked_jti or set()
        self._leeway_s = leeway_s

    def matches(self, request: Request) -> bool:
        token = _bearer_token(request)
        return token is not None and token.count(".") == 2

    async def authenticate(self, request: Request) -> AuthResult:
        token = _bearer_token(request)
        if token is None:
            raise Unauthorized("missing bearer token")
        payload = _verify_jwt(
            token,
            issuer=self._issuer,
            audience=self._audience,
            keys=self._keys,
            revoked_jti=self._revoked_jti,
            leeway_s=self._leeway_s,
        )
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise Unauthorized("invalid bearer token")
        jti = payload.get("jti")
        jti_str = jti if isinstance(jti, str) else None
        return AuthResult(
            subject=f"jwt:{_jwt_subject_id(sub=sub, jti=jti_str)}",
            token_id=jti_str,
        )


# ---------------------------------------------------------------------------
# OIDC / OAuth2 (discovery + JWKS + RS256/ES256 via PyJWT)
# ---------------------------------------------------------------------------


@dataclass
class _CachedJwks:
    """A fetched JWKS plus the moment it must be refreshed."""

    keys: dict[str, Any]  # kid -> JWK dict
    fetched_at: float
    raw: dict[str, Any]


class OidcAuth(AuthProvider):
    """Verify an OIDC id_token / JWT access token against an external IdP.

    Flow on each request:

    1. Pull the bearer JWT, read its unverified header to get ``kid`` + ``alg``.
    2. Resolve the signing key from a TTL-cached JWKS. On an unknown ``kid`` we
       force one refresh (handles routine key rotation) before giving up.
    3. Verify the signature (RS256/ES256/RS384/…) and the standard claims:
       ``iss`` (must be an allowed issuer), ``aud`` (must intersect allowed
       audiences), ``exp`` / ``nbf`` / ``iat`` with a configurable clock-skew
       leeway, and a non-empty ``sub``.
    4. Map the principal to a tenant via the ``tenant_claim`` (falls back to a
       static ``default_tenant``).

    The discovery document (``{issuer}/.well-known/openid-configuration``) is
    fetched once to locate ``jwks_uri``; JWKS is cached with its own TTL. No
    id_token bytes are ever logged — only the opaque salted subject id.
    """

    _ALLOWED_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

    def __init__(
        self,
        *,
        issuer: str,
        allowed_issuers: list[str] | None = None,
        audiences: list[str],
        jwks_uri: str | None = None,
        discovery_url: str | None = None,
        jwks_ttl_s: int = 3600,
        leeway_s: int = 60,
        tenant_claim: str = "tenant",
        default_tenant: str = "default",
        http_timeout_s: float = 5.0,
        # Test seam: inject a JWKS so the integration harness need not serve HTTP.
        static_jwks: dict[str, Any] | None = None,
    ) -> None:
        if _pyjwt is None:
            raise RuntimeError(
                "OidcAuth requires PyJWT — install the 'auth' extra "
                "(pip install 'concierge[auth]')"
            )
        self._issuer = issuer
        # The token's iss must be in this set; defaults to just the configured issuer.
        self._allowed_issuers = set(allowed_issuers or [issuer])
        if not audiences:
            raise ValueError("OidcAuth requires at least one allowed audience")
        self._audiences = list(audiences)
        self._discovery_url = (
            discovery_url
            or f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        self._jwks_uri = jwks_uri
        self._jwks_ttl_s = jwks_ttl_s
        self._leeway_s = leeway_s
        self._tenant_claim = tenant_claim
        self._default_tenant = default_tenant
        self._http_timeout_s = http_timeout_s
        self._cache: _CachedJwks | None = None
        self._lock = threading.Lock()
        if static_jwks is not None:
            self._cache = _CachedJwks(
                keys={k["kid"]: k for k in static_jwks.get("keys", []) if "kid" in k},
                fetched_at=time.time(),
                raw=static_jwks,
            )

    def matches(self, request: Request) -> bool:
        token = _bearer_token(request)
        return token is not None and token.count(".") == 2

    # -- JWKS fetching / caching -------------------------------------------

    async def _discover_jwks_uri(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        async with httpx.AsyncClient(timeout=self._http_timeout_s) as client:
            resp = await client.get(self._discovery_url)
            resp.raise_for_status()
            doc = resp.json()
        uri = doc.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise Unauthorized("oidc discovery document missing jwks_uri")
        self._jwks_uri = uri
        return uri

    async def _fetch_jwks(self) -> _CachedJwks:
        uri = await self._discover_jwks_uri()
        async with httpx.AsyncClient(timeout=self._http_timeout_s) as client:
            resp = await client.get(uri)
            resp.raise_for_status()
            raw = resp.json()
        keys = {k["kid"]: k for k in raw.get("keys", []) if isinstance(k, dict) and "kid" in k}
        if not keys:
            raise Unauthorized("oidc jwks contained no usable keys")
        cached = _CachedJwks(keys=keys, fetched_at=time.time(), raw=raw)
        with self._lock:
            self._cache = cached
        return cached

    def _cache_fresh(self) -> bool:
        return (
            self._cache is not None
            and (time.time() - self._cache.fetched_at) < self._jwks_ttl_s
        )

    async def _key_for(self, kid: str) -> Any:
        """Resolve a signing key for ``kid``, refreshing once on a miss/expiry."""
        if self._cache is None or not self._cache_fresh():
            await self._fetch_jwks()
        assert self._cache is not None
        jwk = self._cache.keys.get(kid)
        if jwk is None:
            # Unknown kid → the IdP likely rotated keys. Force one refresh.
            await self._fetch_jwks()
            jwk = self._cache.keys.get(kid) if self._cache else None
        if jwk is None:
            raise Unauthorized("invalid bearer token")
        return _pyjwt.PyJWK(jwk).key

    # -- verification -------------------------------------------------------

    async def authenticate(self, request: Request) -> AuthResult:
        token = _bearer_token(request)
        if token is None:
            raise Unauthorized("missing bearer token")
        try:
            header = _pyjwt.get_unverified_header(token)
        except Exception:  # noqa: BLE001 - any malformed token is a hard reject
            raise Unauthorized("invalid bearer token") from None
        alg = header.get("alg")
        kid = header.get("kid")
        if alg not in self._ALLOWED_ALGS or not isinstance(kid, str):
            raise Unauthorized("invalid bearer token")

        key = await self._key_for(kid)
        try:
            # PyJWT validates signature + exp/nbf/iat/aud; we check iss against the
            # allowed set ourselves so multiple issuers are supported.
            payload = _pyjwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=self._audiences,
                options={
                    "require": ["exp", "iss", "sub"],
                    "verify_aud": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                },
                leeway=self._leeway_s,
            )
        except _pyjwt.PyJWTError:
            # Do not echo the library's message (could include claim values).
            raise Unauthorized("invalid bearer token") from None

        iss = payload.get("iss")
        if iss not in self._allowed_issuers:
            raise Unauthorized("invalid bearer token")
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise Unauthorized("invalid bearer token")

        jti = payload.get("jti")
        jti_str = jti if isinstance(jti, str) else None
        tenant = payload.get(self._tenant_claim)
        tenant_id = tenant if isinstance(tenant, str) and tenant else self._default_tenant
        return AuthResult(
            subject=f"oidc:{_jwt_subject_id(sub=sub, jti=jti_str)}",
            tenant_id=tenant_id,
            token_id=jti_str,
        )


# ---------------------------------------------------------------------------
# mTLS pass-through (TLS terminated at the proxy; we read forwarded cert meta)
# ---------------------------------------------------------------------------


class MtlsForwardedAuth(AuthProvider):
    """Service-to-service auth via client certs validated *at the reverse proxy*.

    TLS — including client-cert validation against the configured CA — terminates
    at the ingress/reverse proxy (see ``docs/DEPLOYMENT.md``). The proxy forwards
    the *already-validated* client-cert identity in a header (nginx ingress
    ``ssl-client-subject-dn`` / ``ssl-client-verify``, or Envoy's
    ``X-Forwarded-Client-Cert``). The gateway trusts those headers **only** when
    the immediate peer IP is inside a configured trusted-proxy CIDR — otherwise a
    client could forge the headers directly. This is the load-bearing assumption;
    see the security note in ``docs/DEPLOYMENT.md`` and ``docs/AUTH.md``.

    The cert subject/SAN is mapped to a ``tenant_id`` via ``subject_tenant_map``
    (exact match), falling back to ``default_tenant``.
    """

    def __init__(
        self,
        *,
        trusted_proxy_cidrs: list[str],
        subject_header: str = "x-forwarded-client-cert",
        verify_header: str | None = "ssl-client-verify",
        subject_tenant_map: dict[str, str] | None = None,
        default_tenant: str = "default",
    ) -> None:
        # Parse CIDRs once; an empty list means "trust nobody" (deny-by-default).
        self._trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
            ipaddress.ip_network(c, strict=False) for c in trusted_proxy_cidrs
        ]
        self._subject_header = subject_header.lower()
        self._verify_header = verify_header.lower() if verify_header else None
        self._subject_tenant_map = dict(subject_tenant_map or {})
        self._default_tenant = default_tenant

    def matches(self, request: Request) -> bool:
        # Claim the request only if the forwarded-cert header is present.
        return request.headers.get(self._subject_header) is not None

    def _peer_trusted(self, request: Request) -> bool:
        peer = request.client.host if request.client else None
        if not peer:
            return False
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(addr in net for net in self._trusted)

    @staticmethod
    def _extract_subject(raw: str) -> str:
        """Pull a stable principal out of a forwarded-cert header value.

        Supports both the nginx form (a bare subject DN) and Envoy's XFCC form
        (``Subject="...";SAN=...;Hash=...``). We prefer SAN/URI when present,
        else the Subject, returning the raw value as a last resort.
        """
        if "=" in raw and (";" in raw or raw.lower().startswith("subject=")):
            parts: dict[str, str] = {}
            for seg in raw.split(";"):
                if "=" in seg:
                    k, _, v = seg.partition("=")
                    parts[k.strip().lower()] = v.strip().strip('"')
            for pref in ("uri", "san", "subject"):
                if parts.get(pref):
                    return parts[pref]
        return raw.strip()

    async def authenticate(self, request: Request) -> AuthResult:
        # 1. Trusted-proxy gate FIRST — forged headers from an untrusted peer must
        #    never be honored, even if perfectly formed.
        if not self._peer_trusted(request):
            raise Unauthorized("client certificate headers from untrusted source")

        # 2. If the proxy reports its own verification verdict, require success.
        if self._verify_header is not None:
            verdict = request.headers.get(self._verify_header)
            if verdict is not None and verdict.upper() not in ("SUCCESS", "0", "OK"):
                raise Unauthorized("client certificate not verified by proxy")

        raw_subject = request.headers.get(self._subject_header)
        if not raw_subject:
            raise Unauthorized("missing client certificate")
        principal = self._extract_subject(raw_subject)
        if not principal:
            raise Unauthorized("missing client certificate")

        tenant_id = self._subject_tenant_map.get(principal, self._default_tenant)
        cert_id = _cert_subject_id(principal)
        return AuthResult(
            subject=f"mtls:{cert_id}",
            tenant_id=tenant_id,
            token_id=f"mtls:{cert_id}",
        )


# ---------------------------------------------------------------------------
# Provider chain + central revocation enforcement
# ---------------------------------------------------------------------------


class ProviderChain(AuthProvider):
    """Run providers in order; first matching credential shape wins; deny if none.

    A request that no provider claims (``matches`` all False) is denied — there is
    no implicit fallthrough to "allow". After a provider authenticates, the result's
    ``token_id`` is checked against the revocation store; a revoked id is rejected
    no matter which provider produced it (static, tenant, OIDC, or mTLS).
    """

    def __init__(
        self,
        providers: list[AuthProvider],
        *,
        revocation: RevocationStore | None = None,
    ) -> None:
        if not providers:
            raise ValueError("ProviderChain requires at least one provider")
        self._providers = providers
        self._revocation = revocation

    async def authenticate(self, request: Request) -> AuthResult:
        last_error: Unauthorized | None = None
        attempted = False
        for provider in self._providers:
            if not provider.matches(request):
                continue
            attempted = True
            try:
                result = await provider.authenticate(request)
            except Unauthorized as e:
                last_error = e
                continue
            await self._reject_if_revoked(result)
            return result
        if last_error is not None:
            raise last_error
        if not attempted:
            raise Unauthorized("no authentication credentials presented")
        raise Unauthorized("authentication failed")

    async def _reject_if_revoked(self, result: AuthResult) -> None:
        if self._revocation is None or result.token_id is None:
            return
        if await self._revocation.is_revoked(result.token_id):
            raise Unauthorized("credential has been revoked")


class RevocationEnforcingAuth(AuthProvider):
    """Wrap a single provider so its results are checked against the revocation list.

    Equivalent to a one-element :class:`ProviderChain`, but kept distinct so a
    lone configured provider reads clearly at the call site.
    """

    def __init__(self, provider: AuthProvider, revocation: RevocationStore) -> None:
        self._provider = provider
        self._revocation = revocation

    def matches(self, request: Request) -> bool:
        return self._provider.matches(request)

    async def authenticate(self, request: Request) -> AuthResult:
        result = await self._provider.authenticate(request)
        if result.token_id is not None and await self._revocation.is_revoked(result.token_id):
            raise Unauthorized("credential has been revoked")
        return result


def build_auth_provider(cfg: AuthProviderConfig) -> AuthProvider:
    """Factory for the programmatic ``AuthProviderConfig`` (static | jwt)."""
    if cfg.provider == "static":
        return StaticBearerAuth(cfg.bearer_tokens)
    if cfg.provider == "jwt":
        if not cfg.jwt_issuer or not cfg.jwt_audience or not cfg.jwt_jwks:
            raise ValueError("jwt provider requires jwt_issuer, jwt_audience, and jwt_jwks")
        return JwtBearerAuth(
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
            jwks=cfg.jwt_jwks,
            revoked_jti=set(cfg.jwt_revoked_jti),
            leeway_s=cfg.jwt_leeway_s,
        )
    raise ValueError(f"unknown auth provider: {cfg.provider!r}")


# ---------------------------------------------------------------------------
# Offline HS256 JWKS helpers (used by JwtBearerAuth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JwkKey:
    kid: str
    alg: str
    secret: bytes | None = None


def _load_jwks(jwks: dict[str, Any]) -> dict[str, _JwkKey]:
    keys: dict[str, _JwkKey] = {}
    for entry in jwks.get("keys", []):
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        kty = entry.get("kty")
        alg = entry.get("alg", "HS256")
        if not isinstance(kid, str) or not kid:
            continue
        if kty == "oct" and alg == "HS256":
            raw_k = entry.get("k")
            if not isinstance(raw_k, str):
                continue
            secret = _b64url_decode(raw_k)
            keys[kid] = _JwkKey(kid=kid, alg=alg, secret=secret)
        elif kty == "RSA":
            raise ValueError(
                "RS256 JWKS requires the OIDC provider (OidcAuth); "
                "JwtBearerAuth is HS256-only"
            )
    if not keys:
        raise ValueError("jwks contains no supported verification keys")
    return keys


def _verify_jwt(
    token: str,
    *,
    issuer: str,
    audience: str,
    keys: dict[str, _JwkKey],
    revoked_jti: set[str],
    leeway_s: int,
) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthorized("invalid bearer token")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (json.JSONDecodeError, ValueError):
        raise Unauthorized("invalid bearer token") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise Unauthorized("invalid bearer token")

    alg = header.get("alg")
    kid = header.get("kid")
    if alg != "HS256" or not isinstance(kid, str):
        raise Unauthorized("invalid bearer token")
    key = keys.get(kid)
    if key is None or key.secret is None:
        raise Unauthorized("invalid bearer token")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(key.secret, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise Unauthorized("invalid bearer token")

    jti = payload.get("jti")
    if isinstance(jti, str) and jti in revoked_jti:
        raise Unauthorized("revoked bearer token")

    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise Unauthorized("invalid bearer token")
    if int(exp) < now - leeway_s:
        raise Unauthorized("expired bearer token")

    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and int(nbf) > now + leeway_s:
        raise Unauthorized("invalid bearer token")

    if payload.get("iss") != issuer:
        raise Unauthorized("invalid bearer token")
    aud = payload.get("aud")
    if isinstance(aud, list):
        if audience not in aud:
            raise Unauthorized("invalid bearer token")
    elif aud != audience:
        raise Unauthorized("invalid bearer token")

    return payload


__all__ = [
    "AuthProvider",
    "AuthProviderConfig",
    "AuthResult",
    "JwtBearerAuth",
    "LocalhostAllowAuth",
    "MtlsForwardedAuth",
    "NoAuth",
    "OidcAuth",
    "ProviderChain",
    "RevocationEnforcingAuth",
    "StaticBearerAuth",
    "TenantBearerAuth",
    "build_auth_provider",
]
