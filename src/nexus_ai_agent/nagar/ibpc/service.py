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
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nexus_ai_agent.application.ports.caption_engine import CaptionEnginePort
from nexus_ai_agent.application.ports.job_queue import JobQueuePort, JobStatus
from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalLedger
from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused, compile_ast
from nexus_ai_agent.nagar.ibpc.footprint import probe_media, verify_keep
from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    IntentAST,
    Refusal,
    parse_intent,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalBinding,
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
from nexus_ai_agent.nagar.ibpc.session import (
    LiveSessionManager,
    SessionError,
    TurnState,
)

#: Closed surface constants. Pinned against the canonical worker's own map by
#: tests/architecture/test_ibpc_boundary.py (which may import render_jobs; this
#: module may not): SURFACE_TO_CANONICAL[("edit", "trim")] == "timeline.trim".
JOB_TYPE = "creative_render"
COMMAND = "edit"
SURFACE_OPERATION = "trim"
CANONICAL_OPERATION = "timeline.trim"
WORKSPACE_PREFIX = "creative_"
DRAIN_TIMEOUT_S = 300.0


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
    ) -> PipelineResult | Clarification | Refusal:
        session = self._sessions.get_session(session_id)
        revision = int(self._deps.revision_provider())
        self._sessions.begin_turn(
            session_id, turn_id, sequence_number=sequence_number, project_revision=revision
        )
        src = Path(source_path)
        if not src.is_file():
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("missing_source", f"source media not found: {source_path!r}")
        info, fps = probe_media(src, binary=self._deps.verifier_binary)
        duration_us = info.duration_us
        source_asset_digest = sha256_file(src)

        parsed = parse_intent(
            text,
            project_id=session.project_id,
            asset_id=asset_id,
            source_revision=revision,
            duration_us=duration_us,
        )
        if not isinstance(parsed, IntentAST):
            # clarification / refusal: no approval, no execution, turn stays live
            return parsed
        ast = parsed.ast
        try:
            plan = compile_ast(ast, registry=self._deps.registry)
        except CompilationRefused as exc:
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal(exc.reason, exc.detail)
        preview = build_preview(plan, ast, source_duration_us=duration_us, fps=fps)
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
        self._contexts[stored.approval_id] = _TurnContext(
            ast=ast,
            plan=plan,
            preview=preview,
            readback=readback,
            intent_digest=parsed.intent_digest,
            source_path=src,
            source_asset_digest=source_asset_digest,
            duration_us=duration_us,
            fps=fps,
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

    async def submit_voice_intent(
        self,
        session_id: str,
        turn_id: str,
        *,
        sequence_number: int,
        audio: bytes,
        asset_id: str,
        source_path: str,
        audio_dir: Path,
    ) -> PipelineResult | Clarification | Refusal:
        """Voice turn: audio → local engine → FINAL transcript → same typed
        pipeline. The transcript is untrusted input; approval is still required."""
        engine = self._deps.caption_engine
        if engine is None or not engine.is_available():
            return Refusal("caption_profile_unavailable", "no local caption engine configured")
        self._sessions.get_session(session_id)
        self._sessions.assert_local_processing(session_id, "audio")
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
        return self._continue_with_text(
            session_id, turn.turn_id, transcript, asset_id=asset_id, source_path=source_path
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
        src = Path(source_path)
        if not src.is_file():
            self._sessions.mark_failed(session_id, turn_id)
            return Refusal("missing_source", f"source media not found: {source_path!r}")
        info, fps = probe_media(src, binary=self._deps.verifier_binary)
        parsed = parse_intent(
            text,
            project_id=session.project_id,
            asset_id=asset_id,
            source_revision=turn.project_revision,
            duration_us=info.duration_us,
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
        self._contexts[stored.approval_id] = _TurnContext(
            ast=ast,
            plan=plan,
            preview=preview,
            readback=readback,
            intent_digest=parsed.intent_digest,
            source_path=src,
            source_asset_digest=sha256_file(src),
            duration_us=info.duration_us,
            fps=fps,
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

    # -- approval -------------------------------------------------------------

    def approve(
        self, session_id: str, turn_id: str, approval_id: str, *, principal_id: str
    ) -> ApprovalBinding:
        self._sessions.get_session(session_id)
        ctx = self._contexts[approval_id]
        binding = self._approvals.approve(
            approval_id,
            principal_id=principal_id,
            source_revision=int(self._deps.revision_provider()),
            ast_digest=ctx.ast.ast_digest,
            plan_digest=ctx.plan.plan_digest,
            preview_digest=ctx.preview.preview_digest,
            source_asset_digest=ctx.source_asset_digest,
            turn_is_active=lambda: self._sessions.turn_is_active(session_id, turn_id),
        )
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
        workspace.mkdir(parents=True, exist_ok=True)
        destination = workspace / "input.mp4"
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        return destination

    @staticmethod
    def _seconds_str(microseconds: int) -> str:
        return f"{microseconds / 1_000_000:.6f}".rstrip("0").rstrip(".") or "0"

    async def execute(
        self, session_id: str, turn_id: str, approval_id: str, *, principal_id: str
    ) -> ExecutionOutcome:
        """Re-validate every binding, consume the approval once, then enqueue
        exactly one canonical job through the injected queue port."""
        session = self._sessions.get_session(session_id)
        if session.principal_id != principal_id:
            return ExecutionOutcome(status="refused", detail="wrong principal")
        try:
            turn = self._sessions.get_turn(session_id, turn_id)  # refuses stale turns
        except SessionError as exc:
            return ExecutionOutcome(status="refused", detail=f"{exc.reason}: {exc.detail}")
        ctx = self._contexts[approval_id]
        revision = int(self._deps.revision_provider())
        try:
            # single-use consume FIRST: replay/stale/tamper/revoked all refused here
            self._approvals.consume(
                approval_id,
                principal_id=principal_id,
                source_revision=revision,
                ast_digest=ctx.ast.ast_digest,
                plan_digest=ctx.plan.plan_digest,
                preview_digest=ctx.preview.preview_digest,
                source_asset_digest=ctx.source_asset_digest,
            )
        except ApprovalError as exc:
            return ExecutionOutcome(status="refused", detail=f"{exc.reason}: {exc.detail}")
        if turn.turn_state not in (TurnState.APPROVED, TurnState.EXECUTING):
            return ExecutionOutcome(
                status="refused",
                detail=f"turn state {turn.turn_state.value} cannot execute "
                "(cancelled/interrupted/partial/stale turns never execute)",
            )

        self._sessions.mark_executing(session_id, turn_id)
        workspace = self._guarded_workspace(approval_id)
        input_path = self._stage_source(ctx.source_path, workspace)
        in_us = int(ctx.ast.params["in_point_us"])
        out_us = int(ctx.ast.params["out_point_us"])
        idempotency_key = f"ibpc:{approval_id}"
        payload = {
            "command": COMMAND,
            "operation": SURFACE_OPERATION,
            "args": [self._seconds_str(in_us), self._seconds_str(out_us)],
            "workspace_dir": str(workspace),
            "input_path": str(input_path),
            "media_duration_us": int(ctx.duration_us),
            "user_id": session.user_id,
            "chat_id": session.chat_id,
            "lang": "en",
            "idempotency_key": idempotency_key,
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
        # re-measure the artifact independently of the renderer's claim
        artifact_digest = sha256_file(artifact_path)
        report = verify_keep(
            ctx.source_path,
            artifact_path,
            scope=ctx.ast.scope,
            binary=self._deps.verifier_binary,
        )
        attempt_identity = await self._attempt_identity(job_id)
        provenance_reference = self._provenance_reference(job_id)
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
            session_id=session_id,
            turn_id=turn_id,
            execution_attempt_identity=attempt_identity,
            output_artifact_digest=artifact_digest,
            verifier_report_digest=report.report_digest,
            verifier_verdict=report.verdict,
            evidence_references=(
                f"artifact:{artifact_digest}",
                f"verifier_report:{report.report_digest}",
                f"approval:{approval_id}",
            ),
            provenance_reference=provenance_reference,
            created_at=self._deps.now(),
        )
        self._sessions.mark_executed(session_id, turn_id)
        return ExecutionOutcome(
            status="executed", job_id=job_id, capsule=capsule, report=report, detail=""
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
                records = journal.records_for_job(job_id)
                chain = verify_chain(records)
                reference["journal"] = {
                    "record_count": len(records),
                    "head_hash": chain.head_hash,
                    "chain_verified": chain.ok,
                    "first_broken_seq": chain.first_broken_seq,
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
