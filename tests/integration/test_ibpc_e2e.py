"""End-to-end: IBPC live-editing slice over the REAL canonical chain (M2–M5).

No mocks on the execution path: a real FFmpeg-generated clip, the real
``InProcessJobQueue`` with its causal-ledger observer + journal, the real
``default_job_handlers`` (``creative_render`` → CommandBus → registry →
trim lane → measured artifact → queue verifier), and the real footprint
verifier on the decoded artifact. The only declared test doubles are the
revision provider (a counter) and, in the voice test, the caption engine
(the external model seam — transcription is never part of the execution path).

Proven by these tests:

* text intent → typed AST → registry-validated plan → plan-derived preview →
  readback → single-use approval → ONE canonical job → real artifact →
  independent content verification → IBPC capsule referencing the existing
  causal journal + passport;
* replay of a consumed approval is refused;
* ambiguous/unsupported intents clarify/refuse and enqueue NOTHING;
* stale/cancelled turns can never execute;
* a tampered artifact fails independent verification.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.sql_application_directory import (
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.application.principal import ChannelBinding
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.authority import DirectoryAuthority
from nexus_ai_agent.nagar.ibpc.footprint import verify_keep
from nexus_ai_agent.nagar.ibpc.intent import Clarification, Refusal
from nexus_ai_agent.nagar.ibpc.models import ApprovalState, CheckVerdict
from nexus_ai_agent.nagar.ibpc.service import (
    IBPCService,
    IBPCServiceDeps,
    PipelineResult,
)
from nexus_ai_agent.nagar.ibpc.session import StaleTurnError
from nexus_ai_agent.provenance.observer import QueueLedgerObserver
from nexus_ai_agent.provenance.paths import causal_journal_db_path, try_open_causal_journal
from nexus_ai_agent.storage.migrations import ensure_startup_schema
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "ibpc-e2e"
REVISION = 7
#: the lane renders at LaneProfile defaults — the source matches so the
#: footprint verifier can compare decoded frames 1:1
SIZE = "1280x720"
RATE = 30
SECONDS = 2.0


def _clip(path: Path, *, seconds: float | None = None) -> None:
    duration = SECONDS if seconds is None else seconds
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")
    proc = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={SIZE}:rate={RATE}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not synthesize clip: {proc.stderr.decode('utf-8', 'replace')[-200:]}")


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    creative_root = tmp_path / "creative_tmp"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    ensure_startup_schema()
    # Phase 1: a REAL durable directory is the trusted canonical authority; the
    # service resolves + verifies the acting principal from server state, and
    # the canonical worker independently re-authorizes the schema-2 payload.
    directory = SQLApplicationDirectory()
    binding = ChannelBinding("cli", "e2e-owner")
    principal = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT_ID, principal)
    queue_db = tmp_path / "jobs.sqlite3"
    journal = try_open_causal_journal(causal_journal_db_path(queue_db))
    observer = QueueLedgerObserver(journal) if journal is not None else None
    queue = InProcessJobQueue(queue_db, causal_observer=observer)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,
            registry=build_runtime_registry(),
            workspace_root=creative_root,
            revision_provider=lambda: REVISION,
            journal=journal,
            facts_provider=queue,
            attempt_provider=queue.get_attempt_history,
            authority=DirectoryAuthority(directory),
            channel_binding_ref=channel_binding_id(binding),
        )
    )
    yield service, queue, creative_root, journal, str(principal)
    settings_module.get_settings.cache_clear()


def _open_session(service: IBPCService, principal_id: str) -> str:
    return service.open_session(
        tenant_id="ten-1",
        project_id=PROJECT_ID,
        principal_id=principal_id,
        user_id=42,
        chat_id=4242,
        current_revision=REVISION,
    )


@pytest.mark.asyncio
async def test_scope_signature_tamper_refuses_before_queue_admission(
    harness, tmp_path: Path
) -> None:
    service, queue, _creative_root, _journal, principal = harness
    source = tmp_path / "tamper-source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "tamper-turn",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "tamper-turn", result.approval_id, principal_id=principal)
    context = service._contexts[result.approval_id]
    assert context.scope_signature is not None
    context.scope_signature = "sha256:" + "0" * 64

    outcome = await service.execute(sid, "tamper-turn", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert "scope_signature_mismatch" in outcome.detail
    assert queue.job_ids() == []


@pytest.mark.asyncio
async def test_full_chain_text_intent_to_verified_capsule(harness, tmp_path: Path) -> None:
    service, queue, creative_root, journal, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)

    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    assert result.ast.operation == "timeline.trim"
    assert "0.500s..1.500s" in result.readback.text
    assert "timeline.trim" in result.readback.text
    assert result.preview.output_duration_us == 1_000_000
    assert service.approvals.get(result.approval_id).state is ApprovalState.PENDING
    # nothing was enqueued before approval
    assert queue.job_ids() == []

    # wrong principal cannot approve
    with pytest.raises(ApprovalError):
        service.approve(sid, "turn-1", result.approval_id, principal_id="mallory")
    binding = service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    assert binding.state is ApprovalState.APPROVED

    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
    assert outcome.job_id is not None
    assert queue.job_ids() == [outcome.job_id]  # exactly one durable row

    job_result = await queue.get_result(outcome.job_id)
    assert job_result is not None and job_result["success"] is True
    assert job_result["operation"] == "timeline.trim"
    status = await queue.get_status(outcome.job_id)
    assert status is JobStatus.COMPLETED

    capsule = outcome.capsule
    assert capsule is not None
    # the capsule binds the real artifact, re-measured independently
    artifact = Path(str(job_result["artifact_path"]))
    assert capsule.output_artifact_digest == sha256_file(artifact) == job_result["sha256"]
    # ...and the same digests as the approved binding
    assert capsule.typed_ast_digest == result.ast.ast_digest
    assert capsule.compiled_plan_digest == result.plan.plan_digest
    assert capsule.preview_digest == result.preview.preview_digest
    assert capsule.scope_digest == result.ast.scope.scope_digest
    assert capsule.approval_id == result.approval_id
    assert capsule.approval_binding == service.approvals.get(result.approval_id).identity_digest
    assert capsule.source_revision == REVISION
    assert capsule.execution_attempt_identity.startswith(outcome.job_id + ":attempt")

    # independent, content-based verification of the real artifact
    report = outcome.report
    assert report is not None
    assert capsule.verifier_report_digest == report.report_digest
    by_name = {c.name: c for c in report.checks}
    assert by_name["intended_change"].verdict is CheckVerdict.PASS
    assert report.verdict in (CheckVerdict.PASS, CheckVerdict.INCONCLUSIVE)
    assert report.verdict is not CheckVerdict.FAIL
    assert capsule.verifier_verdict == report.verdict

    # durable evidence references the EXISTING journal + passport
    prov = capsule.provenance_reference
    assert prov["journal"] is not None
    assert prov["journal"]["chain_verified"] is True
    assert prov["journal"]["head_hash"].startswith("sha256:")
    assert prov["passport"] is not None
    assert prov["passport"].get("passport_digest", "").startswith("sha256:")
    assert journal is not None and journal.records_for_job(outcome.job_id)

    # replay of the consumed approval is refused — single-use
    replay = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert replay.status == "refused"
    assert "replay" in replay.detail
    assert queue.job_ids() == [outcome.job_id]  # still exactly one row


@pytest.mark.asyncio
async def test_ambiguous_and_unsupported_intents_enqueue_nothing(harness, tmp_path: Path) -> None:
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)

    clar = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim the video",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(clar, Clarification) and clar.questions

    refusal = service.submit_text_intent(
        sid,
        "turn-2",
        sequence_number=2,
        text="brighten the whole clip",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(refusal, Refusal) and refusal.reason == "unsupported_operation_in_slice"

    out_of_range = service.submit_text_intent(
        sid,
        "turn-3",
        sequence_number=3,
        text="trim from 1 to 99 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(out_of_range, Refusal) and out_of_range.reason == "out_of_range"

    assert queue.job_ids() == []  # no approval, no job, no mutation


@pytest.mark.asyncio
async def test_stale_and_cancelled_turns_never_execute(harness, tmp_path: Path) -> None:
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)

    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)

    # a newer turn arrives first; the older one is now stale
    newer = service.submit_text_intent(
        sid,
        "turn-2",
        sequence_number=2,
        text="trim from 0.2 to 0.4 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(newer, PipelineResult)
    with pytest.raises(StaleTurnError):
        service.submit_text_intent(
            sid,
            "turn-0",
            sequence_number=1,
            text="trim from 0.1 to 0.2 seconds",
            asset_id="src",
            source_path=str(source),
        )

    # cancel turn-1: its approval is revoked and it can never execute
    service.sessions.cancel(sid, "turn-1")
    with pytest.raises(ApprovalError):
        service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert queue.job_ids() == []

    # the live turn still works end to end
    service.approve(sid, "turn-2", newer.approval_id, principal_id=principal)
    ok = await service.execute(sid, "turn-2", newer.approval_id, principal_id=principal)
    assert ok.status in ("executed", "executed_unverified"), ok.detail


@pytest.mark.asyncio
async def test_tampered_artifact_fails_independent_verification(harness, tmp_path: Path) -> None:
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified")
    job_result = await queue.get_result(outcome.job_id)
    artifact = Path(str(job_result["artifact_path"]))

    # pristine artifact: intended change proven
    pristine = verify_keep(source, artifact, scope=result.ast.scope)
    assert {c.name: c.verdict for c in pristine.checks}["intended_change"] is CheckVerdict.PASS

    # post-hoc tamper: re-grade every frame of the artifact -> preservation FAIL
    binary = resolve_ffmpeg_bin()
    tampered = tmp_path / "tampered.mp4"
    proc = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(artifact),
            "-vf",
            "eq=brightness=0.25",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(tampered),
        ],
        capture_output=True,
        timeout=180,
    )
    assert proc.returncode == 0
    report = verify_keep(source, tampered, scope=result.ast.scope, binary=binary)
    assert report.verdict is CheckVerdict.FAIL


class _FakeCaptionEngine:
    """Declared test double for the external caption seam only (the engine is
    injected; transcription is never on the execution path)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_path: Any, *, language: str | None = None) -> Any:
        from nexus_ai_agent.creative.packs.caption.models import TranscriptRef, TranscriptSegment

        return TranscriptRef(
            transcript_id="tr_e2e",
            segments=(TranscriptSegment(start_us=0, end_us=2_000_000, text=self._text),),
        )


