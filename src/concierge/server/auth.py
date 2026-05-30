"""AuthProvider implementations for client→gateway access."""
from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod

from fastapi import Request

from ..errors import Unauthorized

# Domain-separation salt for deriving an opaque audit id from a token digest.
# Bumping the version invalidates previously-emitted ids (intended).
_TOKEN_ID_SALT = b"concierge/auth/token-id/v1"


def _token_id(token_digest: bytes) -> str:
    """Stable, non-reversible id for a token, safe to put in logs/audit."""
    return hashlib.sha256(_TOKEN_ID_SALT + token_digest).hexdigest()[:12]


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


class LocalhostAllowAuth(AuthProvider):
    """Allow connections that arrive from the loopback interface only."""

    async def authenticate(self, request: Request) -> AuthResult:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return AuthResult(subject="localhost")
        raise Unauthorized("non-localhost client refused")
