"""
Catalog (registry) service.

Holds every primitive the gateway knows about across every connected upstream.
The downstream client only sees *published* entries; the catalog is the full
inventory used by `gateway_discover_catalog`.

Backed by an in-memory store with an interface that lets us swap in SQLite /
Postgres / Redis later.
"""
from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

from .types import CatalogEntry, PrimitiveType


class CatalogStore(ABC):
    """Storage interface — write once, read many."""

    @abstractmethod
    async def upsert(self, entry: CatalogEntry) -> None: ...

    @abstractmethod
    async def remove(self, canonical_name: str) -> None: ...

    @abstractmethod
    async def remove_by_server(self, server_id: str) -> int: ...

    @abstractmethod
    async def get(self, canonical_name: str) -> CatalogEntry | None: ...

    @abstractmethod
    async def all(self) -> list[CatalogEntry]: ...


class InMemoryCatalogStore(CatalogStore):
    def __init__(self) -> None:
        self._by_name: dict[str, CatalogEntry] = {}

    async def upsert(self, entry: CatalogEntry) -> None:
        self._by_name[entry.canonical_name] = entry

    async def remove(self, canonical_name: str) -> None:
        self._by_name.pop(canonical_name, None)

    async def remove_by_server(self, server_id: str) -> int:
        victims = [n for n, e in self._by_name.items() if e.server_id == server_id]
        for n in victims:
            self._by_name.pop(n, None)
        return len(victims)

    async def get(self, canonical_name: str) -> CatalogEntry | None:
        return self._by_name.get(canonical_name)

    async def all(self) -> list[CatalogEntry]:
        return list(self._by_name.values())


class Catalog:
    """
    Higher-level facade on top of a CatalogStore.
    Provides the search/filter operations that the discovery primitive needs.
    """

    def __init__(self, store: CatalogStore | None = None) -> None:
        self.store = store or InMemoryCatalogStore()

    # ----- writes -----
    async def upsert(self, entry: CatalogEntry) -> None:
        await self.store.upsert(entry)

    async def remove(self, canonical_name: str) -> None:
        await self.store.remove(canonical_name)

    async def replace_server(self, server_id: str, entries: list[CatalogEntry]) -> None:
        """Atomic-ish refresh of all entries for one upstream server."""
        await self.store.remove_by_server(server_id)
        for e in entries:
            await self.store.upsert(e)

    async def set_callable_for_server(self, server_id: str, value: bool) -> int:
        """Set callable flag on all known entries for a server (resilience marking).
        Keeps entries (does not remove). Returns count of entries updated.
        """
        updated = 0
        for e in list(await self.store.all()):
            if e.server_id == server_id and getattr(e, "callable", True) != value:
                # Re-upsert a copy with updated flag (pydantic immutable update pattern)
                new_e = e.model_copy(update={"callable": value})
                await self.store.upsert(new_e)
                updated += 1
        return updated

    # ----- reads -----
    async def get(self, canonical_name: str) -> CatalogEntry | None:
        return await self.store.get(canonical_name)

    async def list(
        self,
        *,
        query: str | None = None,
        server: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        primitive_type: PrimitiveType | None = None,
        max_risk: str | None = None,
        names: set[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CatalogEntry]:
        q = (query or "").lower().strip()
        tags_set = {t.lower() for t in (tags or [])}
        risk_order = {"low": 0, "medium": 1, "high": 2, "dangerous": 3}
        risk_cap = risk_order.get(max_risk, 3) if max_risk else 3

        out: list[CatalogEntry] = []
        for e in await self.store.all():
            if names is not None and e.canonical_name not in names:
                continue
            if server and e.server_id != server:
                continue
            if primitive_type and e.primitive_type != primitive_type:
                continue
            if category and category.lower() not in {c.lower() for c in e.categories}:
                continue
            if tags_set and not tags_set.issubset({t.lower() for t in e.tags}):
                continue
            if risk_order.get(e.risk_level.value, 1) > risk_cap:
                continue
            if q:
                hay = " ".join([
                    e.canonical_name,
                    e.display_label,
                    e.short_description or "",
                    e.usage_guidance or "",
                    " ".join(e.tags),
                    " ".join(e.categories),
                ]).lower()
                if q not in hay:
                    continue
            out.append(e)

        out.sort(key=lambda e: e.canonical_name)
        return out[offset : offset + limit]

    async def servers(self) -> builtins.list[str]:
        return sorted({e.server_id for e in await self.store.all()})

    async def count(self) -> int:
        return sum(1 for _ in await self.store.all())