@pytest.mark.asyncio
async def test_voice_turn_flows_through_the_same_pipeline(harness, tmp_path: Path) -> None:
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    service._deps = IBPCServiceDeps(
        queue=service._deps.queue,
        registry=service._deps.registry,
        workspace_root=service._deps.workspace_root,
        revision_provider=service._deps.revision_provider,
        journal=service._deps.journal,
        facts_provider=service._deps.facts_provider,
        attempt_provider=service._deps.attempt_provider,
        authority=service._deps.authority,
        channel_binding_ref=service._deps.channel_binding_ref,
        caption_engine=_FakeCaptionEngine("trim from 0.5 to 1.5 seconds"),
    )
    sid = _open_session(service, principal)
    result = await service.submit_voice_intent(
        sid,
        "voice-1",
        sequence_number=1,
        audio=b"\x00" * 64,
        asset_id="src",
        source_path=str(source),
        audio_dir=tmp_path / "audio",
    )
    assert isinstance(result, PipelineResult)
    assert result.ast.scope.start_us == 500_000 and result.ast.scope.end_us == 1_500_000
    # the raw transcript never appears in any record — digest only
    turn = service.sessions.get_session(sid).turns["voice-1"]
    evidence = turn.evidence()
    assert "trim from 0.5 to 1.5 seconds" not in str(evidence)
    assert evidence["transcript"].startswith("sha256:")

    service.approve(sid, "voice-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "voice-1", result.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
    assert outcome.capsule is not None
    assert outcome.capsule.verifier_verdict is not CheckVerdict.FAIL


@pytest.mark.asyncio
async def test_concurrent_double_execute_has_exactly_one_winner(harness, tmp_path: Path) -> None:
    """Two concurrent execute() calls for the same approval race through the
    service-level lock + the ledger CAS: exactly one wins, the other is a
    typed replay refusal, and the queue holds exactly one job."""
    import asyncio

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcomes = await asyncio.gather(
        service.execute(sid, "turn-1", result.approval_id, principal_id=principal),
        service.execute(sid, "turn-1", result.approval_id, principal_id=principal),
    )
    statuses = sorted(o.status for o in outcomes)
    assert statuses[1] == "refused"
    assert statuses[0] in ("executed", "executed_unverified")
    refused = next(o for o in outcomes if o.status == "refused")
    assert "replay" in refused.detail or "invalid_state" in refused.detail
    assert len(queue.job_ids()) == 1


@pytest.mark.asyncio
async def test_cancel_racing_the_staging_window_is_clean(harness, tmp_path: Path) -> None:
    """P1 (independent review): a cancel/interrupt that lands after the turn is
    reserved must not corrupt the execution.

    ``cancel``/``interrupt`` refuse while a turn is EXECUTING, so once the
    reservation transition commits no cancel can terminalise the turn in the
    lock-free staging window. We drive the race deterministically by attempting
    the cancel from inside the staging hook (the exact window) and assert it is
    refused, the execution completes, and exactly one job is enqueued — no
    unhandled ``TransitionError`` and no contradictory state."""
    from nexus_ai_agent.nagar.ibpc.session import TransitionError

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    seen: dict[str, object] = {}
    original_stage = service._stage_source

    def _stage_then_cancel(src, workspace):
        # The exact window: after reserve/consume, before enqueue.
        try:
            service.sessions.cancel(sid, "turn-1")
            seen["cancel"] = "allowed"
        except TransitionError as exc:
            seen["cancel"] = exc.reason
        return original_stage(src, workspace)

    service._stage_source = _stage_then_cancel
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)

    assert seen["cancel"] == "executing"  # the cancel was refused, not applied
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
    assert len(queue.job_ids()) == 1
    assert service.sessions.get_turn(sid, "turn-1").turn_state.value == "EXECUTED"


