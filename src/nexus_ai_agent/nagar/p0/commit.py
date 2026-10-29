"""Canonical P0 commit adapter for the existing Creative Studio queue path."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.packs.edit.models import TrimInput
from nexus_ai_agent.creative.render_jobs import CREATIVE_RENDER_JOB_TYPE
from nexus_ai_agent.creative.studio.models import ActorIdentity, Project
from nexus_ai_agent.nagar.creative.handoff import (
    CanonicalHandoffFacts,
    build_creative_payload,
    source_asset_facts,
)
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, digest_of
from nexus_ai_agent.provenance.observer import utc_now_iso

from .runtime import P0Preview, canonical_command_input


class P0CommitContext(BaseModel):
    """Composition-root facts; callers cannot derive these from a preview."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)
    actor: ActorIdentity
    tenant_id: str = Field(min_length=1)
    project: Project
    directory: Any
    queue: Any
    channel_binding_id: str = Field(pattern=r"^cbn_[0-9a-f]{64}$")
    workspace_dir: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    source_text: str = Field(min_length=1, max_length=16_384)
    user_id: int
    chat_id: int
    lang: str = "en"
    causal_observer: Any | None = None


class P0CommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["enqueued"]
    job_id: str
    idempotency_key: str
    operation: Literal["timeline.trim"] = "timeline.trim"
    preview_revision: int
    canonical_payload: dict[str, object]
    causal_recorded: bool = True


def _idempotency_key(preview: P0Preview, context: P0CommitContext, payload: dict[str, Any]) -> str:
    if preview.plan is None or preview.ir is None:
        raise ValueError("preview_plan_missing")
    body = {
        "project_id": context.project.project_id,
        "tenant_id": context.tenant_id,
        "actor_id": context.actor.actor_id,
        "plan": preview.plan.model_dump(mode="json"),
        "ir": preview.ir.model_dump(mode="json"),
        "payload": payload,
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:40]
    return f"nagar-p0:{digest}"


async def commit_preview(preview: P0Preview, context: P0CommitContext) -> P0CommitResult:
    """Recheck trusted authority, then enqueue a typed canonical render request.

    This function never dispatches a renderer or a second bus. The existing
    worker rechecks authorization, policy, revision, CommandBus schema and
    artifact verification after the durable queue insert.
    """
    if preview.status != "PREVIEW_READY" or preview.plan is None or preview.segment is None:
        raise ValueError("preview_not_commit_ready")
    if preview.plan.project_id != context.project.project_id:
        raise PermissionError("preview_project_mismatch")
    if preview.plan.tenant_id != context.tenant_id:
        raise PermissionError("preview_tenant_mismatch")
    if preview.plan.timeline_revision != context.project.state_revision:
        raise ValueError("stale_preview_revision")
    if preview.segment.timeline_revision != context.project.state_revision:
        raise ValueError("stale_segment_revision")
    if context.project.state_revision != 0:
        raise ValueError("commit_revision_seam_unavailable")
    if preview.segment.asset_id != "src":
        raise ValueError("canonical_worker_requires_src_asset")
    if context.causal_observer is None:
        raise ValueError("causal_observer_required")

    # Authoritative membership/binding checks happen before queue insertion.
    access = context.directory.authorize(context.actor, context.project.project_id)
    principal = PrincipalId(context.actor.actor_id)
    context.directory.require_binding(context.channel_binding_id, principal)
    access.require_permissions(("project:read", "project:write"))
    source = source_asset_facts(context.project, preview.segment.asset_id)
    canonical_input = TrimInput.model_validate(canonical_command_input(preview))
    if canonical_input.out_point_us > int(source["duration_us"]):
        raise ValueError("canonical_trim_exceeds_authoritative_source")

    proposal = _PreviewProposal(
        operation="timeline.trim",
        input=canonical_input.model_dump(mode="json"),
    )
    idempotency_key = _idempotency_key(preview, context, proposal.input)
    payload = build_creative_payload(
        proposal,
        workspace_dir=context.workspace_dir,
        input_path=context.input_path,
        media_duration_us=int(source["duration_us"]),
        user_id=context.user_id,
        chat_id=context.chat_id,
        lang=context.lang,
        idempotency_key=idempotency_key,
        canonical=CanonicalHandoffFacts(
            principal_id=context.actor.actor_id,
            channel_binding_id=context.channel_binding_id,
            project_id=context.project.project_id,
        ),
        source_text=context.source_text,
    )
    # Validate the complete queue boundary before the insert. The worker repeats
    # these checks; this preflight ensures malformed payloads create no row.
    from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload

    CreativeRenderPayload.model_validate(payload)
    job_id = await context.queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    context.causal_observer.observe(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=job_id,
            job_type=CREATIVE_RENDER_JOB_TYPE,
            idempotency_key=idempotency_key,
            status="PENDING",
            payload_digest=digest_of(payload),
            detail={
                "intent_id": preview.plan.canonical_nel_hash,
                "operation_id": idempotency_key,
                "semantic_segment_id": preview.plan.semantic_segment_id,
                "timeline_revision": preview.plan.timeline_revision,
                "required_evidence": list(preview.plan.required_evidence),
            },
            occurred_at=utc_now_iso(),
        )
    )
    return P0CommitResult(
        status="enqueued",
        job_id=job_id,
        idempotency_key=idempotency_key,
        preview_revision=preview.plan.timeline_revision,
        canonical_payload=canonical_input.model_dump(mode="json", exclude_none=True),
    )


class _PreviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["timeline.trim"]
    input: dict[str, object]
