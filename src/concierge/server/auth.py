"""AuthProvider implementations for client→gateway access."""
from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import Request

from ..errors import Unauthorized


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
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = set(tokens)

    async def authenticate(self, request: Request) -> AuthResult:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise Unauthorized("missing bearer token")
        token = header.split(" ", 1)[1].strip()
        if token not in self.tokens:
            raise Unauthorized("invalid bearer token")
        return AuthResult(subject=f"token:{token[:6]}...")


class LocalhostAllowAuth(AuthProvider):
    """Allow connections that arrive from the loopback interface only."""

    async def authenticate(self, request: Request) -> AuthResult:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return AuthResult(subject="localhost")
        raise Unauthorized("non-localhost client refused")