@pytest.mark.asyncio
async def test_cancel_before_execute_refuses_and_enqueues_nothing(harness, tmp_path: Path) -> None:
    """A cancel committed before execute() must refuse the execution and leave
    the queue empty (revocation is honoured; nothing is enqueued)."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    service.sessions.cancel(sid, "turn-1")

    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert queue.job_ids() == []


@pytest.mark.asyncio
async def test_enqueue_failure_marks_failed_without_leaking(harness, tmp_path: Path) -> None:
    """An enqueue failure must surface as a typed refusal, terminalise the turn
    as FAILED, and never raise an unhandled state-transition exception."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    async def _boom(*, job_type, idempotency_key, payload):
        raise RuntimeError("queue unavailable")

    service._deps.queue.enqueue = _boom  # type: ignore[assignment]
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)

    assert outcome.status == "refused"
    assert "enqueue_failed" in outcome.detail
    assert queue.job_ids() == []
    # read the raw turn (get_turn refuses terminal turns by design)
    turn = service.sessions.get_session(sid).turns["turn-1"]
    assert turn.turn_state.value == "FAILED"


@pytest.mark.asyncio
async def test_execute_runs_blocking_media_work_off_the_event_loop(harness, tmp_path: Path) -> None:
    """``_resolve_identity`` / ``_stage_source`` / ``_verify_artifact`` /
    ``_provenance_reference`` are synchronous and file/ffmpeg-backed.  ``execute``
    must dispatch each to a worker thread so a long render never blocks the loop."""
    import threading

    service, queue, creative_root, journal, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    loop_ident = threading.get_ident()
    idents: dict[str, int] = {}

    def _wrap(name: str, fn):
        def inner(*args: Any, **kwargs: Any) -> Any:
            idents[name] = threading.get_ident()
            return fn(*args, **kwargs)

        return inner

    service._resolve_identity = _wrap("_resolve_identity", service._resolve_identity)
    service._stage_source = _wrap("_stage_source", service._stage_source)
    service._verify_artifact = _wrap("_verify_artifact", service._verify_artifact)
    service._provenance_reference = _wrap("_provenance_reference", service._provenance_reference)

    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
    for name in (
        "_resolve_identity",
        "_stage_source",
        "_verify_artifact",
        "_provenance_reference",
    ):
        assert name in idents, f"{name} was never called"
        assert idents[name] != loop_ident, f"{name} ran on the event-loop thread"


