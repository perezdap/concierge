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
    await catalog.upsert(_entry("foo"))
    bus = NotificationBus(coalesce_window_s=0)
    publishing = PublishingService(catalog, bus)
    sessions = SessionManager()
    s = await sessions.create()

    enabled, skipped = await publishing.enable(s, ["s1.foo", "s1.does_not_exist"])
    assert enabled == ["s1.foo"]
    assert skipped == ["s1.does_not_exist"]

    msg = await bus.queue_for(s.session_id).get()
    assert msg["method"] == "notifications/tools/list_changed"

    assert await publishing.is_published(s, "s1.foo", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_disable_removes_only_published():
    catalog = Catalog()
    await catalog.upsert(_entry("foo"))
    await catalog.upsert(_entry("bar"))
    bus = NotificationBus(coalesce_window_s=0)
    publishing = PublishingService(catalog, bus)
    sessions = SessionManager()
    s = await sessions.create()

    await publishing.enable(s, ["s1.foo", "s1.bar"])
    # drain notification
    await asyncio.wait_for(bus.queue_for(s.session_id).get(), timeout=1)

    removed = await publishing.disable(s, ["s1.foo"])
    assert removed == ["s1.foo"]
    assert not await publishing.is_published(s, "s1.foo", PrimitiveType.TOOL)
    assert await publishing.is_published(s, "s1.bar", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_require_published_distinguishes_unknown_vs_not_published():
    catalog = Catalog()
    await catalog.upsert(_entry("foo"))
    publishing = PublishingService(catalog, NotificationBus(coalesce_window_s=0))
    sessions = SessionManager()
    s = await sessions.create()

    with pytest.raises(NotPublished):
        await publishing.require_published(s, "s1.foo", PrimitiveType.TOOL)
    with pytest.raises(UnknownPrimitive):
        await publishing.require_published(s, "s1.nope", PrimitiveType.TOOL)


@pytest.mark.asyncio
async def test_disable_all_clears_every_bucket():
    catalog = Catalog()
    await catalog.upsert(_entry("t", PrimitiveType.TOOL))
    await catalog.upsert(_entry("r", PrimitiveType.RESOURCE))
    await catalog.upsert(_entry("p", PrimitiveType.PROMPT))
    publishing = PublishingService(catalog, NotificationBus(coalesce_window_s=0))
    sessions = SessionManager()
    s = await sessions.create()
    await publishing.enable(s, ["s1.t", "s1.r", "s1.p"])
    n = await publishing.disable_all(s)
    assert n == 3
    assert not s.published_tools and not s.published_resources and not s.published_prompts
