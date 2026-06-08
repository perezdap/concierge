"""
Publishing service.

This is the heart of "catalog vs. publish" separation:

  cataloged  — known to the gateway (lives in the Catalog).
  published  — visible in this session's tools/list / resources/list / prompts/list.
  callable   — additionally allowed by policy + upstream is healthy.
  blocked    — explicitly denied (policy / approval pending / upstream down).

The PublishingService never decides policy itself — it just records which
canonical_names are exposed per session and emits list_changed notifications.
"""
from __future__ import annotations

from collections.abc import Iterable

from ..errors import NotPublished, UnknownPrimitive
from .catalog import Catalog
from .notifications import NotificationBus
from .session import SessionManager
from .types import (
    CatalogEntry,
    PrimitiveType,
    PublishedPrimitive,
    Session,
)


class PublishingService:
    def __init__(
        self, catalog: Catalog, bus: NotificationBus, sessions: SessionManager | None = None
    ) -> None:
        self.catalog = catalog
        self.bus = bus
        self.sessions = sessions

    # ------------------------------------------------------------------
    # mutate
    # ------------------------------------------------------------------
    async def enable(
        self,
        session: Session,
        canonical_names: Iterable[str],
        *,
        by: str = "client",
    ) -> tuple[list[str], list[str]]:
        """Enable a batch of catalog entries on this session.

        Returns (enabled, skipped) — skipped includes unknown names.
        Emits a single list_changed per primitive type touched.
        """
        enabled: list[str] = []
        skipped: list[str] = []
        touched: set[PrimitiveType] = set()
        for name in canonical_names:
            entry = await self.catalog.get(name)
            if entry is None:
                skipped.append(name)
                continue
            bucket = session.published_set(entry.primitive_type)
            if name in bucket:
                continue  # idempotent
            bucket[name] = PublishedPrimitive(
                canonical_name=name,
                primitive_type=entry.primitive_type,
                enabled_by=by,
            )
            enabled.append(name)
            touched.add(entry.primitive_type)

        if enabled:
            await self._persist(session)
        self._emit_changed(session.session_id, touched)
        return enabled, skipped

    async def disable(
        self,
        session: Session,
        canonical_names: Iterable[str],
    ) -> list[str]:
        removed: list[str] = []
        touched: set[PrimitiveType] = set()
        for name in canonical_names:
            entry = await self.catalog.get(name)
            ptype: PrimitiveType | None = entry.primitive_type if entry else None
            # Try all buckets in case catalog was purged.
            for cand in [ptype] if ptype else list(PrimitiveType):
                bucket = session.published_set(cand)
                if name in bucket:
                    bucket.pop(name, None)
                    removed.append(name)
                    touched.add(cand)
                    break
        if removed:
            await self._persist(session)
        self._emit_changed(session.session_id, touched)
        return removed

    async def disable_all(self, session: Session) -> int:
        n = 0
        for ptype in PrimitiveType:
            bucket = session.published_set(ptype)
            n += len(bucket)
            bucket.clear()
        if n:
            await self._persist(session)
        self._emit_changed(session.session_id, set(PrimitiveType))
        return n

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------
    async def is_published(
        self, session: Session, canonical_name: str, ptype: PrimitiveType
    ) -> bool:
        return canonical_name in session.published_set(ptype)

    async def list_published(
        self, session: Session, ptype: PrimitiveType
    ) -> list[CatalogEntry]:
        bucket = session.published_set(ptype)
        out: list[CatalogEntry] = []
        for name in bucket:
            e = await self.catalog.get(name)
            if e is not None:
                out.append(e)
        out.sort(key=lambda x: x.canonical_name)
        return out

    async def require_published(
        self, session: Session, canonical_name: str, ptype: PrimitiveType
    ) -> CatalogEntry:
        if not await self.is_published(session, canonical_name, ptype):
            # Distinguish "unknown" from "not published" for clearer errors.
            if await self.catalog.get(canonical_name) is None:
                raise UnknownPrimitive(f"no such {ptype.value}: {canonical_name}")
            raise NotPublished(f"{ptype.value} {canonical_name!r} is not enabled in this session")
        entry = await self.catalog.get(canonical_name)
        if entry is None:
            # Race: cataloged then evicted. Treat as not-published.
            raise NotPublished(canonical_name)
        return entry

    # ------------------------------------------------------------------
    async def _persist(self, session: Session) -> None:
        """Delegate to SessionManager.save() so backends like Redis stay in sync."""
        if self.sessions is not None:
            await self.sessions.save(session)

    def _emit_changed(self, session_id: str, touched: set[PrimitiveType]) -> None:
        if PrimitiveType.TOOL in touched:
            self.bus.tools_list_changed(session_id)
        if PrimitiveType.RESOURCE in touched:
            self.bus.resources_list_changed(session_id)
        if PrimitiveType.PROMPT in touched:
            self.bus.prompts_list_changed(session_id)