@pytest.mark.asyncio
async def test_voice_intent_runs_parse_body_off_the_event_loop(harness, tmp_path: Path) -> None:
    """The voice parse/preview/approval body probes media with sync ffmpeg; it
    must run on a worker thread, not on the loop."""
    import threading

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    service._deps = IBPCServiceDeps(
        queue=service._deps.queue,
        registry=service._deps.registry,
        workspace_root=service._deps.workspace_root,
        revision_provider=service._deps.revision_provider,
        journal=service._deps.journal,
        facts_provider=service._deps.facts_provider,
        attempt_provider=service._deps.attempt_provider,
        authority=service._deps.authority,
        channel_binding_ref=service._deps.channel_binding_ref,
        caption_engine=_FakeCaptionEngine("trim from 0.5 to 1.5 seconds"),
    )
    sid = _open_session(service, principal)

    loop_ident = threading.get_ident()
    seen: dict[str, int] = {}
    original = service._continue_with_text

    def inner(*args: Any, **kwargs: Any) -> Any:
        seen["ident"] = threading.get_ident()
        return original(*args, **kwargs)

    service._continue_with_text = inner
    result = await service.submit_voice_intent(
        sid,
        "voice-1",
        sequence_number=1,
        audio=b"\x00" * 64,
        asset_id="src",
        source_path=str(source),
        audio_dir=tmp_path / "audio",
    )
    assert isinstance(result, PipelineResult)
    assert "ident" in seen, "_continue_with_text was never called"
    assert seen["ident"] != loop_ident, "the voice parse body ran on the event-loop thread"


