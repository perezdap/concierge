"""Runtime admin configuration models (P2-ADMIN-1)."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ConfigVersionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class ValidationIssue(BaseModel):
    """Single validation error suitable for admin UI display."""

    path: str
    message: str
    code: str | None = None


class ValidationResult(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ConfigVersion(BaseModel):
    """Immutable version record for runtime gateway configuration."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    status: ConfigVersionStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str | None = None
    config: dict[str, Any]
    validation: ValidationResult | None = None
    parent_id: str | None = None
    promoted_at: datetime | None = None

    def redacted_config(self) -> dict[str, Any]:
        from .redaction import redact_config_for_api

        return redact_config_for_api(self.config)  # type: ignore[return-value]


class ConfigVersionSummary(BaseModel):
    id: str
    status: ConfigVersionStatus
    created_at: datetime
    created_by: str | None
    validation_ok: bool | None
    parent_id: str | None = None
    promoted_at: datetime | None = None