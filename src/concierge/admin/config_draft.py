"""Draft config lifecycle, validation, and apply-live orchestration."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from ..adapters.factory import build_adapter
from ..config import GatewayConfig, ProfileConfig, UpstreamServerConfig
from .config_store import ConfigStore
from .models import ConfigVersion, ValidationIssue
from .models import ValidationResult as StoreValidationResult
from .reload import ReloadCoordinator, ReloadError

T = TypeVar("T", bound=BaseModel)


class ApplyDraftError(Exception):
    """Draft apply failure with a stable code for API mapping."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ApplyDraftLiveResult:
    runtime_applied: bool
    reload_coordinator_configured: bool


def validation_error_detail(validation: StoreValidationResult) -> dict[str, Any]:
    return {
        "code": "validation_failed",
        "issues": [i.model_dump() for i in validation.issues],
    }


def pydantic_validation_issues(exc: ValidationError) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            path=".".join(str(p) for p in err["loc"]),
            message=err["msg"],
            code=err.get("type"),
        )
        for err in exc.errors()
    ]


def validate_pydantic_dict(model: type[T], data: dict[str, Any]) -> StoreValidationResult:
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return StoreValidationResult(ok=False, issues=pydantic_validation_issues(exc))
    return StoreValidationResult(ok=True)


def validate_profile_dict(data: dict[str, Any]) -> StoreValidationResult:
    return validate_pydantic_dict(ProfileConfig, data)


def validate_upstream_dict(data: dict[str, Any]) -> StoreValidationResult:
    result = validate_pydantic_dict(UpstreamServerConfig, data)
    if not result.ok:
        return result
    try:
        build_adapter(UpstreamServerConfig.model_validate(data))
    except ValueError as e:
        return StoreValidationResult(
            ok=False,
            issues=[ValidationIssue(path="", message=str(e), code="transport")],
        )
    return StoreValidationResult(ok=True)


async def config_for_reads(store: ConfigStore) -> dict[str, Any]:
    draft = await store.get_draft()
    if draft is not None:
        return draft.config
    active = await store.get_active_version()
    if active is not None:
        return active.config
    return GatewayConfig().model_dump(mode="json")


async def ensure_draft(
    store: ConfigStore,
    *,
    created_by: str | None,
) -> dict[str, Any]:
    draft = await store.get_draft()
    if draft is not None:
        return draft.config
    active = await store.get_active_version()
    if active is not None:
        base = deepcopy(active.config)
    else:
        base = GatewayConfig().model_dump(mode="json")
    await store.create_or_update_draft(base, created_by=created_by)
    refreshed = await store.get_draft()
    if refreshed is None:
        raise HTTPException(status_code=500, detail="failed to create config draft")
    return refreshed.config


async def apply_draft_live(
    store: ConfigStore,
    *,
    reload_coordinator: ReloadCoordinator | None,
    subject: str | None,
) -> tuple[ConfigVersion, ApplyDraftLiveResult]:
    """Validate runtime, apply live, then promote the current draft to active."""
    draft = await store.get_draft()
    if draft is None:
        raise ApplyDraftError("no_draft", "no draft config to apply")

    runtime_applied = False
    if reload_coordinator is not None:
        cfg = GatewayConfig.model_validate(draft.config)
        try:
            await reload_coordinator.validate_apply_reload(
                cfg,
                version_id=draft.id,
                subject=subject or "",
            )
            runtime_applied = True
        except ReloadError as e:
            raise ApplyDraftError("apply_reload_failed", str(e)) from e

    try:
        active = await store.promote_draft_to_active(created_by=subject)
    except ValueError as e:
        raise ApplyDraftError("promote_failed", str(e)) from e

    return active, ApplyDraftLiveResult(
        runtime_applied=runtime_applied,
        reload_coordinator_configured=reload_coordinator is not None,
    )


async def reload_active_live(
    store: ConfigStore,
    reload_coordinator: ReloadCoordinator,
    *,
    subject: str | None,
) -> str:
    """Validate and apply the active config against the live runtime."""
    active = await store.get_active_version()
    if active is None:
        raise ApplyDraftError("no_active", "no active config to reload", status_code=404)

    cfg = GatewayConfig.model_validate(active.config)
    result = await reload_coordinator.validate_for_apply(
        cfg,
        version_id=active.id,
        subject=subject or "",
    )
    if not result.ok or result.bundle is None:
        raise ApplyDraftError(
            "reload_validate_failed",
            result.error or "validation failed",
        )

    apply_result = await reload_coordinator.apply(
        result.bundle,
        version_id=active.id,
        subject=subject or "",
    )
    if not apply_result.ok:
        raise ApplyDraftError(
            "reload_apply_failed",
            apply_result.error or "apply failed",
            status_code=500,
        )
    return active.id