def test_corrupt_source_is_a_typed_refusal_not_a_crash(harness, tmp_path: Path) -> None:
    """A present-but-unreadable source must fail closed with a typed Refusal
    (``unreadable_source``) and mark the turn failed, never raise a raw
    RenderError/FootprintVerifyError out of ``submit_text_intent``."""
    service, queue, _, _, principal = harness
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"not a video at all" * 32)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-bad",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(bad),
    )
    assert isinstance(result, Refusal), f"expected a Refusal, got {type(result).__name__}"
    assert result.reason == "unreadable_source"
    # the turn is failed, not left live, and nothing was enqueued
    assert service.sessions.get_session(sid).turns["turn-bad"].turn_state.name == "FAILED"
    assert queue.job_ids() == []


@pytest.mark.asyncio
async def test_corrupt_source_voice_is_a_typed_refusal_not_a_crash(harness, tmp_path: Path) -> None:
    """Same guarantee on the voice path: a corrupt source becomes a typed
    Refusal, not an untyped exception escaping the async method."""
    service, queue, _, _, principal = harness
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(b"not a video at all" * 32)
    service._deps = IBPCServiceDeps(
        queue=service._deps.queue,
        registry=service._deps.registry,
        workspace_root=service._deps.workspace_root,
        revision_provider=service._deps.revision_provider,
        journal=service._deps.journal,
        facts_provider=service._deps.facts_provider,
        attempt_provider=service._deps.attempt_provider,
        authority=service._deps.authority,
        channel_binding_ref=service._deps.channel_binding_ref,
        caption_engine=_FakeCaptionEngine("trim from 0.5 to 1.5 seconds"),
    )
    sid = _open_session(service, principal)
    result = await service.submit_voice_intent(
        sid,
        "voice-bad",
        sequence_number=1,
        audio=b"\x00" * 64,
        asset_id="src",
        source_path=str(bad),
        audio_dir=tmp_path / "audio",
    )
    assert isinstance(result, Refusal), f"expected a Refusal, got {type(result).__name__}"
    assert result.reason == "unreadable_source"
    assert queue.job_ids() == []


