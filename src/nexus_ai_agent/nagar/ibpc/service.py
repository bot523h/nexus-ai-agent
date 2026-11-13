"""IBPC service — composes the proof-carrying live-editing slice.

The service owns **no execution primitive**: execution goes exclusively through
the injected :class:`~nexus_ai_agent.application.ports.job_queue.JobQueuePort`
(the existing canonical queue), whose worker runs the existing
``creative_render`` handler → CommandBus → render lane → verifier. The
service never imports the queue implementation, the worker, ``render_jobs``,
the studio bus, or the pack runtime (pinned by
``tests/architecture/test_ibpc_boundary.py``).

Pipeline per turn:

    text/audio → parse_intent (clarify, never guess)
               → compile_ast against the injected live registry
               → build_preview + readback from the same plan
               → pending ApprovalBinding (revision + digests + turn)
               → approve (principal + all digests re-checked)
               → execute: re-validate everything, consume the approval
                  (single-use), stage source, enqueue ONE canonical job,
                  drain, re-measure the artifact, run the independent
                  footprint verifier, build the IBPC capsule referencing the
                  existing provenance journal + passport.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.application.ports.caption_engine import CaptionEnginePort
from nexus_ai_agent.application.ports.job_queue import JobQueuePort, JobStatus
from nexus_ai_agent.connect.key_provider import KeyProvider, KeyProviderError
from nexus_ai_agent.creative.slideshow.ffmpeg import RenderError, sha256_file
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalIdentity, ApprovalLedger
from nexus_ai_agent.nagar.ibpc.approval_mac import ApprovalEnvelope, issue_envelope, verify_envelope
from nexus_ai_agent.nagar.ibpc.authority import (
    CanonicalAuthorityError,
    CanonicalAuthorityPort,
)
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused, compile_ast
from nexus_ai_agent.nagar.ibpc.delta import DeltaIntentAST, parse_delta_intent
from nexus_ai_agent.nagar.ibpc.footprint import FootprintVerifyError, probe_media, verify_keep
from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    IntentAST,
    Refusal,
    parse_intent,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalBinding,
    ApprovalState,
    CompiledPlan,
    IBPCCapsule,
    PreviewManifest,
    PrivacyMode,
    Readback,
    TypedCreativeAST,
    VerifierReport,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview, render_readback
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy
from nexus_ai_agent.nagar.ibpc.proof import evaluate_proof_gate
from nexus_ai_agent.nagar.ibpc.session import (
    LiveSessionManager,
    SessionError,
    StaleTurnError,
    TransitionError,
    TurnState,
)
from nexus_ai_agent.nagar.ibpc.shadow import build_shadow_preview
from nexus_ai_agent.nagar.ibpc.signature import compute_scope_signature
from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

#: Closed surface constants. Pinned against the canonical worker's own map by
#: tests/architecture/test_ibpc_boundary.py (which may import render_jobs; this
#: module may not): SURFACE_TO_CANONICAL[("edit", "trim")] == "timeline.trim".
JOB_TYPE = "creative_render"
COMMAND = "edit"
SURFACE_OPERATION = "trim"
CANONICAL_OPERATION: Literal["timeline.trim"] = "timeline.trim"
WORKSPACE_PREFIX = "creative_"
DRAIN_TIMEOUT_S = 300.0


class ScopeSignatureMismatchError(ApprovalError):
    """Approved execution scope changed after the human approval boundary."""

    def __init__(self) -> None:
        super().__init__("scope_signature_mismatch", "approved execution scope changed")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobFactsProvider(Protocol):
    def get_job_facts(self, job_id: str) -> Any: ...


@dataclass
class IBPCServiceDeps:
    """Everything the service needs is injected by the composition root."""

    queue: JobQueuePort
    registry: CapabilityRegistry
    workspace_root: Path
    revision_provider: Any  # Callable[[], int]
    journal: Any | None = None  # provenance.journal.CausalJournal | None
    facts_provider: JobFactsProvider | None = None
    caption_engine: CaptionEnginePort | None = None
    attempt_provider: Any | None = None  # Callable[[str], list[dict]] | None
    now: Any = utc_now  # Callable[[], str]
    verifier_binary: str | None = None
    #: Trusted, server-side canonical authority. When supplied, the service
    #: resolves + verifies the acting principal/project/binding from persistent
    #: state before any job admission (Phase 1). When absent the slice is
    #: experimental-only: execution is refused (never a silent schema-1 fallback).
    authority: CanonicalAuthorityPort | None = None
    #: The canonical channel binding reference for the composition root's
    #: session (e.g. ``cbn_<64hex>``). Trusted input, never a model/user value.
    channel_binding_ref: str | None = None
    #: Test/composition seam: the canonical intent builder. Defaults to the real
    #: ``CanonicalCreativeIntent.create`` (pinned against the worker's own
    #: builder by tests/architecture/test_ibpc_boundary.py).
    intent_builder: Any | None = None
    #: Bounded retention for the in-process execution-context store. Only
    #: *terminal* contexts (approval CONSUMED/REVOKED) are ever evicted, oldest
    #: first, so an active/pending approval can never lose its context. Set to
    #: ``None`` to keep every context (unbounded, the legacy behaviour).
    max_contexts: int | None = 1024
    #: Shared, stable key ring used only for authenticated approval envelopes.
    #: None permits planning but fails closed at approval/admission.
    approval_key_provider: KeyProvider | None = None


@dataclass(frozen=True)
class PipelineResult:
    """Everything a caller needs to show the readback and ask for approval."""

    turn_id: str
    approval_id: str
    intent_digest: str
    ast: TypedCreativeAST
    plan: CompiledPlan
    preview: PreviewManifest
    readback: Readback
    shadow_manifest: Any = None
    media_signature: Any = None


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str  # "executed" | "failed" | "refused"
    job_id: str | None = None
    capsule: IBPCCapsule | None = None
    report: VerifierReport | None = None
    detail: str = ""


@dataclass
class _TurnContext:
    ast: TypedCreativeAST
    plan: CompiledPlan
    preview: PreviewManifest
    readback: Readback
    intent_digest: str
    source_path: Path
    source_asset_digest: str
    duration_us: int
    fps: float
    session_id: str
    turn_id: str
    tenant_id: str
    project_id: str
    principal_id: str
    #: canonical binding reference the intent was compiled under
    binding_ref: str = ""
    #: verified principal/project/binding resolved from server state (Phase 1)
    verified_identity: Any = None
    #: insertion order in the context store — the eviction recency key. Only
    #: terminal contexts are evicted, oldest first.
    created_order: int = 0
    scope_signature: str | None = None
    shadow_manifest: Any = None
    media_signature: Any = None
    approval_envelope: ApprovalEnvelope | None = None
    parent_proposal_id: str | None = None
    parent_scope_digest: str | None = None


class IBPCService:
    """Proof-carrying live editing over the canonical execution chain."""

    def __init__(self, deps: IBPCServiceDeps) -> None:
        self._deps = deps
        self._privacy = PrivacyPolicy(now=deps.now)
        self._approvals = ApprovalLedger(now=deps.now)
        self._sessions = LiveSessionManager(
            now=deps.now, privacy=self._privacy, on_revoke_turn=self._approvals.revoke_turn
        )
        self._contexts: dict[str, _TurnContext] = {}
        self._context_order = 0
        # serializes the sensitive section of execute() (identity validation ->
        # atomic consume -> turn-state transition) across concurrent requests
        self._execute_lock = asyncio.Lock()

    # -- accessors ----------------------------------------------------------

    @property
    def sessions(self) -> LiveSessionManager:
        return self._sessions

    @property
    def approvals(self) -> ApprovalLedger:
        return self._approvals

    # -- session ------------------------------------------------------------

    def open_session(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        user_id: int,
        chat_id: int,
        privacy_mode: PrivacyMode = PrivacyMode.LOCAL_VAULT,
        consent_scope: str = "",
        current_revision: int = 0,
    ) -> str:
        session = self._sessions.open_session(
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            user_id=user_id,
            chat_id=chat_id,
            privacy_mode=privacy_mode,
            consent_scope=consent_scope,
            current_revision=current_revision,
        )
        return session.session_id

    # -- intent → AST → plan → preview → approval ----------------------------

    def submit_text_intent(
        self,
        session_id: str,
        turn_id: str,
        *,
        sequence_number: int,
        text: str,
        asset_id: str,
        source_path: str,
        base_approval_id: str | None = None,
    ) -> PipelineResult | Clarification | Refusal:
        session = self._sessions.get_session(session_id)
        revision = int(self._deps.revision_provider())
        self._sessions.begin_turn(
            session_id, turn_id, sequence_number=sequence_number, project_revision=revision
        )
        try:
            verified = self._resolve_authority(session.principal_id, session.project_id)
        except CanonicalAuthorityError as exc:
            # No trusted identity → typed refusal before job admission. Never a
            # synthetic binding, never broad service permissions (Phase 1).
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("unauthorized_principal", f"{exc.reason}: {exc.detail}")
        src = Path(source_path)
        if not src.is_file():
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("missing_source", f"source media not found: {source_path!r}")
        try:
            info, fps = probe_media(src, binary=self._deps.verifier_binary)
        except (RenderError, FootprintVerifyError) as exc:
            # an unreadable/corrupt source is a typed refusal, never a crash
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("unreadable_source", str(exc))
        duration_us = info.duration_us
        source_asset_digest = sha256_file(src)

        parsed: IntentAST | DeltaIntentAST | Clarification | Refusal
        base_scope = None
        base_binding = None
        base_ctx: _TurnContext | None = None
        parent_proposal_id: str | None = None
        parent_scope_digest: str | None = None
        if base_approval_id is not None:
            try:
                base_binding = self._approvals.get(base_approval_id)
                base_ctx = self._contexts[base_approval_id]
            except (ApprovalError, KeyError):
                self._sessions.mark_failed(session_id, turn_id)
                return Refusal("unknown_base_proposal", "Delta base proposal is unavailable")
            assert base_binding is not None and base_ctx is not None
            if (
                base_ctx.source_asset_digest != source_asset_digest
                or base_ctx.ast.asset_id != asset_id
            ):
                self._sessions.mark_failed(session_id, turn_id)
                return Refusal(
                    "delta_source_mismatch", "Delta must refine the exact base asset bytes"
                )
            if (
                base_binding.session_id != session_id
                or base_binding.principal_id != session.principal_id
                or base_binding.project_id != session.project_id
                or base_binding.source_revision != revision
                or base_binding.state is not ApprovalState.PENDING
            ):
                self._sessions.mark_failed(session_id, turn_id)
                return Refusal(
                    "stale_base_proposal", "Delta requires the current unapproved proposal"
                )
            base_scope = base_ctx.ast.scope
            parent_proposal_id = base_ctx.ast.ast_id
            parent_scope_digest = base_ctx.ast.scope.scope_digest
            parsed = parse_delta_intent(
                text,
                current_scope=base_scope,
                project_id=session.project_id,
                asset_id=asset_id,
                source_revision=revision,
                duration_us=duration_us,
                channel_binding_id=verified.channel_binding_id,
            )
        else:
            parsed = parse_intent(
                text,
                project_id=session.project_id,
                asset_id=asset_id,
                source_revision=revision,
                duration_us=duration_us,
                channel_binding_id=verified.channel_binding_id,
            )
        if not isinstance(parsed, (IntentAST, DeltaIntentAST)):
            # clarification / refusal: no approval, no execution, turn stays live
            return parsed
        ast = parsed.ast if isinstance(parsed, IntentAST) else parsed.resolved_ast
        if base_binding is not None and base_approval_id is not None:
            self._approvals.revoke(base_approval_id)
            self._contexts.pop(base_approval_id, None)
        try:
            plan = compile_ast(ast, registry=self._deps.registry)
        except CompilationRefused as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal(exc.reason, exc.detail)
        preview = build_preview(plan, ast, source_duration_us=duration_us, fps=fps)
        try:
            shadow_manifest = build_shadow_preview(
                src,
                ast.scope,
                plan.plan_digest,
                ffmpeg_bin=self._deps.verifier_binary,
                source_duration_us=duration_us,
                max_keyframes=3,
            )
            media_signature = compute_scope_signature(
                src, ast.scope, ffmpeg_bin=self._deps.verifier_binary, max_frames=3
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("media_evidence_unavailable", str(exc))
        readback = render_readback(ast, plan, preview)

        binding = ApprovalBinding(
            session_id=session_id,
            turn_id=turn_id,
            sequence_number=sequence_number,
            tenant_id=session.tenant_id,
            project_id=session.project_id,
            principal_id=session.principal_id,
            source_revision=revision,
            source_asset_digest=source_asset_digest,
            ast_digest=ast.ast_digest,
            plan_digest=plan.plan_digest,
            preview_digest=preview.preview_digest,
        )
        stored = self._approvals.create(binding)
        self._store_context(
            stored.approval_id,
            _TurnContext(
                ast=ast,
                plan=plan,
                preview=preview,
                readback=readback,
                intent_digest=parsed.intent_digest,
                source_path=src,
                source_asset_digest=source_asset_digest,
                duration_us=duration_us,
                fps=fps,
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=session.tenant_id,
                project_id=session.project_id,
                principal_id=session.principal_id,
                binding_ref=verified.channel_binding_id,
                verified_identity=verified,
                shadow_manifest=shadow_manifest,
                media_signature=media_signature,
                parent_proposal_id=parent_proposal_id,
                parent_scope_digest=parent_scope_digest,
            ),
        )
        self._sessions.mark_intent_parsed(session_id, turn_id, intent_digest=parsed.intent_digest)
        self._sessions.mark_plan_preview(session_id, turn_id)
        self._sessions.mark_awaiting_approval(session_id, turn_id, approval_id=stored.approval_id)
        return PipelineResult(
            turn_id=turn_id,
            approval_id=stored.approval_id,
            intent_digest=parsed.intent_digest,
            ast=ast,
            plan=plan,
            preview=preview,
            readback=readback,
            shadow_manifest=shadow_manifest,
            media_signature=media_signature,
        )

    async def submit_voice_intent(
        self,
        session_id: str,
        turn_id: str,
        *,
        sequence_number: int,
        audio: bytes,
        asset_id: str,
        source_path: str,
        audio_dir: Path | None = None,
    ) -> PipelineResult | Clarification | Refusal:
        """Voice turn: audio → local engine → FINAL transcript → same typed
        pipeline. The transcript is untrusted input; approval is still required.

        ``audio_dir`` defaults to a service-derived, session-scoped dir under
        the workspace root (the session id is internally generated); whatever
        dir is used, submit_audio enforces resolve-based containment inside it.
        """
        engine = self._deps.caption_engine
        if engine is None or not engine.is_available():
            return Refusal("caption_profile_unavailable", "no local caption engine configured")
        session = self._sessions.get_session(session_id)
        self._sessions.assert_local_processing(session_id, "audio")
        if audio_dir is None:
            audio_dir = self._deps.workspace_root / "audio" / session.session_id
        revision = int(self._deps.revision_provider())
        self._sessions.begin_turn(
            session_id, turn_id, sequence_number=sequence_number, project_revision=revision
        )
        try:
            turn = await self._sessions.submit_audio(
                session_id, turn_id, audio, engine=engine, audio_dir=audio_dir
            )
        except Exception as exc:  # noqa: BLE001 — typed failure, turn marked failed
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("transcription_failed", str(exc))
        transcript = self._sessions.transcript_text(session_id, turn.turn_id)
        # The parse/preview/approval body probes the media and hashes it with
        # synchronous ffmpeg calls; run it off the event loop.
        return await asyncio.to_thread(
            self._continue_with_text,
            session_id,
            turn.turn_id,
            transcript,
            asset_id=asset_id,
            source_path=source_path,
        )

    def _continue_with_text(
        self,
        session_id: str,
        turn_id: str,
        text: str,
        *,
        asset_id: str,
        source_path: str,
    ) -> PipelineResult | Clarification | Refusal:
        session = self._sessions.get_session(session_id)
        turn = self._sessions.get_turn(session_id, turn_id)
        try:
            verified = self._resolve_authority(session.principal_id, session.project_id)
        except CanonicalAuthorityError as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("unauthorized_principal", f"{exc.reason}: {exc.detail}")
        src = Path(source_path)
        if not src.is_file():
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("missing_source", f"source media not found: {source_path!r}")
        try:
            info, fps = probe_media(src, binary=self._deps.verifier_binary)
        except (RenderError, FootprintVerifyError) as exc:
            # an unreadable/corrupt source is a typed refusal, never a crash
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("unreadable_source", str(exc))
        parsed = parse_intent(
            text,
            project_id=session.project_id,
            asset_id=asset_id,
            source_revision=turn.project_revision,
            duration_us=info.duration_us,
            channel_binding_id=verified.channel_binding_id,
        )
        if not isinstance(parsed, IntentAST):
            return parsed
        ast = parsed.ast
        try:
            plan = compile_ast(ast, registry=self._deps.registry)
        except CompilationRefused as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal(exc.reason, exc.detail)
        preview = build_preview(plan, ast, source_duration_us=info.duration_us, fps=fps)
        readback = render_readback(ast, plan, preview)
        binding = ApprovalBinding(
            session_id=session_id,
            turn_id=turn_id,
            sequence_number=turn.sequence_number,
            tenant_id=session.tenant_id,
            project_id=session.project_id,
            principal_id=session.principal_id,
            source_revision=turn.project_revision,
            source_asset_digest=sha256_file(src),
            ast_digest=ast.ast_digest,
            plan_digest=plan.plan_digest,
            preview_digest=preview.preview_digest,
        )
        stored = self._approvals.create(binding)
        self._store_context(
            stored.approval_id,
            _TurnContext(
                ast=ast,
                plan=plan,
                preview=preview,
                readback=readback,
                intent_digest=parsed.intent_digest,
                source_path=src,
                source_asset_digest=sha256_file(src),
                duration_us=info.duration_us,
                fps=fps,
                session_id=session_id,
                turn_id=turn_id,
                tenant_id=session.tenant_id,
                project_id=session.project_id,
                principal_id=session.principal_id,
                binding_ref=verified.channel_binding_id,
                verified_identity=verified,
            ),
        )
        self._sessions.mark_intent_parsed(session_id, turn_id, intent_digest=parsed.intent_digest)
        self._sessions.mark_plan_preview(session_id, turn_id)
        self._sessions.mark_awaiting_approval(session_id, turn_id, approval_id=stored.approval_id)
        return PipelineResult(
            turn_id=turn_id,
            approval_id=stored.approval_id,
            intent_digest=parsed.intent_digest,
            ast=ast,
            plan=plan,
            preview=preview,
            readback=readback,
        )

    # -- canonical authority --------------------------------------------------

    def _resolve_authority(self, principal_id: str, project_id: str) -> Any:
        """Resolve + verify the acting principal from server state (Phase 1).

        The identity comes from the composition root (an injected
        :class:`CanonicalAuthorityPort`), never from a model proposal or an
        arbitrary user value.  Raises a typed
        :class:`CanonicalAuthorityError` — never a crash, never a synthetic
        grant — when the principal is unknown, is not a project member, or the
        binding does not belong to it.
        """
        authority = self._deps.authority
        if authority is None:
            raise CanonicalAuthorityError(
                "authority_unconfigured",
                "no trusted canonical authority is configured; refusing to execute",
            )
        binding_ref = self._deps.channel_binding_ref
        if not binding_ref:
            raise CanonicalAuthorityError(
                "missing_binding", "no canonical channel binding was configured for this service"
            )
        return authority.resolve_principal(
            principal_id=principal_id,
            project_id=project_id,
            binding_ref=binding_ref,
        )

    # -- approval -------------------------------------------------------------

    def _resolve_identity(
        self, session_id: str, turn_id: str, approval_id: str, *, principal_id: str
    ) -> tuple[Any, Any, _TurnContext, ApprovalIdentity]:
        """Fail-closed full identity resolution — the Phase-C core.

        Order (per mission §4.4): context → session/turn → binding → revision
        → real asset re-hash → digest match. Every step raises a typed error;
        nothing is trusted from the caller alone. Returns the session, turn,
        context and the full ApprovalIdentity for the ledger.
        """
        # 1) execution context must exist (a deleted/incomplete context fails
        #    closed with a typed error, never a KeyError/500)
        ctx = self._contexts.get(approval_id)
        if ctx is None:
            raise ApprovalError("unknown_context", f"no execution context for {approval_id!r}")
        # 2) session must exist (typed SessionError)
        session = self._sessions.get_session(session_id)
        # 3) turn must belong to THIS session and not be stale/terminal —
        #    session-level guards surface as a typed approval refusal
        try:
            turn = self._sessions.get_turn(session_id, turn_id)
        except (SessionError, StaleTurnError, TransitionError) as exc:
            raise ApprovalError("turn_unavailable", f"{type(exc).__name__}: {exc}") from exc
        # 4) binding must exist (typed ApprovalError)
        binding = self._approvals.get(approval_id)
        # 5) the binding must belong to this exact session/turn/tenant/project/
        #    principal/sequence — cross-anything use is refused here
        if binding.session_id != session.session_id:
            raise ApprovalError(
                "identity_mismatch_session_id", "binding belongs to another session"
            )
        if binding.turn_id != turn.turn_id:
            raise ApprovalError("identity_mismatch_turn_id", "binding belongs to another turn")
        if binding.sequence_number != turn.sequence_number:
            raise ApprovalError(
                "identity_mismatch_sequence_number", "sequence does not match the turn"
            )
        if binding.tenant_id != session.tenant_id:
            raise ApprovalError("identity_mismatch_tenant_id", "binding belongs to another tenant")
        if binding.project_id != session.project_id:
            raise ApprovalError(
                "identity_mismatch_project_id", "binding belongs to another project"
            )
        if binding.principal_id != principal_id or session.principal_id != principal_id:
            raise ApprovalError("wrong_principal", "request principal differs from session/binding")
        if ctx.session_id != session.session_id or ctx.turn_id != turn.turn_id:
            raise ApprovalError(
                "context_mismatch", "execution context does not match this session/turn"
            )
        # 5b) the trusted canonical authority must still verify at execution
        #     time (Phase 1): the principal is a member of the project and the
        #     binding belongs to it.  Authority revocation fails closed.
        try:
            verified = self._resolve_authority(session.principal_id, session.project_id)
        except CanonicalAuthorityError as exc:
            raise ApprovalError("authority_unverified", f"{exc.reason}: {exc.detail}") from exc
        if ctx.binding_ref != verified.channel_binding_id:
            raise ApprovalError(
                "binding_mismatch", "binding differs from the context the approval was issued in"
            )
        # 6) revision must still be current
        revision = int(self._deps.revision_provider())
        if binding.source_revision != revision:
            raise ApprovalError(
                "stale_revision",
                f"binding bound to revision {binding.source_revision}, current {revision}",
            )
        # 7) the REAL asset is re-hashed now — a changed file can never execute
        try:
            actual_asset = sha256_file(ctx.source_path)
        except OSError as exc:
            raise ApprovalError("asset_unreadable", str(exc)) from exc
        if actual_asset != binding.source_asset_digest:
            raise ApprovalError("asset_changed", "source asset changed after the preview")
        # 8) context digests must match the binding (AST/plan/preview tamper)
        if ctx.ast.ast_digest != binding.ast_digest:
            raise ApprovalError("ast_mismatch", "AST digest differs from the binding")
        if ctx.plan.plan_digest != binding.plan_digest:
            raise ApprovalError("plan_mismatch", "plan digest differs from the binding")
        if ctx.preview.preview_digest != binding.preview_digest:
            raise ApprovalError("preview_tampered", "preview digest differs from the binding")
        identity = ApprovalIdentity(
            principal_id=principal_id,
            tenant_id=session.tenant_id,
            project_id=session.project_id,
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence_number=turn.sequence_number,
            source_revision=revision,
            source_asset_digest=actual_asset,
            ast_digest=ctx.ast.ast_digest,
            plan_digest=ctx.plan.plan_digest,
            preview_digest=ctx.preview.preview_digest,
        )
        return session, turn, ctx, identity

    @staticmethod
    def _scope_signature(ctx: _TurnContext, identity: ApprovalIdentity) -> str:
        """Sign the canonical approval scope using the frozen IBPC digest form."""
        return canonical_digest(
            {
                "identity": identity.as_dict(),
                "intent_id": ctx.ast.ast_id,
                "normalized_plan": ctx.plan.model_dump(mode="json"),
                "target_asset_refs": [ctx.ast.asset_id, ctx.source_asset_digest],
                "capability_set": [CANONICAL_OPERATION],
                "parameter_digest": digest_of(ctx.ast.params),
                "metadata_preview_digest": ctx.preview.preview_digest,
                "shadow_manifest": ctx.shadow_manifest.model_dump(mode="json")
                if ctx.shadow_manifest
                else None,
                "media_signature": ctx.media_signature.model_dump(mode="json")
                if ctx.media_signature
                else None,
                "proposal_id": ctx.ast.ast_id,
                "proposal_revision": ctx.ast.source_revision,
                "parent_proposal_id": ctx.parent_proposal_id,
                "parent_scope_digest": ctx.parent_scope_digest,
                "source_asset": {
                    "asset_id": ctx.ast.asset_id,
                    "sha256": ctx.source_asset_digest,
                    "byte_length": ctx.source_path.stat().st_size,
                },
            },
            CanonicalizationScheme.JCS_RFC8785_V2,
        )

    def approve(
        self, session_id: str, turn_id: str, approval_id: str, *, principal_id: str
    ) -> ApprovalBinding:
        """Approve only in the exact context the binding was issued in.

        Raises typed ApprovalError/SessionError (fail-closed) on unknown
        session/turn/approval/context, any identity mismatch, stale revision,
        changed asset or tampered digests. Never changes another turn's state.
        """
        _session, _turn, ctx, identity = self._resolve_identity(
            session_id, turn_id, approval_id, principal_id=principal_id
        )
        provider = self._deps.approval_key_provider
        if provider is None:
            raise ApprovalError(
                "approval_key_unavailable", "approval authentication is not configured"
            )
        try:
            provider.key(provider.active_key_id)
        except (KeyProviderError, AttributeError) as exc:
            raise ApprovalError(
                "approval_key_unavailable", "configured approval key is invalid"
            ) from exc
        binding = self._approvals.approve(
            approval_id,
            identity=identity,
            turn_is_active=lambda: self._sessions.turn_is_active(session_id, turn_id),
        )
        ctx.scope_signature = self._scope_signature(ctx, identity)
        try:
            issued_at = self._deps.now()
            issue_dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
            if issue_dt.tzinfo is None:
                raise ValueError("approval issue time must be timezone-aware")
            expires_at = (issue_dt + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ctx.approval_envelope = issue_envelope(
                provider,
                approval_id=approval_id,
                payload={"scope_digest": ctx.scope_signature},
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except (KeyProviderError, TypeError, ValueError) as exc:
            self._approvals.revoke(approval_id)
            raise ApprovalError(
                "approval_key_unavailable", "approval envelope could not be issued"
            ) from exc
        self._sessions.mark_approved(session_id, turn_id)
        return binding

    # -- fenced execution ------------------------------------------------------

    def _guarded_workspace(self, approval_id: str) -> Path:
        root = self._deps.workspace_root.resolve(strict=False)
        workspace = (root / f"{WORKSPACE_PREFIX}ibpc_{approval_id[:16]}").resolve(strict=False)
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise SessionError("workspace_escape", str(exc)) from exc
        if not workspace.name.startswith(WORKSPACE_PREFIX):
            raise SessionError("workspace_prefix", workspace.name)
        return workspace

    def _stage_source(self, source: Path, workspace: Path) -> Path:
        """Copy the admitted source into a private, no-follow workspace file.

        The source is opened with ``O_RDONLY | O_NOFOLLOW`` (a symlinked/replaced
        source is refused) and the workspace directory is opened with
        ``O_DIRECTORY | O_NOFOLLOW`` so the *destination* cannot be a symlink
        redirect. The bytes are read from that already-validated descriptor, so a
        rename/symlink swap of the path *after* the open cannot change what is
        copied — the descriptor pins the exact admitted inode."""
        workspace.mkdir(parents=True, exist_ok=True)
        destination = workspace / "input.mp4"
        src_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            src_fd = os.open(source, src_flags)
        except OSError as exc:
            raise SessionError("source_unreadable", f"{source}: {exc}") from exc
        try:
            dst_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(src_fd, "rb", closefd=True) as src_file:
                    src_fd = -1  # ownership transferred to src_file
                    with os.fdopen(dst_fd, "wb", closefd=True) as dst_file:
                        dst_fd = -1
                        shutil.copyfileobj(src_file, dst_file)
                        dst_file.flush()
                        os.fsync(dst_file.fileno())
            finally:
                for fd in (src_fd, dst_fd):
                    if fd >= 0:
                        os.close(fd)
        except OSError as exc:
            raise SessionError("staging_failed", f"{destination}: {exc}") from exc
        return destination

    def _sha256_fd(self, path: Path) -> str:
        """Stream a file's digest without following a final symlink."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return "sha256:" + digest.hexdigest()
        finally:
            if fd >= 0:
                os.close(fd)

    def _store_context(self, approval_id: str, ctx: _TurnContext) -> None:
        """Insert a context and, when the store is bounded, evict the oldest
        *terminal* contexts so the in-process store cannot grow without bound.

        A context is evictable only when its approval is CONSUMED (execution
        finished) or REVOKED (cancel/interrupt/stale). PENDING/APPROVED contexts
        are never evicted, so an active or awaiting execution can always resolve
        its context; a replay of a consumed approval is refused by the ledger
        before any context lookup, so eviction cannot re-enable replay."""
        ctx.created_order = self._context_order
        self._context_order += 1
        self._contexts[approval_id] = ctx
        limit = self._deps.max_contexts
        if limit is None or len(self._contexts) <= limit:
            return
        evictable = sorted(
            (
                (c.created_order, aid)
                for aid, c in self._contexts.items()
                if self._is_terminal_context(aid)
            ),
        )
        overflow = len(self._contexts) - limit
        for _, aid in evictable[:overflow]:
            del self._contexts[aid]

    def _is_terminal_context(self, approval_id: str) -> bool:
        try:
            binding = self._approvals.get(approval_id)
        except ApprovalError:
            return True  # no live binding → nothing can resolve it → evictable
        return binding.state in (ApprovalState.CONSUMED, ApprovalState.REVOKED)

    def _verify_artifact(
        self, ctx: _TurnContext, artifact_path: Path, input_path: Path
    ) -> tuple[str, Any]:
        """Hash the artifact and run the independent verifier. Synchronous and
        ffmpeg-backed, so callers run it via ``asyncio.to_thread``.

        Verification reads the **staged, immutable input copy** (never the
        mutable origin path), so a filesystem swap of the source after admission
        cannot change the evidence the verifier is derived from."""
        artifact_digest = sha256_file(artifact_path)
        report = verify_keep(
            input_path,
            artifact_path,
            scope=ctx.ast.scope,
            binary=self._deps.verifier_binary,
        )
        return artifact_digest, report

    @staticmethod
    def _seconds_str(microseconds: int) -> str:
        return f"{microseconds / 1_000_000:.6f}".rstrip("0").rstrip(".") or "0"

    def _canonical_intent(self, ctx: _TurnContext) -> CanonicalCreativeIntent:
        """Build the schema-2 canonical intent with the SAME builder the worker
        uses (``application.creative_intent``), over the exact trim input the
        worker re-derives from ``args``.  The worker's divergence check then
        compares two independently constructed records; a mismatch fails closed
        before any media work."""
        builder = self._deps.intent_builder or CanonicalCreativeIntent.create
        in_us = int(ctx.ast.params["in_point_us"])
        out_us = int(ctx.ast.params["out_point_us"])
        input_data = {
            "clip_asset_id": ctx.ast.asset_id,
            "in_point_us": in_us,
            "out_point_us": out_us,
        }
        proposal_json = json.dumps(
            input_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        proposal_id = "proposal_" + hashlib.sha256(proposal_json.encode("utf-8")).hexdigest()
        source_text_sha256 = (
            "sha256:" + hashlib.sha256(ctx.intent_digest.encode("utf-8")).hexdigest()
        )
        return builder(
            project_id=ctx.project_id,
            proposal_id=proposal_id,
            source_text_sha256=source_text_sha256,
            operation=CANONICAL_OPERATION,
            input_data=input_data,
        )

    async def execute(
        self, session_id: str, turn_id: str, approval_id: str, *, principal_id: str
    ) -> ExecutionOutcome:
        """Re-validate every binding, consume the approval once, then enqueue
        exactly one canonical job through the injected queue port."""
        # Sensitive section, serialized: Context Validation → Session/Turn
        # Validation → Binding Validation → Atomic Consume → state transition.
        # A wrong or cross-context request is refused BEFORE the consume, so it
        # can never burn a valid approval; the consume itself is the atomic
        # compare-and-set under the ledger lock (concurrent double-consume
        # yields exactly one winner, the rest are typed replay refusals).
        async with self._execute_lock:
            try:
                # _resolve_identity re-hashes the source asset (synchronous I/O)
                # and the ledger/context stores use threading locks, so it runs
                # off the event loop.
                session, turn, ctx, identity = await asyncio.to_thread(
                    self._resolve_identity,
                    session_id,
                    turn_id,
                    approval_id,
                    principal_id=principal_id,
                )
            except (ApprovalError, SessionError) as exc:
                return ExecutionOutcome(status="refused", detail=f"{exc.reason}: {exc.detail}")
            if ctx.scope_signature is None or not hmac.compare_digest(
                ctx.scope_signature, self._scope_signature(ctx, identity)
            ):
                error = ScopeSignatureMismatchError()
                return ExecutionOutcome(status="refused", detail=f"{error.reason}: {error.detail}")
            provider = self._deps.approval_key_provider
            envelope = ctx.approval_envelope
            if (
                provider is None
                or envelope is None
                or not verify_envelope(
                    provider, envelope, {"scope_digest": ctx.scope_signature}, now=self._deps.now()
                )
            ):
                return ExecutionOutcome(
                    status="refused", detail="approval_mac_invalid: envelope verification failed"
                )
            try:
                self._approvals.consume(approval_id, identity=identity)
            except ApprovalError as exc:
                return ExecutionOutcome(status="refused", detail=f"{exc.reason}: {exc.detail}")
            if turn.turn_state not in (TurnState.APPROVED, TurnState.EXECUTING):
                return ExecutionOutcome(
                    status="refused",
                    detail=f"turn state {turn.turn_state.value} cannot execute "
                    "(cancelled/interrupted/partial/stale turns never execute)",
                )
            # Reservation is inside the lock: cancel()/interrupt() refuse while a
            # turn is EXECUTING, so no cancel can land between this transition and
            # the state read-back below.  Without that, a cancel racing the
            # (lock-free) staging/enqueue window could terminalise the turn and a
            # later mark_failed would raise an uncaught TransitionError.
            self._sessions.mark_executing(session_id, turn_id)
            # Pure read-back under the lock: the authoritative state at reservation.
            reserved = self._sessions.get_session(session_id).turns.get(turn_id)
            if reserved is None or reserved.turn_state is not TurnState.EXECUTING:
                return ExecutionOutcome(
                    status="refused", detail="turn is no longer in an executing state"
                )
        workspace = self._guarded_workspace(approval_id)
        # staging copies the source file (synchronous I/O) — keep it off the loop
        try:
            input_path = await asyncio.to_thread(self._stage_source, ctx.source_path, workspace)
        except SessionError as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return ExecutionOutcome(status="refused", detail=f"{exc.reason}: {exc.detail}")
        # The staged copy must equal the admitted content identity. A concurrent
        # rename/symlink swap of the source between admission and staging would
        # otherwise let the worker render bytes the approval never covered.
        staged_digest = await asyncio.to_thread(self._sha256_fd, input_path)
        if staged_digest != ctx.source_asset_digest:
            self._sessions.mark_failed(session_id, turn_id)
            return ExecutionOutcome(
                status="refused",
                detail="source_changed_during_staging: staged content does not match the "
                "admitted asset digest",
            )
        in_us = int(ctx.ast.params["in_point_us"])
        out_us = int(ctx.ast.params["out_point_us"])
        idempotency_key = f"ibpc:{approval_id}"
        payload = {
            "command": COMMAND,
            "operation": SURFACE_OPERATION,
            "args": [self._seconds_str(in_us), self._seconds_str(out_us)],
            "workspace_dir": str(workspace),
            "input_path": str(input_path),
            # Bind the *staged* bytes' content identity to the enqueued job. The
            # worker (task-nagar-workspace-redelivery-01) re-measures the staged
            # input and refuses a media-bearing payload that carries no digest, so
            # the digest staged_digest (already checked == ctx.source_asset_digest
            # above) is the admitted identity the worker must reproduce.
            "input_sha256": staged_digest,
            "media_duration_us": int(ctx.duration_us),
            "user_id": session.user_id,
            "chat_id": session.chat_id,
            "lang": "en",
            "idempotency_key": idempotency_key,
            # Phase 1: emit the canonical schema-2 identity so the existing
            # worker independently re-authorizes (SQLApplicationDirectory) and
            # records real authorization evidence. The intent is built with the
            # SAME canonical builder the worker's own code path uses, so a
            # worker-side divergence check compares like with like.
            "schema_version": 2,
            "principal_id": ctx.verified_identity.principal_id,
            "project_id": ctx.verified_identity.project_id,
            "channel_binding_id": ctx.verified_identity.channel_binding_id,
            "intent": self._canonical_intent(ctx).model_dump(mode="json"),
        }
        try:
            job_id = await self._deps.queue.enqueue(
                job_type=JOB_TYPE, idempotency_key=idempotency_key, payload=payload
            )
        except Exception as exc:  # noqa: BLE001 — enqueue failure is a refusal, not a render claim
            self._sessions.mark_failed(session_id, turn_id)
            return ExecutionOutcome(status="refused", detail=f"enqueue_failed: {exc}")

        status = await self._drain(job_id)
        result = await self._deps.queue.get_result(job_id)
        if status is not JobStatus.COMPLETED or not (result and result.get("success") is True):
            self._sessions.mark_failed(session_id, turn_id)
            return ExecutionOutcome(
                status="failed",
                job_id=job_id,
                detail=f"job {status.value}: {str(result)[:400]}",
            )

        artifact_path = Path(str(result["artifact_path"]))
        # Re-measure the artifact and run the independent verifier off the event
        # loop: both are synchronous and shell out to ffmpeg.
        artifact_digest, report = await asyncio.to_thread(
            self._verify_artifact, ctx, artifact_path, input_path
        )
        attempt_identity = await self._attempt_identity(job_id)
        provenance_reference = await asyncio.to_thread(self._provenance_reference, job_id)
        # Phase 2: rendering completion is not proof. The proof gate decides
        # whether the evidence is complete enough to *claim* verified success.
        gate = evaluate_proof_gate(
            verdict=report.verdict,
            attempt_identity=attempt_identity,
            provenance_reference=provenance_reference,
        )
        capsule = IBPCCapsule(
            tenant_id=session.tenant_id,
            project_id=session.project_id,
            asset_id=ctx.ast.asset_id,
            source_revision=turn.project_revision,
            source_asset_digest=ctx.source_asset_digest,
            intent_digest=ctx.intent_digest,
            typed_ast_digest=ctx.ast.ast_digest,
            scope_digest=ctx.ast.scope.scope_digest,
            capability_identity=ctx.plan.capability.identity_digest,
            capability_schema_digest=ctx.plan.capability.schema_digest,
            compiled_plan_digest=ctx.plan.plan_digest,
            preview_digest=ctx.preview.preview_digest,
            approval_id=approval_id,
            approval_binding=self._approvals.get(approval_id).identity_digest,
            principal_identity=session.principal_id,
            channel_binding_id=ctx.binding_ref,
            session_id=session_id,
            turn_id=turn_id,
            execution_attempt_identity=attempt_identity,
            output_artifact_digest=artifact_digest,
            verifier_report_digest=report.report_digest,
            verifier_verdict=report.verdict,
            proof_status="verified" if gate.proof_complete else "unverified",
            proof_gaps=gate.reasons,
            evidence_references=(
                f"artifact:{artifact_digest}",
                f"verifier_report:{report.report_digest}",
                f"approval:{approval_id}",
            ),
            provenance_reference=provenance_reference,
            created_at=self._deps.now(),
        )
        self._sessions.mark_executed(session_id, turn_id)
        detail = "" if gate.proof_complete else "proof_incomplete: " + ",".join(gate.reasons)
        return ExecutionOutcome(
            status=gate.outcome_status,
            job_id=job_id,
            capsule=capsule,
            report=report,
            detail=detail,
        )

    async def _drain(self, job_id: str, timeout: float = DRAIN_TIMEOUT_S) -> JobStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
        while loop.time() < deadline:
            status = await self._deps.queue.get_status(job_id)
            if status in terminal:
                return status
            await asyncio.sleep(0.05)
        return await self._deps.queue.get_status(job_id)

    async def _attempt_identity(self, job_id: str) -> str:
        if self._deps.attempt_provider is not None:
            try:
                import inspect

                history = self._deps.attempt_provider(job_id)
                if inspect.isawaitable(history):
                    history = await history
                history = history or []
                attempts: list[int] = []
                for h in history:
                    if isinstance(h, dict) and isinstance(h.get("attempt"), int):
                        attempts.append(h["attempt"])
                if attempts:
                    return f"{job_id}:attempt{max(attempts)}"
            except Exception:  # noqa: BLE001 — identity degrades to the row id, never fabricated
                pass
        return f"{job_id}:attempt?"

    def _provenance_reference(self, job_id: str) -> dict[str, Any]:
        """Reference the EXISTING causal journal + passport — no parallel store."""
        from nexus_ai_agent.provenance.models import verify_chain

        reference: dict[str, Any] = {"job_id": job_id, "journal": None, "passport": None}
        journal = self._deps.journal
        if journal is not None:
            try:
                # Per-job records are the evidence; integrity is verified against
                # the WHOLE chain.  A per-job subset is not itself a gapless chain
                # (its seqs interleave with other jobs), so verifying the subset
                # with ``verify_chain`` would falsely report "unverified" for any
                # job that is not first in the journal.  The empty set, by
                # contrast, verifies trivially — hence record_count is carried
                # explicitly and the proof gate requires it to be positive.
                records = journal.records_for_job(job_id)
                global_chain = verify_chain(journal.all_records())
                reference["journal"] = {
                    "record_count": len(records),
                    "head_hash": records[-1].record_hash if records else None,
                    "chain_verified": global_chain.ok,
                    "first_broken_seq": global_chain.first_broken_seq,
                }
            except Exception as exc:  # noqa: BLE001 — evidence degrades honestly
                reference["journal"] = {"error": f"{type(exc).__name__}: {exc}"}
        provider = self._deps.facts_provider
        if journal is not None and provider is not None:
            try:
                from nexus_ai_agent.provenance.passport import PassportBuilder

                passport = PassportBuilder(journal, provider).build(job_id)
                reference["passport"] = passport.to_dict()
            except Exception as exc:  # noqa: BLE001
                reference["passport"] = {"error": f"{type(exc).__name__}: {exc}"}
        return reference


__all__ = [
    "CANONICAL_OPERATION",
    "COMMAND",
    "ExecutionOutcome",
    "IBPCService",
    "IBPCServiceDeps",
    "JOB_TYPE",
    "PipelineResult",
    "SURFACE_OPERATION",
    "utc_now",
]
