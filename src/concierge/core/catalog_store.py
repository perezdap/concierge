"""Persistent CatalogStore implementations with lightweight migration runner."""
from __future__ import annotations

import asyncio
import sqlite3

from .catalog import CatalogStore
from .types import CatalogEntry

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_entries (
    canonical_name TEXT PRIMARY KEY,
    server_id      TEXT NOT NULL,
    primitive_type TEXT NOT NULL,
    transport      TEXT NOT NULL,
    data           TEXT NOT NULL,
    callable       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_catalog_server ON catalog_entries(server_id);
CREATE INDEX IF NOT EXISTS idx_catalog_type  ON catalog_entries(primitive_type);
"""


class SqliteCatalogStore(CatalogStore):
    """CatalogStore backed by SQLite (local file or :memory:).

    Uses WAL mode for better concurrency. Safe for single-node deployments
    or shared filesystem mounts (NFS/SMB) — not recommended for high
    concurrency but fully functional for light multi-node sharing.

    Sync sqlite3 calls are wrapped with ``asyncio.to_thread()`` so they do
    not block the event loop.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.commit()

    async def upsert(self, entry: CatalogEntry) -> None:
        data = entry.model_dump_json()

        def _run() -> None:
            self._conn.execute(
                "INSERT OR REPLACE INTO catalog_entries"
                " (canonical_name, server_id, primitive_type, transport, data, callable)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.canonical_name,
                    entry.server_id,
                    entry.primitive_type.value,
                    entry.transport.value,
                    data,
                    int(entry.callable),
                ),
            )
            self._conn.commit()

        await asyncio.to_thread(_run)

    async def remove(self, canonical_name: str) -> None:
        def _run() -> None:
            self._conn.execute(
                "DELETE FROM catalog_entries WHERE canonical_name = ?", (canonical_name,)
            )
            self._conn.commit()

        await asyncio.to_thread(_run)

    async def remove_by_server(self, server_id: str) -> int:
        def _run() -> int:
            cur = self._conn.execute(
                "DELETE FROM catalog_entries WHERE server_id = ?", (server_id,)
            )
            self._conn.commit()
            return cur.rowcount

        return await asyncio.to_thread(_run)

    async def get(self, canonical_name: str) -> CatalogEntry | None:
        def _run() -> str | None:
            row = self._conn.execute(
                "SELECT data FROM catalog_entries WHERE canonical_name = ?", (canonical_name,)
            ).fetchone()
            return row[0] if row else None

        data = await asyncio.to_thread(_run)
        if data is None:
            return None
        return CatalogEntry.model_validate_json(data)

    async def all(self) -> list[CatalogEntry]:
        def _run() -> list[str]:
            rows = self._conn.execute("SELECT data FROM catalog_entries").fetchall()
            return [r[0] for r in rows]

        data_list = await asyncio.to_thread(_run)
        return [CatalogEntry.model_validate_json(d) for d in data_list]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_entries (
    canonical_name TEXT PRIMARY KEY,
    server_id      TEXT NOT NULL,
    primitive_type TEXT NOT NULL,
    transport      TEXT NOT NULL,
    data           JSONB NOT NULL,
    callable       BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_catalog_server ON catalog_entries(server_id);
CREATE INDEX IF NOT EXISTS idx_catalog_type  ON catalog_entries(primitive_type);
"""


class PostgresCatalogStore(CatalogStore):
    """CatalogStore backed by PostgreSQL (asyncpg).

    The connection pool is created lazily on the first operation and should be
    closed explicitly via ``close()`` on application shutdown.
    """

    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgresCatalogStore")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=10
            )
            async with self._pool.acquire() as conn:
                await conn.execute(_POSTGRES_SCHEMA)
        return self._pool

    async def upsert(self, entry: CatalogEntry) -> None:
        pool = await self._ensure_pool()
        data = entry.model_dump_json()
        await pool.execute(
            """
            INSERT INTO catalog_entries
                (canonical_name, server_id, primitive_type, transport, data, callable)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (canonical_name) DO UPDATE SET
                server_id      = EXCLUDED.server_id,
                primitive_type = EXCLUDED.primitive_type,
                transport      = EXCLUDED.transport,
                data           = EXCLUDED.data,
                callable       = EXCLUDED.callable
            """,
            entry.canonical_name,
            entry.server_id,
            entry.primitive_type.value,
            entry.transport.value,
            data,
            entry.callable,
        )

    async def remove(self, canonical_name: str) -> None:
        pool = await self._ensure_pool()
        await pool.execute(
            "DELETE FROM catalog_entries WHERE canonical_name = $1", canonical_name
        )

    async def remove_by_server(self, server_id: str) -> int:
        pool = await self._ensure_pool()
        result = await pool.execute(
            "DELETE FROM catalog_entries WHERE server_id = $1", server_id
        )
        # asyncpg execute returns a status string like "DELETE 3"
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return 0

    async def get(self, canonical_name: str) -> CatalogEntry | None:
        pool = await self._ensure_pool()
        row = await pool.fetchrow(
            "SELECT data FROM catalog_entries WHERE canonical_name = $1",
            canonical_name,
        )
        if row is None:
            return None
        return CatalogEntry.model_validate_json(row["data"])

    async def all(self) -> list[CatalogEntry]:
        pool = await self._ensure_pool()
        rows = await pool.fetch("SELECT data FROM catalog_entries")
        return [CatalogEntry.model_validate_json(r["data"]) for r in rows]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