# --------------------------------------------------------------------------- #
# Phase 1/2 — authority continuity + proof-completeness (real chain)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_schema2_identity_reaches_the_worker_and_is_authorized(
    harness, tmp_path: Path
) -> None:
    """Phase 1: the enqueued payload carries the canonical schema-2 identity
    (principal + project + binding) and the worker *independently* re-authorizes
    it from the durable directory before any media work."""
    import json as _json

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail

    job_result = await queue.get_result(outcome.job_id)
    canonical_execution = job_result.get("canonical_execution") if job_result else None
    assert isinstance(canonical_execution, dict)
    authorization = canonical_execution["authorization"]
    assert authorization["decision_source"] == "SQLApplicationDirectory.authorize"
    assert authorization["binding_decision_source"] == "SQLApplicationDirectory.require_binding"
    assert authorization["principal_id"] == principal
    assert authorization["channel_binding_id"].startswith("cbn_")

    # the durable queue row itself carries the canonical schema-2 identity
    import sqlite3

    conn = sqlite3.connect(queue.db_path)
    try:
        raw = conn.execute(
            "SELECT payload_json FROM nexus_job_queue WHERE idempotency_key = ?",
            (f"ibpc:{result.approval_id}",),
        ).fetchone()
    finally:
        conn.close()
    assert raw is not None
    payload = _json.loads(raw[0])
    assert payload["schema_version"] == 2
    assert payload["principal_id"] == principal
    assert payload["channel_binding_id"].startswith("cbn_")
    assert payload["intent"]["operation"] == "timeline.trim"


