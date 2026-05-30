"""Unit tests for persistent CatalogStore implementations (P1-2)."""
from __future__ import annotations

import os
import tempfile

import pytest

from concierge.core.catalog_store import SqliteCatalogStore
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType


def _entry(name: str, server: str = "s1", ptype=PrimitiveType.TOOL, callable_: bool = True):
    return CatalogEntry(
        canonical_name=f"{server}__{name}",
        upstream_name=name,
        server_id=server,
        transport=TransportType.STDIO,
        primitive_type=ptype,
        display_label=name,
        short_description=f"do {name}",
        risk_level=RiskLevel.LOW,
        callable=callable_,
    )


@pytest.mark.asyncio
async def test_sqlite_upsert_get_remove():
    store = SqliteCatalogStore(":memory:")
    e = _entry("foo")
    await store.upsert(e)
    got = await store.get("s1__foo")
    assert got is not None
    assert got.canonical_name == "s1__foo"
    assert got.callable is True

    await store.remove("s1__foo")
    assert await store.get("s1__foo") is None


@pytest.mark.asyncio
async def test_sqlite_remove_by_server():
    store = SqliteCatalogStore(":memory:")
    await store.upsert(_entry("a", server="s1"))
    await store.upsert(_entry("b", server="s1"))
    await store.upsert(_entry("c", server="s2"))
    n = await store.remove_by_server("s1")
    assert n == 2
    assert await store.get("s1__a") is None
    assert await store.get("s2__c") is not None


@pytest.mark.asyncio
async def test_sqlite_all_returns_list():
    store = SqliteCatalogStore(":memory:")
    await store.upsert(_entry("a"))
    await store.upsert(_entry("b"))
    all_entries = await store.all()
    assert len(all_entries) == 2
    assert sorted(e.canonical_name for e in all_entries) == ["s1__a", "s1__b"]


@pytest.mark.asyncio
async def test_sqlite_callable_roundtrip():
    store = SqliteCatalogStore(":memory:")
    await store.upsert(_entry("x", callable_=False))
    got = await store.get("s1__x")
    assert got is not None
    assert got.callable is False


@pytest.mark.asyncio
async def test_sqlite_persistence_across_instances():
    """Two SqliteCatalogStore instances on the same file see each other's data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        store1 = SqliteCatalogStore(path)
        await store1.upsert(_entry("persisted"))
        store1.close()

        store2 = SqliteCatalogStore(path)
        got = await store2.get("s1__persisted")
        assert got is not None
        assert got.canonical_name == "s1__persisted"
        store2.close()
    finally:
        os.unlink(path)
