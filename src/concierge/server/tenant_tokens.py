"""Per-tenant token issuance + rotation (P1-1).

For service principals and tenants that do not federate through an external IdP,
the gateway can mint its own opaque bearer tokens, each bound to a ``tenant_id``.
This is the simpler half of "real auth": no OAuth dance, no JWKS — just a strong
random secret the operator hands to a tenant, mapped server-side to a tenant id.

Design (mirrors the P0-2 static-bearer secret hygiene):

* The raw token is generated with ``secrets.token_urlsafe`` and returned **once**,
  at mint time. It is *never* persisted — only its SHA-256 digest is stored, so a
  dump of the store reveals nothing usable.
* Every record carries a stable, salted ``token_id`` (the static analogue of a
  JWT ``jti``) used for audit and for the revocation list.
* Lookup at auth time is by digest: the presented token is hashed and matched in
  constant time against the stored digest. Tenant + token_id come back on a hit.
* Rotation = mint a new token for the tenant and revoke the old token_id.

The store follows the same backend matrix as the rest of P1-2/P1-4: in-memory
(tests/dev), Redis, and Postgres, all behind one async ABC.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

# Domain-separation salt for deriving a token_id from a token digest. Distinct
# from the auth-module salt so the two id spaces never collide.
_TENANT_TOKEN_ID_SALT = b"concierge/auth/tenant-token-id/v1"


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _token_id_for(digest: bytes) -> str:
    """Stable, non-reversible id for a tenant token — safe for logs/revocation."""
    return "tt_" + hashlib.sha256(_TENANT_TOKEN_ID_SALT + digest).hexdigest()[:16]


@dataclass(frozen=True)
class TenantTokenRecord:
    """A minted tenant token, minus the secret (digest only)."""

    token_id: str
    tenant_id: str
    digest: bytes


@dataclass(frozen=True)
class MintedToken:
    """Returned once at mint time — the only moment the raw secret is exposed."""

    token: str
    token_id: str
    tenant_id: str


@dataclass(frozen=True)
class TenantTokenLookup:
    """Result of resolving a presented token to its tenant + id."""

    token_id: str
    tenant_id: str


class TenantTokenStore(ABC):
    """Backend-agnostic per-tenant token store."""

    async def mint(self, tenant_id: str) -> MintedToken:
        """Generate a fresh token for ``tenant_id`` and persist its digest."""
        token = secrets.token_urlsafe(32)
        digest = _digest(token)
        token_id = _token_id_for(digest)
        await self._put(TenantTokenRecord(token_id=token_id, tenant_id=tenant_id, digest=digest))
        return MintedToken(token=token, token_id=token_id, tenant_id=tenant_id)

    async def resolve(self, token: str) -> TenantTokenLookup | None:
        """Constant-time match of a presented token to its tenant + token_id.

        Every stored digest is compared (no early exit) so match time leaks
        neither how many tokens exist nor which one matched.
        """
        presented = _digest(token)
        matched: TenantTokenRecord | None = None
        for rec in await self._all():
            if hmac.compare_digest(presented, rec.digest):
                matched = rec
        if matched is None:
            return None
        return TenantTokenLookup(token_id=matched.token_id, tenant_id=matched.tenant_id)

    async def rotate(self, tenant_id: str, old_token_id: str) -> MintedToken:
        """Mint a new token for ``tenant_id`` and drop the old record.

        The caller is responsible for also adding ``old_token_id`` to the
        revocation list if the old secret may still be cached anywhere.
        """
        minted = await self.mint(tenant_id)
        await self._remove(old_token_id)
        return minted

    @abstractmethod
    async def _put(self, record: TenantTokenRecord) -> None: ...

    @abstractmethod
    async def _all(self) -> list[TenantTokenRecord]: ...

    @abstractmethod
    async def _remove(self, token_id: str) -> None: ...

    async def list_records(self) -> list[TenantTokenRecord]:
        """Inspection: minted tokens (digests, never raw secrets)."""
        return await self._all()

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryTenantTokenStore(TenantTokenStore):
    """Process-local tenant token store. Tests/dev only."""

    def __init__(self) -> None:
        self._records: dict[str, TenantTokenRecord] = {}
        self._lock = asyncio.Lock()

    async def _put(self, record: TenantTokenRecord) -> None:
        async with self._lock:
            self._records[record.token_id] = record

    async def _all(self) -> list[TenantTokenRecord]:
        async with self._lock:
            return list(self._records.values())

    async def _remove(self, token_id: str) -> None:
        async with self._lock:
            self._records.pop(token_id, None)


class RedisTenantTokenStore(TenantTokenStore):
    """Redis-backed tenant token store. Stores ``token_id -> {tenant, digest_hex}``."""

    KEY_SCHEMA_VERSION = "v1"

    def __init__(self, redis_url: str, *, key_prefix: str = "tenant_token") -> None:
        if aioredis is None:
            raise RuntimeError("redis[asyncio] is required for RedisTenantTokenStore")
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._prefix = f"{key_prefix}:{self.KEY_SCHEMA_VERSION}:"

    def _key(self, token_id: str) -> str:
        return f"{self._prefix}{token_id}"

    async def _put(self, record: TenantTokenRecord) -> None:
        await self._redis.hset(  # type: ignore[misc]
            self._key(record.token_id),
            mapping={"tenant": record.tenant_id, "digest": record.digest.hex()},
        )

    async def _all(self) -> list[TenantTokenRecord]:
        out: list[TenantTokenRecord] = []
        async for key in self._redis.scan_iter(match=f"{self._prefix}*"):
            data = await self._redis.hgetall(key)  # type: ignore[misc]
            if not data:
                continue
            token_id = str(key)[len(self._prefix):]
            out.append(
                TenantTokenRecord(
                    token_id=token_id,
                    tenant_id=str(data.get("tenant", "default")),
                    digest=bytes.fromhex(str(data["digest"])),
                )
            )
        return out

    async def _remove(self, token_id: str) -> None:
        await self._redis.delete(self._key(token_id))

    async def aclose(self) -> None:
        await self._redis.aclose()


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_tokens (
    token_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    digest_hex  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenant_tokens_tenant ON tenant_tokens(tenant_id);
"""