@pytest.mark.asyncio
async def test_execution_refused_when_authority_is_revoked(harness, tmp_path: Path) -> None:
    """Phase 1 fail-closed: if the trusted authority no longer verifies the
    principal at execution time, nothing is enqueued."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    # revoke trust: replace the injected authority with one that denies
    from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError

    class _Revoked:
        def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
            raise CanonicalAuthorityError("authorization_denied", "revoked mid-flight")

    service._deps = IBPCServiceDeps(
        queue=service._deps.queue,
        registry=service._deps.registry,
        workspace_root=service._deps.workspace_root,
        revision_provider=service._deps.revision_provider,
        journal=service._deps.journal,
        facts_provider=service._deps.facts_provider,
        attempt_provider=service._deps.attempt_provider,
        authority=_Revoked(),
        channel_binding_ref=service._deps.channel_binding_ref,
    )
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert "authority_unverified" in outcome.detail
    assert queue.job_ids() == []


@pytest.mark.asyncio
async def test_proof_gate_downgrades_when_verifier_is_not_pass(harness, tmp_path: Path) -> None:
    """Phase 2: an artifact whose independent verdict is not PASS (e.g. a
    lossy re-encode that cannot be proven bit-exact) must never be reported as
    verified: the outcome is ``executed_unverified`` and the capsule names the
    exact gap."""
    from nexus_ai_agent.nagar.ibpc.models import CheckVerdict as _V
    from nexus_ai_agent.nagar.ibpc.models import VerifierReport

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    # Force a non-PASS verdict from the independent verifier without touching
    # the real pipeline: wrap _verify_artifact to rewrite only the verdict.
    original = service._verify_artifact

    def _downgrade(ctx, artifact_path, input_path):
        digest, report = original(ctx, artifact_path, input_path)
        downgraded = VerifierReport(
            scope_digest=report.scope_digest,
            source_digest=report.source_digest,
            artifact_digest=report.artifact_digest,
            source_frames=report.source_frames,
            artifact_frames=report.artifact_frames,
            checks=report.checks,
            verdict=_V.INCONCLUSIVE,
        )
        return digest, downgraded

    service._verify_artifact = _downgrade
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "executed_unverified", outcome.detail
    assert outcome.capsule is not None
    assert outcome.capsule.proof_status == "unverified"
    assert "verdict_not_pass:INCONCLUSIVE" in outcome.capsule.proof_gaps
    assert "verdict_not_pass:INCONCLUSIVE" in outcome.detail


@pytest.mark.asyncio
async def test_empty_journal_never_mints_verified_capsule(harness, tmp_path: Path) -> None:
    """P0 (independent review): a real execution whose provenance journal holds
    ZERO records for the job must never be reported as verified proof.

    ``verify_chain([])`` returns ``ok=True`` (a vacuous truth), so a gate that
    trusts ``chain_verified`` alone would mint a proof-bearing capsule with no
    evidence. We swap the service's journal for a real-but-empty one so the job
    runs and the observer writes to the *old* journal only — the proof gate must
    then see ``record_count == 0`` and withhold the verified claim."""
    from nexus_ai_agent.provenance.journal import CausalJournal

    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    # A real journal with no records for this job (separate file; the wired
    # observer keeps writing to the original journal, which the service no
    # longer references).
    service._deps.journal = CausalJournal(tmp_path / "empty.causal.sqlite3")

    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)

    assert queue.job_ids() == [outcome.job_id]  # the job really ran
    assert outcome.capsule is not None
    assert outcome.capsule.provenance_reference["journal"]["record_count"] == 0
    assert outcome.capsule.proof_status == "unverified"
    assert "journal_empty" in outcome.capsule.proof_gaps
    assert outcome.status == "executed_unverified"


@pytest.mark.asyncio
async def test_journal_records_must_match_job(harness, tmp_path: Path) -> None:
    """P0 (independent review): the proof gate must accept a job whose records
    are interleaved with other jobs' records in the shared chain.

    ``records_for_job(job)`` is NOT itself a gapless chain (its seqs skip the
    other jobs' rows), so verifying that subset with ``verify_chain`` reports a
    false ``sequence gap`` for any job that is not first in the journal. The
    reference must therefore verify the WHOLE chain and require the per-job
    record count to be positive — a second execution must still reach verified
    proof."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)

    async def _run_one(turn: str) -> object:
        sid = _open_session(service, principal)
        result = service.submit_text_intent(
            sid,
            turn,
            sequence_number=1,
            text="trim from 0.5 to 1.5 seconds",
            asset_id="src",
            source_path=str(source),
        )
        assert isinstance(result, PipelineResult)
        service.approve(sid, turn, result.approval_id, principal_id=principal)
        return await service.execute(sid, turn, result.approval_id, principal_id=principal)

    first = await _run_one("turn-1")
    second = await _run_one("turn-2")
    for outcome in (first, second):
        assert outcome.capsule is not None
        journal_ref = outcome.capsule.provenance_reference["journal"]
        # interleaved chain still verifies globally, and the per-job evidence is
        # positive — never a false "unverified" from a subset sequence gap.
        assert journal_ref["chain_verified"] is True
        assert journal_ref["record_count"] > 0
        assert journal_ref["head_hash"].startswith("sha256:")
        # the journal must contribute NO gap (the lossy lane's verdict may still
        # be INCONCLUSIVE, but that is never confused with missing provenance).
        assert "journal_chain_unverified" not in outcome.capsule.proof_gaps
        assert "journal_empty" not in outcome.capsule.proof_gaps
        assert "journal_missing" not in outcome.capsule.proof_gaps
        assert "journal_head_missing" not in outcome.capsule.proof_gaps


def _service_with_limit(service, limit):
    import dataclasses

    from nexus_ai_agent.nagar.ibpc.service import IBPCService

    return IBPCService(dataclasses.replace(service._deps, max_contexts=limit))


def test_context_store_retains_active_and_evicts_terminal(harness, tmp_path: Path) -> None:
    """P2 (independent review): the in-process context store must be bounded,
    but only *terminal* contexts may ever be evicted. An active (PENDING or
    APPROVED) context is never evicted, and eviction never re-enables replay."""
    from nexus_ai_agent.nagar.ibpc.models import ApprovalState

    service, _, _, _, principal = harness
    bounded = _service_with_limit(service, 2)
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(bounded, principal)

    def _submit(turn: str, seq: int):
        return bounded.submit_text_intent(
            sid,
            turn,
            sequence_number=seq,
            text="trim from 0.5 to 1.5 seconds",
            asset_id="src",
            source_path=str(source),
        )

    # three live approvals, limit 2 → nothing is evictable (all PENDING).
    results = [_submit(f"turn-{i}", i) for i in (1, 2, 3)]
    assert len(bounded._contexts) == 3  # active contexts are never evicted

    # cancel the oldest → it becomes terminal (REVOKED) and evictable.
    oldest = results[0].approval_id
    bounded.sessions.cancel(sid, "turn-1")
    assert bounded.approvals.get(oldest).state is ApprovalState.REVOKED

    # a fourth submission over the limit evicts the oldest *terminal* context.
    _submit("turn-4", 4)
    assert oldest not in bounded._contexts  # terminal context reclaimed
    for r in results[1:]:
        assert r.approval_id in bounded._contexts  # active contexts retained


@pytest.mark.asyncio
async def test_context_eviction_does_not_re_enable_replay(harness, tmp_path: Path) -> None:
    """Evicting a consumed context must never let a consumed approval execute
    again: the ledger replay refusal fires before any context lookup."""
    service, queue, _, _, principal = harness
    bounded = _service_with_limit(service, 1)
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(bounded, principal)

    first = bounded.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(first, PipelineResult)
    bounded.approve(sid, "turn-1", first.approval_id, principal_id=principal)
    outcome = await bounded.execute(sid, "turn-1", first.approval_id, principal_id=principal)
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail

    # force eviction of the consumed context with a fresh live submission.
    bounded.submit_text_intent(
        sid,
        "turn-2",
        sequence_number=2,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert first.approval_id not in bounded._contexts  # consumed → reclaimed

    replay = await bounded.execute(sid, "turn-1", first.approval_id, principal_id=principal)
    assert replay.status == "refused"
    # the consumed approval is never re-executable: the attempt is refused
    # (unknown_context after eviction; the ledger replay refusal otherwise) and
    # NO second job is ever enqueued.
    assert replay.job_id is None
    assert len(queue.job_ids()) == 1


@pytest.mark.asyncio
async def test_source_swap_during_staging_is_refused(harness, tmp_path: Path) -> None:
    """P1 (independent review): a filesystem swap of the source *before* the
    staging copy is refused — the staged content identity is re-checked against
    the admitted digest, so bytes the approval never covered are never rendered.

    The swap is driven in the real TOCTOU window (after admission, before the
    copy) via a staging hook, not by a sleep."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    other = tmp_path / "other.mp4"
    _clip(other, seconds=3.0)  # different content → different digest
    original_stage = service._stage_source

    def _swap_then_stage(src, workspace):
        source.write_bytes(other.read_bytes())  # swap BEFORE the copy
        return original_stage(src, workspace)

    service._stage_source = _swap_then_stage
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert "source_changed_during_staging" in outcome.detail
    assert queue.job_ids() == []  # nothing rendered


@pytest.mark.asyncio
async def test_symlinked_source_is_refused(harness, tmp_path: Path) -> None:
    """A symlinked source path is refused at staging (``O_NOFOLLOW``): the bytes
    rendered are always the admitted inode, never a redirect."""
    service, queue, _, _, principal = harness
    real = tmp_path / "real.mp4"
    _clip(real)
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(link),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert "source_unreadable" in outcome.detail or "source_changed" in outcome.detail
    assert queue.job_ids() == []


@pytest.mark.asyncio
async def test_staged_digest_mismatch_refuses_before_enqueue(harness, tmp_path: Path) -> None:
    """If the staged copy's content identity ever diverges from the admitted
    digest, execution must be refused BEFORE enqueue (nothing rendered)."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    other = tmp_path / "other.mp4"
    _clip(other, seconds=3.0)
    original_stage = service._stage_source

    def _stage_wrong_bytes(src, workspace):
        staged = original_stage(src, workspace)
        staged.write_bytes(other.read_bytes())  # simulate a substituted staged copy
        return staged

    service._stage_source = _stage_wrong_bytes
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert outcome.status == "refused"
    assert "source_changed_during_staging" in outcome.detail
    assert queue.job_ids() == []  # nothing was enqueued


@pytest.mark.asyncio
async def test_verifier_reads_the_staged_immutable_input(harness, tmp_path: Path) -> None:
    """The independent verifier must be derived from the staged, immutable input
    copy — a swap of the mutable origin path after staging must not change the
    evidence."""
    service, queue, _, _, principal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service, principal)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id=principal)

    other = tmp_path / "other.mp4"
    _clip(other, seconds=3.0)
    original_verify = service._verify_artifact
    seen: dict[str, str] = {}

    def _verify(ctx, artifact_path, input_path):
        # the verifier's source argument must be the staged copy, not the origin
        seen["input_path"] = str(input_path)
        seen["origin"] = str(ctx.source_path)
        # swap the mutable origin right before the verifier decodes: irrelevant,
        # because it decodes the staged copy.
        ctx.source_path.write_bytes(other.read_bytes())
        return original_verify(ctx, artifact_path, input_path)

    service._verify_artifact = _verify
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id=principal)
    assert seen["input_path"] != seen["origin"]
    assert seen["input_path"].endswith("input.mp4")
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
