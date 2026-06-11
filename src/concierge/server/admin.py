"""Debug/admin endpoints. Same auth as /mcp.

Adds the P1-1 token-administration surface: mint / rotate / revoke per-tenant
tokens and revoke any token id (including OIDC ``jti``). The minted secret is
returned exactly once, at creation — it is never persisted in the clear and never
re-readable. These endpoints sit behind the same ``AuthProvider`` as the rest of
the gateway; in production, restrict them further at the ingress.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters.manager import AdapterManager
from ..admin.config_draft import ApplyDraftError, apply_draft_live, reload_active_live
from ..admin.config_store import ConfigStore
from ..admin.models import ValidationResult as StoreValidationResult
from ..admin.redaction import redact_config_diff, redact_config_for_api
from ..core.catalog import Catalog
from ..core.session import SessionManager
from ..gateway.service import GatewayService
from ..policy.approval import QueuedApprovalBroker
from ..policy.approval_store import ApprovalStore
from .auth import AuthProvider, AuthResult
from .revocation import RevocationStore
from .tenant_tokens import TenantTokenStore


class MintTokenRequest(BaseModel):
    tenant_id: str = Field(min_length=1)


class RotateTokenRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    old_token_id: str = Field(min_length=1)
    # Revoke the old id too (default), so a cached copy of the old secret dies.
    revoke_old: bool = True


class RevokeRequest(BaseModel):
    token_id: str = Field(min_length=1)
    # Optional TTL so a short-lived JWT's revocation self-prunes when it expires.
    ttl_s: int | None = None


class ApprovalDecisionRequest(BaseModel):
    approval_id: str = Field(min_length=1)
    # Optional free-text rationale recorded on the decision + sent in the webhook.
    reason: str | None = None


class DraftConfigRequest(BaseModel):
    config: dict[str, Any]
    created_by: str | None = None


class ImportYamlRequest(BaseModel):
    yaml: str
    created_by: str | None = None
    expand_environment: bool = True


class ValidationIssueResponse(BaseModel):
    path: str
    message: str
    code: str | None = None


class ValidationResponse(BaseModel):
    ok: bool
    issues: list[ValidationIssueResponse] = Field(default_factory=list)


def _validation_response(result: StoreValidationResult) -> ValidationResponse:
    return ValidationResponse(
        ok=result.ok,
        issues=[
            ValidationIssueResponse(path=i.path, message=i.message, code=i.code)
            for i in result.issues
        ],
    )


def _version_meta(version: Any) -> dict[str, Any]:
    return {
        "id": version.id,
        "status": version.status.value,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "parent_id": version.parent_id,
        "promoted_at": version.promoted_at.isoformat() if version.promoted_at else None,
        "validation_ok": version.validation.ok if version.validation else None,
    }


if TYPE_CHECKING:
    from ..admin.reload import ReloadCoordinator


def build_admin_router(
    *,
    auth: AuthProvider,
    catalog: Catalog,
    adapters: AdapterManager,
    sessions: SessionManager,
    service: GatewayService,
    revocation: RevocationStore | None = None,
    tenant_tokens: TenantTokenStore | None = None,
    approval_broker: object | None = None,
    approval_store: ApprovalStore | None = None,
    operator_subjects: list[str] | None = None,
    config_store: ConfigStore | None = None,
    reload_coordinator: ReloadCoordinator | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])
    operator_allow = set(operator_subjects or [])

    async def _auth_dep(request: Request) -> None:
        await auth.authenticate(request)

    async def _operator(request: Request) -> AuthResult:
        """Authenticate the caller as an operator permitted to decide approvals.

        Model (documented in docs/AUTH.md): the caller must authenticate via the
        P1-1 chain. When ``operator_subjects`` is configured, the authenticated
        subject must be in that allow-list; an empty list means any authenticated
        principal may decide — but only for approvals belonging to *their own*
        tenant (enforced per-decision below). Cross-tenant grants are forbidden.
        """
        result = await auth.authenticate(request)
        if operator_allow and (result.subject is None or result.subject not in operator_allow):
            raise HTTPException(status_code=403, detail="not an approval operator")
        return result

    @router.get("/health", dependencies=[Depends(_auth_dep)])
    async def health() -> dict:
        return {
            "ok": True,
            "catalog_count": await catalog.count(),
            "session_count": len(await sessions.all()),
            "servers": [a.health().model_dump() for a in adapters.all()],
        }

    @router.get("/catalog", dependencies=[Depends(_auth_dep)])
    async def list_catalog() -> dict:
        return {"entries": [e.model_dump() for e in await catalog.list(limit=10_000)]}

    @router.get("/sessions", dependencies=[Depends(_auth_dep)])
    async def list_sessions() -> dict:
        return {"sessions": [s.model_dump(mode="json") for s in await sessions.all()]}

    @router.post("/refresh/{server_id}", dependencies=[Depends(_auth_dep)])
    async def refresh(server_id: str) -> dict:
        n = await adapters.refresh_server(server_id)
        return {"server": server_id, "entries": n}

    # -- P1-1 token administration -----------------------------------------

    @router.post("/tokens", dependencies=[Depends(_auth_dep)])
    async def mint_token(body: MintTokenRequest) -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        minted = await tenant_tokens.mint(body.tenant_id)
        # The raw token is returned ONCE here; it is never stored or logged.
        return {
            "token": minted.token,
            "token_id": minted.token_id,
            "tenant_id": minted.tenant_id,
        }

    @router.post("/tokens/rotate", dependencies=[Depends(_auth_dep)])
    async def rotate_token(body: RotateTokenRequest) -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        minted = await tenant_tokens.rotate(body.tenant_id, body.old_token_id)
        if body.revoke_old and revocation is not None:
            await revocation.revoke(body.old_token_id)
        return {
            "token": minted.token,
            "token_id": minted.token_id,
            "tenant_id": minted.tenant_id,
            "revoked_old": bool(body.revoke_old and revocation is not None),
        }

    @router.get("/tokens", dependencies=[Depends(_auth_dep)])
    async def list_tokens() -> dict:
        if tenant_tokens is None:
            raise HTTPException(status_code=501, detail="tenant token store not configured")
        # Never returns secrets — only the opaque ids + tenant bindings.
        return {
            "tokens": [
                {"token_id": r.token_id, "tenant_id": r.tenant_id}
                for r in await tenant_tokens.list_records()
            ]
        }

    @router.post("/revocations", dependencies=[Depends(_auth_dep)])
    async def revoke(body: RevokeRequest) -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        await revocation.revoke(body.token_id, ttl_s=body.ttl_s)
        return {"revoked": body.token_id}

    @router.delete("/revocations/{token_id}", dependencies=[Depends(_auth_dep)])
    async def unrevoke(token_id: str) -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        await revocation.unrevoke(token_id)
        return {"unrevoked": token_id}

    @router.get("/revocations", dependencies=[Depends(_auth_dep)])
    async def list_revocations() -> dict:
        if revocation is None:
            raise HTTPException(status_code=501, detail="revocation store not configured")
        return {"revoked": await revocation.all()}

    # -- P1-3 approval decisions -------------------------------------------

    def _require_queue() -> QueuedApprovalBroker:
        if not isinstance(approval_broker, QueuedApprovalBroker):
            raise HTTPException(
                status_code=501,
                detail="approval queue not enabled (policy.approval_mode != 'queue')",
            )
        return approval_broker

    @router.get("/approvals")
    async def list_approvals(operator: AuthResult = Depends(_operator)) -> dict:
        if approval_store is None:
            raise HTTPException(status_code=501, detail="approval store not configured")
        # An operator only ever sees pending approvals for their own tenant.
        pending = await approval_store.list_pending(tenant_id=operator.tenant_id)
        return {"approvals": [r.to_public() for r in pending]}

    async def _decide(
        body: ApprovalDecisionRequest, operator: AuthResult, *, granted: bool
    ) -> dict:
        broker = _require_queue()
        # Tenant-scoped: the store refuses a decision on another tenant's record
        # (returns None), so a cross-tenant grant is impossible.
        record = await broker.decide(
            body.approval_id,
            granted=granted,
            decided_by=operator.subject or "operator",
            tenant_id=operator.tenant_id,
            reason=body.reason,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="unknown approval for this tenant")
        return {"approval": record.to_public()}

    @router.post("/approvals/grant")
    async def grant_approval(
        body: ApprovalDecisionRequest, operator: AuthResult = Depends(_operator)
    ) -> dict:
        return await _decide(body, operator, granted=True)

    @router.post("/approvals/deny")
    async def deny_approval(
        body: ApprovalDecisionRequest, operator: AuthResult = Depends(_operator)
    ) -> dict:
        return await _decide(body, operator, granted=False)

    # -- P2 admin runtime configuration --------------------------------------

    def _require_config_store() -> ConfigStore:
        if config_store is None:
            raise HTTPException(status_code=501, detail="config store not configured")
        return config_store

    async def _auth_subject(request: Request) -> AuthResult:
        return await auth.authenticate(request)

    @router.get("/config/active", dependencies=[Depends(_auth_dep)])
    async def get_active_config() -> dict:
        store = _require_config_store()
        active = await store.get_active_version()
        if active is None:
            return {"config": None, "version": None}
        return {
            "config": active.redacted_config(),
            "version": _version_meta(active),
        }

    @router.get("/config/draft", dependencies=[Depends(_auth_dep)])
    async def get_draft_config() -> dict:
        store = _require_config_store()
        draft = await store.get_draft()
        if draft is None:
            return {"config": None, "version": None}
        return {
            "config": draft.redacted_config(),
            "version": _version_meta(draft),
        }

    @router.put("/config/draft", dependencies=[Depends(_auth_dep)])
    async def update_draft_config(
        body: DraftConfigRequest,
        auth_result: AuthResult = Depends(_auth_subject),
    ) -> dict:
        store = _require_config_store()
        created_by = body.created_by or auth_result.subject
        draft = await store.create_or_update_draft(body.config, created_by=created_by)
        return {
            "config": draft.redacted_config(),
            "version": _version_meta(draft),
        }

    @router.post("/config/draft/validate", dependencies=[Depends(_auth_dep)])
    async def validate_draft_config() -> ValidationResponse:
        store = _require_config_store()
        result = await store.validate_draft()
        return _validation_response(result)

    @router.get("/config/draft/diff", dependencies=[Depends(_auth_dep)])
    async def preview_draft_diff() -> dict:
        store = _require_config_store()
        draft = await store.get_draft()
        if draft is None:
            raise HTTPException(status_code=404, detail="no draft config")
        active = await store.get_active_redacted() or {}
        diff = redact_config_diff(active, redact_config_for_api(draft.config))
        return {"diff": diff}

    @router.post("/config/apply", dependencies=[Depends(_auth_dep)])
    async def apply_draft_config(
        auth_result: AuthResult = Depends(_auth_subject),
    ) -> dict:
        store = _require_config_store()
        try:
            active, result = await apply_draft_live(
                store,
                reload_coordinator=reload_coordinator,
                subject=auth_result.subject,
            )
        except ApplyDraftError as e:
            if e.code == "apply_reload_failed":
                raise HTTPException(
                    status_code=e.status_code,
                    detail={"code": e.code, "message": e.message},
                ) from e
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        return {
            "version": _version_meta(active),
            "applied": True,
            "runtime_applied": result.runtime_applied,
        }

    @router.post("/config/reload", dependencies=[Depends(_auth_dep)])
    async def reload_runtime_config(
        auth_result: AuthResult = Depends(_auth_subject),
    ) -> dict:
        if reload_coordinator is None:
            raise HTTPException(status_code=501, detail="reload coordinator not configured")
        store = _require_config_store()
        try:
            version_id = await reload_active_live(
                store,
                reload_coordinator,
                subject=auth_result.subject,
            )
        except ApplyDraftError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail={"code": e.code, "message": e.message},
            ) from e
        return {"reloaded": True, "version_id": version_id}

    @router.post("/config/rollback", dependencies=[Depends(_auth_dep)])
    async def rollback_config() -> dict:
        store = _require_config_store()
        try:
            rolled = await store.rollback()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"version": _version_meta(rolled), "rolled_back": True}

    @router.post("/config/import", dependencies=[Depends(_auth_dep)])
    async def import_config_yaml(
        body: ImportYamlRequest,
        auth_result: AuthResult = Depends(_auth_subject),
    ) -> dict:
        store = _require_config_store()
        created_by = body.created_by or auth_result.subject
        try:
            draft = await store.import_yaml(
                body.yaml,
                created_by=created_by,
                expand_environment=body.expand_environment,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "config": draft.redacted_config(),
            "version": _version_meta(draft),
        }

    @router.get("/config/export", dependencies=[Depends(_auth_dep)])
    async def export_active_config_yaml() -> dict:
        store = _require_config_store()
        exported = await store.export_active_yaml_redacted()
        if exported is None:
            raise HTTPException(status_code=404, detail="no active config to export")
        return {"yaml": exported}

    @router.get("/config/versions", dependencies=[Depends(_auth_dep)])
    async def list_config_versions() -> dict:
        store = _require_config_store()
        versions = await store.list_versions()
        return {
            "versions": [
                {
                    "id": v.id,
                    "status": v.status.value,
                    "created_at": v.created_at.isoformat(),
                    "created_by": v.created_by,
                    "validation_ok": v.validation_ok,
                    "parent_id": v.parent_id,
                    "promoted_at": v.promoted_at.isoformat() if v.promoted_at else None,
                }
                for v in versions
            ]
        }

    @router.get("/config/status", dependencies=[Depends(_auth_dep)])
    async def config_runtime_status() -> dict:
        store = _require_config_store()
        active = await store.get_active_version()
        draft = await store.get_draft()
        return {
            "config_store": True,
            "active_version_id": active.id if active else None,
            "draft_version_id": draft.id if draft else None,
            "draft_status": draft.status.value if draft else None,
            "reload_coordinator": reload_coordinator is not None,
            "catalog_count": await catalog.count(),
            "session_count": len(await sessions.all()),
            "servers": [a.health().model_dump() for a in adapters.all()],
        }

    return router
