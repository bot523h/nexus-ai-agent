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
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
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
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "ibpc-e2e"
REVISION = 7
#: the lane renders at LaneProfile defaults — the source matches so the
#: footprint verifier can compare decoded frames 1:1
SIZE = "1280x720"
RATE = 30
SECONDS = 2.0


def _clip(path: Path) -> None:
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
            f"testsrc=duration={SECONDS}:size={SIZE}:rate={RATE}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={SECONDS}",
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
    settings_module.get_settings.cache_clear()
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
        )
    )
    yield service, queue, creative_root, journal
    settings_module.get_settings.cache_clear()


def _open_session(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten-1",
        project_id=PROJECT_ID,
        principal_id="user-1",
        user_id=42,
        chat_id=4242,
        current_revision=REVISION,
    )


@pytest.mark.asyncio
async def test_full_chain_text_intent_to_verified_capsule(harness, tmp_path: Path) -> None:
    service, queue, creative_root, journal = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service)

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
    binding = service.approve(sid, "turn-1", result.approval_id, principal_id="user-1")
    assert binding.state is ApprovalState.APPROVED

    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id="user-1")
    assert outcome.status == "executed", outcome.detail
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
    replay = await service.execute(sid, "turn-1", result.approval_id, principal_id="user-1")
    assert replay.status == "refused"
    assert "replay" in replay.detail
    assert queue.job_ids() == [outcome.job_id]  # still exactly one row


@pytest.mark.asyncio
async def test_ambiguous_and_unsupported_intents_enqueue_nothing(harness, tmp_path: Path) -> None:
    service, queue, _, _ = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service)

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
    service, queue, _, _ = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service)

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
        service.approve(sid, "turn-1", result.approval_id, principal_id="user-1")
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id="user-1")
    assert outcome.status == "refused"
    assert queue.job_ids() == []

    # the live turn still works end to end
    service.approve(sid, "turn-2", newer.approval_id, principal_id="user-1")
    ok = await service.execute(sid, "turn-2", newer.approval_id, principal_id="user-1")
    assert ok.status == "executed", ok.detail


@pytest.mark.asyncio
async def test_tampered_artifact_fails_independent_verification(harness, tmp_path: Path) -> None:
    service, queue, _, _ = harness
    source = tmp_path / "source.mp4"
    _clip(source)
    sid = _open_session(service)
    result = service.submit_text_intent(
        sid,
        "turn-1",
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(sid, "turn-1", result.approval_id, principal_id="user-1")
    outcome = await service.execute(sid, "turn-1", result.approval_id, principal_id="user-1")
    assert outcome.status == "executed"
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
    service, queue, _, _ = harness
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
        caption_engine=_FakeCaptionEngine("trim from 0.5 to 1.5 seconds"),
    )
    sid = _open_session(service)
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

    service.approve(sid, "voice-1", result.approval_id, principal_id="user-1")
    outcome = await service.execute(sid, "voice-1", result.approval_id, principal_id="user-1")
    assert outcome.status == "executed", outcome.detail
    assert outcome.capsule is not None
    assert outcome.capsule.verifier_verdict is not CheckVerdict.FAIL
