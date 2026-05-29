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
