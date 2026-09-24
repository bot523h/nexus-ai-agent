"""Gate 5 — the pure half of the job lifecycle contract.

These tests pin the *vocabulary and the boundaries* the worker and the queue
must obey, with no media, no queue and no event loop. The end-to-end proof
(real queue, real FFmpeg, real publication) lives in
``tests/integration/test_gate5_job_lifecycle.py``; this file exists so that a
regression in the state table, the classification table or the publication
policy fails in milliseconds and names the exact link that broke.

The one boundary deliberately asserted here without a queue is the *forbidden*
transition: an attempt may not jump from PENDING to verification or success —
the durable running mark is proven in the e2e suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.application.artifact_publication import (
    DOCUMENT_MARKERS,
    DestinationOccupied,
    StagingNotPublishable,
    VerificationFailed,
    publish_artifact,
    read_published_identity,
    sidecar_path,
    verify_document_artifact,
)
from nexus_ai_agent.application.job_lifecycle import (
    ACTIVE_STATUSES,
    ALLOWED_PHASE_TRANSITIONS,
    FAILURE_STATUS_BY_CLASS,
    FAILURE_STATUSES,
    PHASE_CONTRACT,
    RESULT_FAILURE_CLASS,
    SUCCESS_STATUSES,
    TERMINAL_STATUSES,
    ArtifactTrace,
    FailureClass,
    IllegalPhaseTransition,
    JobPhase,
    PhaseTrack,
    declares_outcome,
    failure_status,
    is_failure,
    is_success,
    is_terminal,
    result_job_status,
    stamp_job_id,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.render_jobs import ERROR_CODES, FAILURE_CLASS_BY_CODE


def _verifier(*, empty_fails: bool = True):
    """A stand-in verifier with the real one's semantics: raise ⇒ invalid.

    ``size > 0`` passes; anything else raises (the canonical verifier's own
    first two checks).
    """

    def _verify(path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        if size <= 0 and empty_fails:
            raise VerificationFailed(f"artifact is empty: {path}")
        return {"size_bytes": size}

    return _verify


# ── the state machine ───────────────────────────────────────────────────────


def test_transition_table_is_the_contract() -> None:
    """Every phase is documented; transitions are explicit; terminals end."""
    assert set(ALLOWED_PHASE_TRANSITIONS) == set(JobPhase)
    assert set(PHASE_CONTRACT) == set(JobPhase)
    for phase, spec in PHASE_CONTRACT.items():
        assert spec.phase is phase
        assert spec.allowed == ALLOWED_PHASE_TRANSITIONS[phase]
        assert spec.entry_condition and spec.exit_condition
        assert spec.side_effects and spec.retry_semantics
        assert isinstance(spec.durable_status, JobStatus)
    assert ALLOWED_PHASE_TRANSITIONS[JobPhase.SUCCEEDED] == frozenset()
    assert ALLOWED_PHASE_TRANSITIONS[JobPhase.FAILED] == frozenset()
    assert PHASE_CONTRACT[JobPhase.SUCCEEDED].terminal is True
    assert PHASE_CONTRACT[JobPhase.FAILED].terminal is True
    assert not PHASE_CONTRACT[JobPhase.PENDING].terminal
    assert not PHASE_CONTRACT[JobPhase.RUNNING].terminal
    assert not PHASE_CONTRACT[JobPhase.VERIFYING].terminal

    track = PhaseTrack()
    for phase in (JobPhase.RUNNING, JobPhase.VERIFYING, JobPhase.SUCCEEDED):
        track.advance(phase)
    assert track.trail() == ("pending", "running", "verifying", "succeeded")


def test_status_algebra_is_total_and_single_sourced() -> None:
    """Success, failure and active statuses partition the enum exactly."""
    assert SUCCESS_STATUSES == {JobStatus.COMPLETED}
    assert FAILURE_STATUSES == {
        JobStatus.FAILED,
        JobStatus.FAILED_RETRYABLE,
        JobStatus.TERMINAL_FAILED,
    }
    assert ACTIVE_STATUSES == {JobStatus.PENDING, JobStatus.PROCESSING}
    assert TERMINAL_STATUSES == SUCCESS_STATUSES | FAILURE_STATUSES
    assert TERMINAL_STATUSES | ACTIVE_STATUSES == set(JobStatus)
    assert not TERMINAL_STATUSES & ACTIVE_STATUSES
    for status in JobStatus:
        assert sum((is_success(status), is_failure(status), not is_terminal(status))) == 1


def test_running_boundary_cannot_be_skipped() -> None:
    """The phase table forbids PENDING → verification and PENDING → success.

    The durable half (the row carries the PROCESSING mark before the handler
    body runs) is proven end to end in
    ``test_gate5_job_lifecycle.test_running_boundary_row_is_processing_while_handler_runs``.
    """
    track = PhaseTrack()
    with pytest.raises(IllegalPhaseTransition):
        track.advance(JobPhase.VERIFYING)
    with pytest.raises(IllegalPhaseTransition):
        track.advance(JobPhase.SUCCEEDED)
    with pytest.raises(IllegalPhaseTransition):
        track.advance(JobPhase.FAILED)
    track.advance(JobPhase.RUNNING)
    with pytest.raises(IllegalPhaseTransition):
        track.advance(JobPhase.PENDING)
    assert PHASE_CONTRACT[JobPhase.PENDING].durable_status is JobStatus.PENDING
    assert PHASE_CONTRACT[JobPhase.RUNNING].durable_status is JobStatus.PROCESSING
    assert PHASE_CONTRACT[JobPhase.SUCCEEDED].durable_status is JobStatus.COMPLETED


def test_side_effect_boundary_refuses_unverified_publication(tmp_path: Path) -> None:
    """No publication before verification; a refused verification publishes
    nothing and a valid foreign artifact is never replaced."""
    destination = tmp_path / "output.mp4"
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"not verified bytes")

    def _refuse(path: Path) -> Any:
        raise VerificationFailed(f"refusing {path}")

    with pytest.raises(VerificationFailed):
        publish_artifact(
            staging_path=staged, destination=destination, identity={"k": "v"}, verify=_refuse
        )
    assert not destination.exists(), "a refused verification must not publish"
    assert not sidecar_path(destination).exists()

    destination.write_bytes(b"existing valid artifact")
    with pytest.raises(DestinationOccupied):
        publish_artifact(
            staging_path=staged,
            destination=destination,
            identity={"k": "v"},
            verify=_verifier(),
        )
    assert destination.read_bytes() == b"existing valid artifact"


# ── failure classes (the evidence for the split) ─────────────────────────────


def test_retryable_vs_terminal_failure() -> None:
    """The split exists because the repository really has both kinds."""
    assert FAILURE_CLASS_BY_CODE["ffmpeg_unavailable"] is FailureClass.RETRYABLE
    assert FAILURE_CLASS_BY_CODE["caption_profile_unavailable"] is FailureClass.RETRYABLE
    assert FAILURE_CLASS_BY_CODE["invalid_request"] is FailureClass.TERMINAL
    assert FAILURE_CLASS_BY_CODE["unsupported_operation"] is FailureClass.TERMINAL
    assert FAILURE_CLASS_BY_CODE["artifact_destination_conflict"] is FailureClass.TERMINAL
    assert set(FAILURE_CLASS_BY_CODE) == set(ERROR_CODES)

    assert FAILURE_STATUS_BY_CLASS[FailureClass.RETRYABLE] is JobStatus.FAILED_RETRYABLE
    assert FAILURE_STATUS_BY_CLASS[FailureClass.TERMINAL] is JobStatus.TERMINAL_FAILED

    assert failure_status(None) is JobStatus.TERMINAL_FAILED
    assert failure_status("something-new") is JobStatus.TERMINAL_FAILED
    assert failure_status("terminal") is JobStatus.TERMINAL_FAILED
    assert failure_status("retryable") is JobStatus.FAILED_RETRYABLE
    assert is_failure(failure_status("retryable"))
    assert is_failure(failure_status(None))
    assert not is_success(failure_status("retryable"))


def test_result_contract_maps_to_statuses_fail_closed() -> None:
    """The queue's classification of handler results, including legacy shapes."""
    success = {"success": True, "artifact_path": "/tmp/a"}
    assert declares_outcome(success)
    assert result_job_status(success) is JobStatus.COMPLETED
    stamped = stamp_job_id(success, "job-1")
    assert stamped["job_id"] == "job-1"

    typed = {"success": False, "error_code": "render_failed", RESULT_FAILURE_CLASS: "retryable"}
    assert result_job_status(typed) is JobStatus.FAILED_RETRYABLE
    unclassified = {"success": False, "error_code": "render_failed"}
    assert result_job_status(unclassified) is JobStatus.TERMINAL_FAILED
    bogus = {"success": False, RESULT_FAILURE_CLASS: "definitely-retryable"}
    assert result_job_status(bogus) is JobStatus.TERMINAL_FAILED

    legacy = {"message": "done"}
    assert not declares_outcome(legacy)
    assert result_job_status(legacy) is None
    assert stamp_job_id(legacy, "job-2") == {"message": "done"}


