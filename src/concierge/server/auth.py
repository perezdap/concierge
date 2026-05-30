"""AuthProvider implementations for client→gateway access."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Request

from ..errors import Unauthorized

# Domain-separation salt for deriving an opaque audit id from a token digest.
# Bumping the version invalidates previously-emitted ids (intended).
_TOKEN_ID_SALT = b"concierge/auth/token-id/v1"
_JWT_SUBJECT_SALT = b"concierge/auth/jwt-subject/v1"


def _token_id(token_digest: bytes) -> str:
    """Stable, non-reversible id for a token, safe to put in logs/audit."""
    return hashlib.sha256(_TOKEN_ID_SALT + token_digest).hexdigest()[:12]


def _jwt_subject_id(*, sub: str, jti: str | None) -> str:
    """Opaque audit subject for a validated JWT — never raw token bytes."""
    material = sub.encode("utf-8") + b"\x00" + (jti or "").encode("utf-8")
    return hashlib.sha256(_JWT_SUBJECT_SALT + material).hexdigest()[:12]


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class AuthProviderConfig:
    """Shape for Builder 2 to mirror in ``config.AuthConfig`` and wire in ``_build_auth``."""

    provider: Literal["static", "jwt"] = "static"
    bearer_tokens: list[str] = field(default_factory=list)
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks: dict[str, Any] | None = None
    jwt_revoked_jti: set[str] = field(default_factory=set)
    jwt_leeway_s: int = 0


class AuthResult:
    def __init__(self, subject: str | None = None, tenant_id: str = "default") -> None:
        self.subject = subject
        self.tenant_id = tenant_id


class AuthProvider(ABC):
    @abstractmethod
    async def authenticate(self, request: Request) -> AuthResult: ...


class NoAuth(AuthProvider):
    async def authenticate(self, request: Request) -> AuthResult:
        return AuthResult(subject=None)


class StaticBearerAuth(AuthProvider):
    """Validate a static bearer token in constant time.

    Tokens are kept only as SHA-256 digests. The presented token is hashed and
    compared with ``hmac.compare_digest`` against every configured digest with
    no early exit, so validation time leaks neither how much of the token
    matched nor which token matched. The audit subject is an opaque, salted
    token id — raw token bytes never reach logs or audit records.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._digests = [hashlib.sha256(t.encode("utf-8")).digest() for t in tokens]

    async def authenticate(self, request: Request) -> AuthResult:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise Unauthorized("missing bearer token")
        token = header.split(" ", 1)[1].strip()
        presented = hashlib.sha256(token.encode("utf-8")).digest()
        matched = False
        for digest in self._digests:
            # Accumulate without short-circuiting to keep timing constant.
            if hmac.compare_digest(presented, digest):
                matched = True
        if not matched:
            raise Unauthorized("invalid bearer token")
        return AuthResult(subject=f"token:{_token_id(presented)}")


class JwtBearerAuth(AuthProvider):
    """Validate bearer JWTs offline against a configured JWKS (HS256 oct keys).

    RS256/PEM verification requires PyJWT (not a project dependency today); use
    HS256 oct keys in JWKS for local/integration tests until Builder 2 adds PyJWT.
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

    async def authenticate(self, request: Request) -> AuthResult:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise Unauthorized("missing bearer token")
        token = header.split(" ", 1)[1].strip()
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
        return AuthResult(subject=f"jwt:{_jwt_subject_id(sub=sub, jti=jti_str)}")


class LocalhostAllowAuth(AuthProvider):
    """Allow connections that arrive from the loopback interface only."""

    async def authenticate(self, request: Request) -> AuthResult:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return AuthResult(subject="localhost")
        raise Unauthorized("non-localhost client refused")


def build_auth_provider(cfg: AuthProviderConfig) -> AuthProvider:
    """Factory for config-selectable auth providers (static | jwt)."""
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
                "RS256 JWKS requires PyJWT; use HS256 oct keys or ask Builder 2 to add PyJWT"
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

    if payload.get("iss") != issuer:
        raise Unauthorized("invalid bearer token")
    aud = payload.get("aud")
    if isinstance(aud, list):
        if audience not in aud:
            raise Unauthorized("invalid bearer token")
    elif aud != audience:
        raise Unauthorized("invalid bearer token")

    return payload
