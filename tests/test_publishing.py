import asyncio

import pytest

from concierge.core.catalog import Catalog
from concierge.core.notifications import NotificationBus
from concierge.core.publishing import PublishingService
from concierge.core.session import SessionManager
from concierge.core.types import CatalogEntry, PrimitiveType, RiskLevel, TransportType
from concierge.errors import NotPublished, UnknownPrimitive


def _entry(name: str, ptype=PrimitiveType.TOOL):
    return CatalogEntry(
        canonical_name=f"s1.{name}",
        upstream_name=name,
        server_id="s1",
        transport=TransportType.STDIO,
        primitive_type=ptype,
        display_label=name,
        short_description="...",
        risk_level=RiskLevel.LOW,
    )


@pytest.mark.asyncio
async def test_enable_publishes_and_emits_notification():
    catalog = Catalog()
    catalog.upsert(_entry("foo"))
    bus = NotificationBus(coalesce_window_s=0)
    publishing = PublishingService(catalog, bus)
    sessions = SessionManager()
    s = await sessions.create()

    enabled, skipped = publishing.enable(s, ["s1.foo", "s1.does_not_exist"])
    assert enabled == ["s1.foo"]
    assert skipped == ["s1.does_not_exist"]

    msg = await bus.queue_for(s.session_id).get()
    assert msg["method"] == "notifications/tools/list_changed"

    assert publishing.is_published(s, "s1.foo", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_disable_removes_only_published():
    catalog = Catalog()
    catalog.upsert(_entry("foo"))
    catalog.upsert(_entry("bar"))
    bus = NotificationBus(coalesce_window_s=0)
    publishing = PublishingService(catalog, bus)
    sessions = SessionManager()
    s = await sessions.create()

    publishing.enable(s, ["s1.foo", "s1.bar"])
    # drain notification
    await asyncio.wait_for(bus.queue_for(s.session_id).get(), timeout=1)

    removed = publishing.disable(s, ["s1.foo"])
    assert removed == ["s1.foo"]
    assert not publishing.is_published(s, "s1.foo", PrimitiveType.TOOL)
    assert publishing.is_published(s, "s1.bar", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_require_published_distinguishes_unknown_vs_not_published():
    catalog = Catalog()
    catalog.upsert(_entry("foo"))
    publishing = PublishingService(catalog, NotificationBus(coalesce_window_s=0))
    sessions = SessionManager()
    s = await sessions.create()

    with pytest.raises(NotPublished):
        publishing.require_published(s, "s1.foo", PrimitiveType.TOOL)
    with pytest.raises(UnknownPrimitive):
        publishing.require_published(s, "s1.nope", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_disable_all_clears_every_bucket():
    catalog = Catalog()
    catalog.upsert(_entry("t", PrimitiveType.TOOL))
    catalog.upsert(_entry("r", PrimitiveType.RESOURCE))
    catalog.upsert(_entry("p", PrimitiveType.PROMPT))
    publishing = PublishingService(catalog, NotificationBus(coalesce_window_s=0))
    sessions = SessionManager()
    s = await sessions.create()
    publishing.enable(s, ["s1.t", "s1.r", "s1.p"])
    n = publishing.disable_all(s)
    assert n == 3
    assert not s.published_tools and not s.published_resources and not s.published_prompts