def test_stamp_job_id_fills_the_trace_record() -> None:
    """The row identity lands in the envelope *and* in the artifact's own
    lineage record — a traceability block that cannot name its job is not
    traceable (the e2e chain test relies on this)."""
    trace = {
        "command_id": "cmd-1",
        "job_id": None,
        "project_id": "shot-1",
        "operation_id": "timeline.trim",
        "revision": 1,
    }
    result = {"success": True, "traceability": trace}
    stamped = stamp_job_id(result, "job-9")
    assert stamped["job_id"] == "job-9"
    assert stamped["traceability"]["job_id"] == "job-9"
    assert result["traceability"]["job_id"] is None, "stamping never mutates its input"


# ── document verification + destination policy (pure) ───────────────────────


def test_document_verification_is_structural(tmp_path: Path) -> None:
    srt = tmp_path / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    verified = verify_document_artifact(srt, kind="srt")
    assert verified.size_bytes > 0 and verified.sha256.startswith("sha256:")
    assert verified.prover == "structure:srt"

    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(VerificationFailed):
        verify_document_artifact(empty, kind="srt")

    wrong_marker = tmp_path / "not-srt.srt"
    wrong_marker.write_text("just prose, no cue\n", encoding="utf-8")
    with pytest.raises(VerificationFailed):
        verify_document_artifact(wrong_marker, kind="srt")

    with pytest.raises(VerificationFailed):
        verify_document_artifact(srt, kind="srt", expected_sha256="sha256:" + "0" * 64)
    with pytest.raises(VerificationFailed):
        verify_document_artifact(srt, kind="unknown-kind")
    assert set(DOCUMENT_MARKERS) >= {"srt", "otio"}


