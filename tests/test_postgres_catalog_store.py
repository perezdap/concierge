"""Integration tests for PostgresCatalogStore (P1-2).

Skipped automatically when asyncpg is missing or no Postgres is reachable.
Set CONCIERGE_TEST_PG_DSN to point at a live database, e.g.:
    postgresql://postgres:concierge@localhost:5432/concierge
"""
from __future__ import annotations

import asyncio
import os

import pytest

from concierge.core.types import (
    CatalogEntry,
    PrimitiveType,
    RiskLevel,
    TransportType,
)

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get(
    "CONCIERGE_TEST_PG_DSN",
    "postgresql://postgres:concierge@localhost:5432/concierge",
)


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    async def _probe():
        try:
            conn = await asyncpg.connect(_DSN)
        except Exception:
            pytest.skip("postgres server not reachable")
        else:
            await conn.close()

    asyncio.run(_probe())
    return _DSN


def _entry(name: str, server: str = "pg1", callable_: bool = True) -> CatalogEntry:
    return CatalogEntry(
        canonical_name=f"{server}__{name}",
        upstream_name=name,
        server_id=server,
        transport=TransportType.STDIO,
        primitive_type=PrimitiveType.TOOL,
        display_label=name,
        short_description=f"do {name}",
        risk_level=RiskLevel.LOW,
        callable=callable_,
    )


async def _fresh_store(dsn):
    from concierge.core.catalog_store import PostgresCatalogStore

    store = PostgresCatalogStore(dsn)
    await store.remove_by_server("pg1")
    await store.remove_by_server("pg2")
    return store


async def test_pg_upsert_get_remove(pg_dsn: str):
    store = await _fresh_store(pg_dsn)
    await store.upsert(_entry("foo"))
    got = await store.get("pg1__foo")
    assert got is not None
    assert got.canonical_name == "pg1__foo"
    await store.remove("pg1__foo")
    assert await store.get("pg1__foo") is None
    await store.close()


async def test_pg_callable_roundtrip(pg_dsn: str):
    store = await _fresh_store(pg_dsn)
    await store.upsert(_entry("x", callable_=False))
    got = await store.get("pg1__x")
    assert got is not None
    assert got.callable is False
    await store.remove_by_server("pg1")
    await store.close()


async def test_pg_remove_by_server(pg_dsn: str):
    store = await _fresh_store(pg_dsn)
    await store.upsert(_entry("a", server="pg1"))
    await store.upsert(_entry("b", server="pg1"))
    await store.upsert(_entry("c", server="pg2"))
    n = await store.remove_by_server("pg1")
    assert n == 2
    assert await store.get("pg1__a") is None
    assert await store.get("pg2__c") is not None
    await store.remove_by_server("pg2")
    await store.close()


async def test_pg_shared_across_instances(pg_dsn: str):
    """Two independent pools (= two app replicas) share catalog state."""
    a = await _fresh_store(pg_dsn)
    await a.upsert(_entry("shared"))
    await a.close()  # replica A's pool fully closed

    from concierge.core.catalog_store import PostgresCatalogStore

    b = PostgresCatalogStore(pg_dsn)
    got = await b.get("pg1__shared")
    assert got is not None
    assert got.canonical_name == "pg1__shared"
    await b.remove_by_server("pg1")
    await b.close()
