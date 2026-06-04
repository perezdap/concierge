import pytest

from concierge.core.catalog import Catalog
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType


def _entry(name: str, server: str = "s1", tags=None, risk=RiskLevel.LOW, ptype=PrimitiveType.TOOL):
    return CatalogEntry(
        canonical_name=f"{server}__{name}",
        upstream_name=name,
        server_id=server,
        transport=TransportType.STDIO,
        primitive_type=ptype,
        display_label=name,
        short_description=f"do {name}",
        tags=tags or [],
        risk_level=risk,
    )


@pytest.mark.asyncio
async def test_upsert_get_remove():
    c = Catalog()
    e = _entry("foo")
    await c.upsert(e)
    assert await c.get("s1__foo") is e
    await c.remove("s1__foo")
    assert await c.get("s1__foo") is None


@pytest.mark.asyncio
async def test_replace_server_atomicish():
    c = Catalog()
    await c.upsert(_entry("a"))
    await c.upsert(_entry("b"))
    await c.upsert(_entry("c", server="s2"))
    await c.replace_server("s1", [_entry("z")])
    names = sorted(e.canonical_name for e in await c.list(limit=100))
    assert names == ["s1__z", "s2__c"]


@pytest.mark.asyncio
async def test_list_filters_by_server_and_query_and_tags_and_risk():
    c = Catalog()
    await c.upsert(_entry("alpha", tags=["read"]))
    await c.upsert(_entry("beta", tags=["read", "fast"]))
    await c.upsert(_entry("gamma", tags=["write"], risk=RiskLevel.DANGEROUS))

    assert {e.canonical_name for e in await c.list(server="s1")} == {
        "s1__alpha",
        "s1__beta",
        "s1__gamma",
    }
    assert {e.canonical_name for e in await c.list(query="bet")} == {"s1__beta"}
    assert {e.canonical_name for e in await c.list(tags=["read"])} == {"s1__alpha", "s1__beta"}
    assert {e.canonical_name for e in await c.list(max_risk="medium")} == {
        "s1__alpha",
        "s1__beta",
    }


@pytest.mark.asyncio
async def test_list_pagination():
    c = Catalog()
    for i in range(10):
        await c.upsert(_entry(f"x{i:02d}"))
    page = await c.list(limit=3, offset=3)
    assert [e.upstream_name for e in page] == ["x03", "x04", "x05"]