def test_traceability_reconstruction_fails_closed() -> None:
    good = {
        "success": True,
        "job_id": "job-9",
        "traceability": {
            "command_id": "cmd-key-timeline.trim",
            "job_id": "job-9",
            "project_id": "shot-key",
            "operation_id": "timeline.trim",
            "revision": 3,
            "state_hash": "sha256:" + "a" * 64,
            "logical_content_identity": "sha256:" + "b" * 64,
            "render_spec_hash": "sha256:" + "c" * 64,
            "physical_sha256": "sha256:" + "d" * 64,
            "size_bytes": 1234,
            "artifact_path": "/tmp/output.mp4",
            "verification": {"prover": "ffmpeg-stderr"},
        },
    }
    trace = ArtifactTrace.reconstruct("job-9", good)
    assert trace.job_id == "job-9" and trace.revision == 3

    with pytest.raises(ValueError):
        ArtifactTrace.reconstruct("job-9", {"success": False})
    with pytest.raises(ValueError):
        ArtifactTrace.reconstruct("job-9", {"success": True})
    truncated = {"success": True, "job_id": "job-9", "traceability": {"command_id": "c"}}
    with pytest.raises(ValueError):
        ArtifactTrace.reconstruct("job-9", truncated)
    mismatched = {**good, "job_id": "other-job"}
    with pytest.raises(ValueError):
        ArtifactTrace.reconstruct("job-9", mismatched)
    # the nested record disagreeing with the row is also a broken chain
    nested_mismatch = {
        **good,
        "traceability": {**good["traceability"], "job_id": "another-job"},
    }
    with pytest.raises(ValueError):
        ArtifactTrace.reconstruct("job-9", nested_mismatch)


def test_publication_identity_is_exact(tmp_path: Path) -> None:
    """Only the *same* identity may reuse a destination; a valid foreign
    artifact is refused, and an invalid one is quarantined (never deleted)."""
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"new bytes")
    destination = tmp_path / "output.mp4"
    identity = {"idempotency_key": "k1", "operation": "timeline.trim", "state_revision": 1}

    published = publish_artifact(
        staging_path=staged,
        destination=destination,
        identity=identity,
        verify=_verifier(),
    )
    assert not published.reused
    assert read_published_identity(destination)["identity"] == identity

    rewritten = tmp_path / "rewritten.mp4"
    rewritten.write_bytes(b"other bytes")
    with pytest.raises(DestinationOccupied):
        publish_artifact(
            staging_path=rewritten,
            destination=destination,
            identity={**identity, "state_revision": 2},
            verify=_verifier(),
        )
    assert destination.read_bytes() == b"new bytes"

    restaged = tmp_path / "restaged.mp4"
    restaged.write_bytes(b"new bytes")
    reused = publish_artifact(
        staging_path=restaged,
        destination=destination,
        identity=identity,
        verify=_verifier(),
    )
    assert reused.reused and reused.sha256 == published.sha256

    destination.write_bytes(b"")
    published_again = publish_artifact(
        staging_path=restaged,
        destination=destination,
        identity=identity,
        verify=_verifier(),
    )
    assert not published_again.reused
    quarantined = list(tmp_path.glob("output.mp4.invalid-*"))
    assert quarantined, "the invalid bytes are kept as evidence, never deleted"
    assert quarantined[0].read_bytes() == b""

    with pytest.raises(StagingNotPublishable):
        publish_artifact(
            staging_path=tmp_path / "missing.mp4",
            destination=destination,
            identity=identity,
            verify=_verifier(),
        )


def test_sidecar_is_atomic_and_readable(tmp_path: Path) -> None:
    """The identity record is written by replace (no partial JSON ever visible)."""
    staged = tmp_path / "staged.mp4"
    staged.write_bytes(b"bytes")
    destination = tmp_path / "output.mp4"
    publish_artifact(
        staging_path=staged,
        destination=destination,
        identity={"idempotency_key": "k"},
        verify=_verifier(),
    )
    record = json.loads(sidecar_path(destination).read_text(encoding="utf-8"))
    assert set(record) == {"identity", "artifact"}
    assert record["artifact"]["physical_artifact_sha256"].startswith("sha256:")