class PostgresTenantTokenStore(TenantTokenStore):
    """Durable tenant token store in Postgres (asyncpg)."""

    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgresTenantTokenStore")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(_PG_SCHEMA)
        return self._pool

    async def _put(self, record: TenantTokenRecord) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            """
            INSERT INTO tenant_tokens (token_id, tenant_id, digest_hex)
            VALUES ($1, $2, $3)
            ON CONFLICT (token_id) DO UPDATE SET
                tenant_id  = EXCLUDED.tenant_id,
                digest_hex = EXCLUDED.digest_hex
            """,
            record.token_id,
            record.tenant_id,
            record.digest.hex(),
        )

    async def _all(self) -> list[TenantTokenRecord]:
        pool = await self._ensure_pool()
        rows = await pool.fetch("SELECT token_id, tenant_id, digest_hex FROM tenant_tokens")
        return [
            TenantTokenRecord(
                token_id=r["token_id"],
                tenant_id=r["tenant_id"],
                digest=bytes.fromhex(r["digest_hex"]),
            )
            for r in rows
        ]

    async def _remove(self, token_id: str) -> None:
        pool = await self._ensure_pool()
        await pool.execute("DELETE FROM tenant_tokens WHERE token_id = $1", token_id)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


__all__ = [
    "InMemoryTenantTokenStore",
    "MintedToken",
    "PostgresTenantTokenStore",
    "RedisTenantTokenStore",
    "TenantTokenLookup",
    "TenantTokenRecord",
    "TenantTokenStore",
]
