"""Approval broker interface.

MVP behavior: any tool flagged `requires_approval` triggers ApprovalRequired.
Phase 2 swaps in a real out-of-band approval workflow (queue, notify operator,
return ticket id, etc.).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.types import CatalogEntry, Session


class ApprovalBroker(ABC):
    @abstractmethod
    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool: ...


class DenyByDefaultApprovalBroker(ApprovalBroker):
    """Always denies — safe default for MVP."""

    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool:
        return False


class AllowListApprovalBroker(ApprovalBroker):
    """Pre-approve an explicit, operator-curated set of canonical tool names.

    Interim (P0-3) broker that makes deny-by-default configurable: tools the
    operator has vetted ahead of time can be invoked, everything else is still
    denied. The real out-of-band approval queue is a later milestone (P1-3).
    """

    def __init__(self, allowed: list[str]) -> None:
        self._allowed = set(allowed)

    async def is_approved(
        self, session: Session, entry: CatalogEntry, arguments: dict[str, Any]
    ) -> bool:
        return entry.canonical_name in self._allowed
