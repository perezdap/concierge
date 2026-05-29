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


def test_upsert_get_remove():
    c = Catalog()
    e = _entry("foo")
    c.upsert(e)
    assert c.get("s1__foo") is e
    c.remove("s1__foo")
    assert c.get("s1__foo") is None


def test_replace_server_atomicish():
    c = Catalog()
    c.upsert(_entry("a"))
    c.upsert(_entry("b"))
    c.upsert(_entry("c", server="s2"))
    c.replace_server("s1", [_entry("z")])
    names = sorted(e.canonical_name for e in c.list(limit=100))
    assert names == ["s1__z", "s2__c"]


def test_list_filters_by_server_and_query_and_tags_and_risk():
    c = Catalog()
    c.upsert(_entry("alpha", tags=["read"]))
    c.upsert(_entry("beta", tags=["read", "fast"]))
    c.upsert(_entry("gamma", tags=["write"], risk=RiskLevel.DANGEROUS))

    assert {e.canonical_name for e in c.list(server="s1")} == {"s1__alpha", "s1__beta", "s1__gamma"}
    assert {e.canonical_name for e in c.list(query="bet")} == {"s1__beta"}
    assert {e.canonical_name for e in c.list(tags=["read"])} == {"s1__alpha", "s1__beta"}
    assert {e.canonical_name for e in c.list(max_risk="medium")} == {"s1__alpha", "s1__beta"}


def test_list_pagination():
    c = Catalog()
    for i in range(10):
        c.upsert(_entry(f"x{i:02d}"))
    page = c.list(limit=3, offset=3)
    assert [e.upstream_name for e in page] == ["x03", "x04", "x05"]
