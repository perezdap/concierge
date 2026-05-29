"""
Catalog (registry) service.

Holds every primitive the gateway knows about across every connected upstream.
The downstream client only sees *published* entries; the catalog is the full
inventory used by `gateway_discover_catalog`.

Backed by an in-memory store with an interface that lets us swap in SQLite /
Postgres / Redis later.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Optional

from .types import CatalogEntry, PrimitiveType


class CatalogStore(ABC):
    """Storage interface — write once, read many."""

    @abstractmethod
    def upsert(self, entry: CatalogEntry) -> None: ...

    @abstractmethod
    def remove(self, canonical_name: str) -> None: ...

    @abstractmethod
    def remove_by_server(self, server_id: str) -> int: ...

    @abstractmethod
    def get(self, canonical_name: str) -> Optional[CatalogEntry]: ...

    @abstractmethod
    def all(self) -> Iterable[CatalogEntry]: ...


class InMemoryCatalogStore(CatalogStore):
    def __init__(self) -> None:
        self._by_name: dict[str, CatalogEntry] = {}

    def upsert(self, entry: CatalogEntry) -> None:
        self._by_name[entry.canonical_name] = entry

    def remove(self, canonical_name: str) -> None:
        self._by_name.pop(canonical_name, None)

    def remove_by_server(self, server_id: str) -> int:
        victims = [n for n, e in self._by_name.items() if e.server_id == server_id]
        for n in victims:
            self._by_name.pop(n, None)
        return len(victims)

    def get(self, canonical_name: str) -> Optional[CatalogEntry]:
        return self._by_name.get(canonical_name)

    def all(self) -> Iterable[CatalogEntry]:
        return list(self._by_name.values())


class Catalog:
    """
    Higher-level facade on top of a CatalogStore.
    Provides the search/filter operations that the discovery primitive needs.
    """

    def __init__(self, store: CatalogStore | None = None) -> None:
        self.store = store or InMemoryCatalogStore()

    # ----- writes -----
    def upsert(self, entry: CatalogEntry) -> None:
        self.store.upsert(entry)

    def remove(self, canonical_name: str) -> None:
        self.store.remove(canonical_name)

    def replace_server(self, server_id: str, entries: list[CatalogEntry]) -> None:
        """Atomic-ish refresh of all entries for one upstream server."""
        self.store.remove_by_server(server_id)
        for e in entries:
            self.store.upsert(e)

    # ----- reads -----
    def get(self, canonical_name: str) -> Optional[CatalogEntry]:
        return self.store.get(canonical_name)

    def list(
        self,
        *,
        query: str | None = None,
        server: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        primitive_type: PrimitiveType | None = None,
        max_risk: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CatalogEntry]:
        q = (query or "").lower().strip()
        tags_set = {t.lower() for t in (tags or [])}
        risk_order = {"low": 0, "medium": 1, "high": 2, "dangerous": 3}
        risk_cap = risk_order.get(max_risk, 3) if max_risk else 3

        out: list[CatalogEntry] = []
        for e in self.store.all():
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

    def servers(self) -> list[str]:
        return sorted({e.server_id for e in self.store.all()})

    def count(self) -> int:
        return sum(1 for _ in self.store.all())
