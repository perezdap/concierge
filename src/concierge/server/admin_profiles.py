"""Admin profile management API (P2-ADMIN-6).

Mutates ``profiles`` on the runtime config draft. Preview resolves selectors against
the live catalog via :class:`~concierge.gateway.profiles.Profile`.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..admin.config_store import ConfigStore
from ..admin.models import ValidationIssue
from ..admin.models import ValidationResult as StoreValidationResult
from ..admin.reload import ReloadCoordinator, ReloadError
from ..config import GatewayConfig, ProfileConfig
from ..core.catalog import Catalog
from ..core.types import CatalogEntry, PrimitiveType
from ..gateway.profiles import Profile, ProfileSelector
from .auth import AuthProvider, AuthResult


class ProfileBody(BaseModel):
    profile: dict[str, Any]


class DuplicateProfileRequest(BaseModel):
    new_name: str = Field(min_length=1)


@dataclass
class AdminProfilesDeps:
    auth: AuthProvider
    config_store: ConfigStore
    catalog: Catalog
    reload_coordinator: ReloadCoordinator | None = None


def _validation_error_detail(validation: StoreValidationResult) -> dict[str, Any]:
    return {
        "code": "validation_failed",
        "issues": [i.model_dump() for i in validation.issues],
    }


def _validate_profile_dict(data: dict[str, Any]) -> StoreValidationResult:
    try:
        ProfileConfig.model_validate(data)
    except ValidationError as exc:
        return StoreValidationResult(
            ok=False,
            issues=[
                ValidationIssue(
                    path=".".join(str(p) for p in err["loc"]),
                    message=err["msg"],
                    code=err.get("type"),
                )
                for err in exc.errors()
            ],
        )
    return StoreValidationResult(ok=True)


def _profile_from_dict(data: dict[str, Any]) -> Profile:
    selectors = [
        ProfileSelector(
            server=s.get("server"),
            tags=list(s.get("tags") or []),
            categories=list(s.get("categories") or []),
            primitive_type=(
                PrimitiveType(s["primitive_type"])
                if s.get("primitive_type")
                else None
            ),
            names=list(s.get("names") or []),
        )
        for s in data.get("selectors") or []
    ]
    return Profile(
        name=data["name"],
        description=data.get("description", ""),
        auto_apply=bool(data.get("auto_apply", False)),
        selectors=selectors,
    )


def _entry_public(entry: CatalogEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json")


async def _preview_profile(profile: Profile, catalog: Catalog) -> dict[str, Any]:
    matched_names = await profile.resolve(catalog)
    warnings: list[str] = []
    if profile.selectors and not matched_names:
        warnings.append("no catalog entries matched any selector")
    for i, selector in enumerate(profile.selectors):
        probe = Profile(name="_preview", selectors=[selector])
        if not await probe.resolve(catalog):
            parts = []
            if selector.server:
                parts.append(f"server={selector.server}")
            if selector.tags:
                parts.append(f"tags={selector.tags}")
            if selector.categories:
                parts.append(f"categories={selector.categories}")
            if selector.primitive_type:
                parts.append(f"primitive_type={selector.primitive_type.value}")
            if selector.names:
                parts.append(f"names={selector.names}")
            hint = ", ".join(parts) if parts else "empty selector"
            warnings.append(f"selector {i} matched no catalog entries ({hint})")

    if matched_names:
        entries = await catalog.list(names=set(matched_names), limit=500)
    else:
        entries = []

    return {
        "profile": profile.name,
        "matched_count": len(entries),
        "canonical_names": matched_names,
        "primitives": [_entry_public(e) for e in entries],
        "warnings": warnings,
    }


async def _config_for_reads(store: ConfigStore) -> dict[str, Any]:
    draft = await store.get_draft()
    if draft is not None:
        return draft.config
    active = await store.get_active_version()
    if active is not None:
        return active.config
    return GatewayConfig().model_dump(mode="json")


async def _ensure_draft(
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


def _find_profile_index(profiles: list[dict[str, Any]], name: str) -> int:
    for i, entry in enumerate(profiles):
        if entry.get("name") == name:
            return i
    raise HTTPException(status_code=404, detail=f"unknown profile: {name}")


def build_admin_profiles_router(deps: AdminProfilesDeps) -> APIRouter:
    router = APIRouter(prefix="/admin/profiles", tags=["admin-profiles"])

    async def _auth(request: Request) -> AuthResult:
        return await deps.auth.authenticate(request)

    @router.get("")
    async def list_profiles(_auth: AuthResult = Depends(_auth)) -> dict:
        cfg = await _config_for_reads(deps.config_store)
        profiles = cfg.get("profiles") or []
        return {
            "profiles": profiles,
            "source": "draft" if (await deps.config_store.get_draft()) else "active",
        }

    @router.get("/{name}")
    async def get_profile(name: str, _auth: AuthResult = Depends(_auth)) -> dict:
        cfg = await _config_for_reads(deps.config_store)
        profiles = cfg.get("profiles") or []
        idx = _find_profile_index(profiles, name)
        return {"profile": profiles[idx]}

    @router.post("")
    async def create_profile(
        body: ProfileBody,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        profile_name = body.profile.get("name")
        if not profile_name or not isinstance(profile_name, str):
            raise HTTPException(status_code=400, detail="profile.name is required")
        cfg = await _ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        profiles = list(cfg.get("profiles") or [])
        if any(p.get("name") == profile_name for p in profiles):
            raise HTTPException(
                status_code=409,
                detail=f"profile already exists: {profile_name}",
            )
        validation = _validate_profile_dict(body.profile)
        if not validation.ok:
            raise HTTPException(
                status_code=400,
                detail=_validation_error_detail(validation),
            )
        profiles.append(body.profile)
        cfg["profiles"] = profiles
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {"profile": body.profile, "version_id": draft.id}

    @router.put("/{name}")
    async def update_profile(
        name: str,
        body: ProfileBody,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        if body.profile.get("name") not in (None, name):
            raise HTTPException(status_code=400, detail="profile.name must match path")
        cfg = await _ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        profiles = list(cfg.get("profiles") or [])
        idx = _find_profile_index(profiles, name)
        updated = dict(body.profile)
        updated["name"] = name
        validation = _validate_profile_dict(updated)
        if not validation.ok:
            raise HTTPException(
                status_code=400,
                detail=_validation_error_detail(validation),
            )
        profiles[idx] = updated
        cfg["profiles"] = profiles
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {"profile": updated, "version_id": draft.id}

    @router.delete("/{name}")
    async def delete_profile(
        name: str,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        cfg = await _ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        profiles = list(cfg.get("profiles") or [])
        idx = _find_profile_index(profiles, name)
        removed = profiles.pop(idx)
        cfg["profiles"] = profiles
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {
            "removed_name": name,
            "profile": removed,
            "version_id": draft.id,
        }

    @router.post("/{name}/duplicate")
    async def duplicate_profile(
        name: str,
        body: DuplicateProfileRequest,
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        cfg = await _ensure_draft(
            deps.config_store,
            created_by=auth_result.subject,
        )
        profiles = list(cfg.get("profiles") or [])
        idx = _find_profile_index(profiles, name)
        if any(p.get("name") == body.new_name for p in profiles):
            raise HTTPException(
                status_code=409,
                detail=f"profile already exists: {body.new_name}",
            )
        copy = deepcopy(profiles[idx])
        copy["name"] = body.new_name
        validation = _validate_profile_dict(copy)
        if not validation.ok:
            raise HTTPException(
                status_code=400,
                detail=_validation_error_detail(validation),
            )
        profiles.append(copy)
        cfg["profiles"] = profiles
        draft = await deps.config_store.create_or_update_draft(
            cfg,
            created_by=auth_result.subject,
        )
        return {"profile": copy, "version_id": draft.id}

    @router.post("/{name}/preview")
    async def preview_profile(
        name: str,
        body: ProfileBody | None = None,
        _auth: AuthResult = Depends(_auth),
    ) -> dict:
        if body is not None and body.profile.get("name") not in (None, name):
            raise HTTPException(status_code=400, detail="profile.name must match path")
        if body is not None:
            data = body.profile
            data["name"] = name
        else:
            cfg = await _config_for_reads(deps.config_store)
            profiles = cfg.get("profiles") or []
            idx = _find_profile_index(profiles, name)
            data = profiles[idx]
        validation = _validate_profile_dict(data)
        if not validation.ok:
            raise HTTPException(
                status_code=400,
                detail=_validation_error_detail(validation),
            )
        profile = _profile_from_dict(data)
        return await _preview_profile(profile, deps.catalog)

    @router.post("/apply")
    async def apply_profiles_draft(
        auth_result: AuthResult = Depends(_auth),
    ) -> dict:
        draft = await deps.config_store.get_draft()
        if draft is None:
            raise HTTPException(status_code=400, detail="no draft config to apply")

        if deps.reload_coordinator is not None:
            cfg = GatewayConfig.model_validate(draft.config)
            try:
                await deps.reload_coordinator.validate_apply_reload(
                    cfg,
                    version_id=draft.id,
                    subject=auth_result.subject or "",
                )
            except ReloadError as e:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "apply_reload_failed", "message": str(e)},
                ) from e

        try:
            active = await deps.config_store.promote_draft_to_active(
                created_by=auth_result.subject,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "version_id": active.id,
            "applied": True,
            "reloaded": deps.reload_coordinator is not None,
        }

    return router