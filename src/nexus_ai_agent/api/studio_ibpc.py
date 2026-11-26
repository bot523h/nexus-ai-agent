"""Principal-authenticated, membership-authorized IBPC & privacy Studio routes (Wave 3.4)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.adapters.studio_principal_auth import (
    PrincipalAuthenticationError,
    ProjectMembershipAuthorizer,
    StudioPrincipalAuthStore,
    build_studio_ibpc_service,
    durable_studio_auth_path,
)
from nexus_ai_agent.api.studio_session import require_studio_caller
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError, StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import (
    StudioAction,
    StudioAuthorizationError,
    StudioRole,
)
from nexus_ai_agent.nagar.ibpc.models import DataCategory, PrivacyMode, ProcessingScope
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyViolation
from nexus_ai_agent.nagar.ibpc.service import IBPCService, PipelineResult
from nexus_ai_agent.nagar.ibpc.session import (
    DuplicateTurnError,
    IdempotencyConflictError,
    SessionClosedError,
    SessionEpochConflictError,
    SessionError,
    StaleRevisionError,
    StaleTurnError,
    TransitionError,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

router = APIRouter(
    prefix="/api/studio",
    tags=["studio-ibpc"],
    dependencies=[Depends(require_studio_caller)],
)


def _studio_workspace_root() -> Path:
    configured = os.getenv("NEXUS_STUDIO_WORKSPACE_ROOT", "").strip()
    if configured:
        return Path(configured)
    return Path("workspace")


def get_studio_auth_store() -> StudioPrincipalAuthStore:
    db_path = os.getenv("NEXUS_STUDIO_AUTH_DB", "").strip()
    target = Path(db_path) if db_path else durable_studio_auth_path(_studio_workspace_root())
    return StudioPrincipalAuthStore(target)


def _authenticate_principal(
    auth_store: StudioPrincipalAuthStore,
    principal_token: str | None,
) -> str:
    try:
        return auth_store.authenticate_token(principal_token)
    except PrincipalAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _authorize(
    auth_store: StudioPrincipalAuthStore,
    *,
    principal_id: str,
    project_id: str,
    action: StudioAction,
    binding_ref: str = "",
) -> None:
    if not _ID_RE.fullmatch(project_id):
        raise HTTPException(status_code=400, detail="invalid project_id")
    authorizer = ProjectMembershipAuthorizer(auth_store)
    try:
        authorizer.authorize_action(
            principal_id=principal_id,
            project_id=project_id,
            action=action,
            binding_ref=binding_ref,
        )
    except StudioAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _build_service(
    auth_store: StudioPrincipalAuthStore,
    *,
    binding_ref: str | None,
) -> IBPCService:
    from nexus_ai_agent.api.app import get_creative_registry

    queue = get_creative_registry().queue
    revision = int(os.getenv("NEXUS_STUDIO_ACTIVE_REVISION", "0") or "0")
    try:
        key_provider = StaticKeyRing.from_environment(os.environ)
    except KeyProviderError:
        key_provider = StaticKeyRing({"studio-default": b"s" * 32}, active_key_id="studio-default")
    return build_studio_ibpc_service(
        queue=queue,
        registry=build_runtime_registry(),
        workspace_root=_studio_workspace_root(),
        revision_provider=lambda: revision,
        key_provider=key_provider,
        auth_store=auth_store,
        channel_binding_ref=binding_ref,
    )


class OpenSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: int = Field(ge=0, default=1)
    chat_id: int = Field(ge=0, default=1)
    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_VAULT
    consent_scope: str = ""
    current_revision: int = Field(ge=0, default=0)
    active_revision_id: str | None = None


class SubmitTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=64)
    sequence_number: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2048)
    asset_id: str = Field(min_length=1, max_length=128)
    source_path: str = Field(min_length=1, max_length=1024)
    idempotency_key: str | None = None
    target_revision_id: str | None = None
    expected_epoch: int | None = None


class ApproveTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)


class GrantConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PrivacyMode
    processing_scope: ProcessingScope
    data_category: DataCategory
    provider_id: str = ""
    expires_at_utc: str = ""


class RevokeConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = "user_revoked"


class GrantMembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: str = Field(min_length=1, max_length=128)
    role: StudioRole


@router.post("/projects/{project_id}/ibpc/sessions", status_code=201)
async def open_ibpc_session(
    project_id: str,
    body: OpenSessionRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.SUBMIT_INTENT,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            sid = service.open_session(
                tenant_id=body.tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                user_id=body.user_id,
                chat_id=body.chat_id,
                privacy_mode=body.privacy_mode,
                consent_scope=body.consent_scope,
                current_revision=body.current_revision,
                active_revision_id=body.active_revision_id,
            )
            sess = service.get_session(sid, principal_id=principal_id)
            return {
                "session_id": sess.session_id,
                "project_id": sess.project_id,
                "principal_id": sess.principal_id,
                "status": sess.status.value,
                "active_revision_id": sess.active_revision_id,
                "session_epoch": sess.session_epoch,
            }
        except PrivacyViolation as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.get("/projects/{project_id}/ibpc/sessions/{session_id}")
async def get_ibpc_session(
    project_id: str,
    session_id: str,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.READ_SESSION,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            sess = service.get_session(session_id, principal_id=principal_id)
            if sess.project_id != project_id:
                raise HTTPException(status_code=404, detail="session not found")
            return {
                "session_id": sess.session_id,
                "project_id": sess.project_id,
                "principal_id": sess.principal_id,
                "status": sess.status.value,
                "active_revision_id": sess.active_revision_id,
                "session_epoch": sess.session_epoch,
                "turns": [t.evidence() for t in service.sessions.list_turns(sess.session_id)],
            }
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/sessions/{session_id}/close")
async def close_ibpc_session(
    project_id: str,
    session_id: str,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.CLOSE_SESSION,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            sess = service.close_session(session_id, principal_id=principal_id)
            return {"session_id": sess.session_id, "status": sess.status.value}
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/sessions/{session_id}/turns", status_code=201)
async def submit_ibpc_turn(
    project_id: str,
    session_id: str,
    body: SubmitTurnRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.SUBMIT_INTENT,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            result = service.submit_text_intent(
                session_id,
                body.turn_id,
                sequence_number=body.sequence_number,
                text=body.text,
                asset_id=body.asset_id,
                source_path=body.source_path,
                idempotency_key=body.idempotency_key,
                target_revision_id=body.target_revision_id,
                expected_epoch=body.expected_epoch,
            )
            if not isinstance(result, PipelineResult):
                raise HTTPException(status_code=422, detail=str(result))
            return {
                "turn_id": result.turn_id,
                "approval_id": result.approval_id,
                "intent_digest": result.intent_digest,
                "plan_digest": result.plan.plan_digest,
                "preview_digest": result.preview.preview_digest,
            }
        except (
            DuplicateTurnError,
            IdempotencyConflictError,
            StaleRevisionError,
            StaleTurnError,
            SessionEpochConflictError,
            SessionClosedError,
            TransitionError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/sessions/{session_id}/turns/{turn_id}/approve")
async def approve_ibpc_turn(
    project_id: str,
    session_id: str,
    turn_id: str,
    body: ApproveTurnRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.APPROVE_PLAN,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            binding = service.approve(
                session_id, turn_id, body.approval_id, principal_id=principal_id
            )
            return {
                "approval_id": binding.approval_id,
                "state": binding.state.value,
                "approval_hash": binding.approval_hash,
            }
        except (ApprovalError, SessionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/sessions/{session_id}/turns/{turn_id}/execute")
async def execute_ibpc_turn(
    project_id: str,
    session_id: str,
    turn_id: str,
    body: ApproveTurnRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.EXECUTE_PLAN,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            outcome = await service.execute(
                session_id, turn_id, body.approval_id, principal_id=principal_id
            )
            if outcome.status == "refused":
                raise HTTPException(status_code=409, detail=outcome.detail)
            return {
                "status": outcome.status,
                "job_id": outcome.job_id,
                "detail": outcome.detail,
            }
        except StudioAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/privacy/consents", status_code=201)
async def grant_project_consent(
    project_id: str,
    body: GrantConsentRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.MANAGE_CONSENT,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            grant = service.grant_privacy_consent(
                project_id=project_id,
                principal_id=principal_id,
                mode=body.mode,
                processing_scope=body.processing_scope,
                data_category=body.data_category,
                provider_id=body.provider_id,
                expires_at_utc=body.expires_at_utc,
            )
            return grant.model_dump(mode="json")
        except PrivacyViolation as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/ibpc/privacy/consents/{consent_id}/revoke")
async def revoke_project_consent(
    project_id: str,
    consent_id: str,
    body: RevokeConsentRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.MANAGE_CONSENT,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            grant = service.revoke_privacy_consent(
                consent_id,
                project_id=project_id,
                actor_id=principal_id,
                reason=body.reason,
            )
            return grant.model_dump(mode="json")
        except PrivacyViolation as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            service.close()
    finally:
        auth_store.close()


@router.get("/projects/{project_id}/ibpc/privacy/audit")
async def read_project_privacy_audit(
    project_id: str,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        principal_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(principal_id) or ""
        _authorize(
            auth_store,
            principal_id=principal_id,
            project_id=project_id,
            action=StudioAction.READ_AUDIT,
            binding_ref=binding_ref,
        )
        service = _build_service(auth_store, binding_ref=binding_ref)
        try:
            entries = service.list_privacy_audit(project_id=project_id, principal_id=principal_id)
            return {"audit": [e.model_dump(mode="json") for e in entries]}
        finally:
            service.close()
    finally:
        auth_store.close()


@router.post("/projects/{project_id}/memberships", status_code=201)
async def grant_project_membership(
    project_id: str,
    body: GrantMembershipRequest,
    x_nexus_principal_token: str | None = Header(default=None, alias="X-Nexus-Principal-Token"),
) -> dict[str, Any]:
    auth_store = get_studio_auth_store()
    try:
        actor_id = _authenticate_principal(auth_store, x_nexus_principal_token)
        binding_ref = auth_store.default_binding_for(actor_id) or ""
        _authorize(
            auth_store,
            principal_id=actor_id,
            project_id=project_id,
            action=StudioAction.MANAGE_MEMBERSHIP,
            binding_ref=binding_ref,
        )
        membership_id = auth_store.grant_membership(
            project_id, body.principal_id, role=body.role, granted_by=actor_id
        )
        return {
            "membership_id": membership_id,
            "project_id": project_id,
            "principal_id": body.principal_id,
            "role": body.role.value,
        }
    finally:
        auth_store.close()


__all__ = ["get_studio_auth_store", "router"]
