"""Unit tests for the canonical job lifecycle contract (task-178).

Covers the pure pieces of the Job layer: the transition matrix (edges,
owners, terminality), the canonical aliases, the ``JobResult`` chain
assembly, the artifact-claim dialects of the real handlers, and every
verification invariant branch (missing / empty / size / sha / probe /
document structure / containment / expected path).

Queue-level enforcement (M1–M10, A/B) lives in
``tests/integration/test_job_lifecycle_queue.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.creative_verification import (
    EXPECTED_ARTIFACT_NAMES,
    creative_render_verifier,
)
from nexus_ai_agent.jobs.lifecycle import (
    PREFLIGHT_REQUIREMENTS,
    RUNNING,
    SIDE_EFFECT_BOUNDARY,
    SUCCEEDED,
    TRANSITIONS,
    JobResult,
    assert_transition,
    is_legal_transition,
    is_terminal,
)
from nexus_ai_agent.jobs.verification import (
    REASON_EMPTY,
    REASON_MISSING,
    REASON_NO_CLAIM,
    REASON_PROBE_FAILED,
    REASON_SHA_FORMAT,
    REASON_SHA_MISMATCH,
    REASON_SIZE_MISMATCH,
    REASON_UNEXPECTED_PATH,
    ArtifactClaim,
    ArtifactClaimError,
    verify_artifact,
)

SHA = "sha256:" + "ab" * 32


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------
def test_canonical_aliases_map_to_persisted_values() -> None:
    assert RUNNING is JobStatus.PROCESSING
    assert SUCCEEDED is JobStatus.COMPLETED


def test_every_mission_failure_path_exists() -> None:
    # PENDING → FAILED / RUNNING → FAILED / RUNNING → VERIFYING → FAILED /
    # VERIFYING → FAILED, plus recovery and the success eligibility edge.
    required = {
        (JobStatus.PENDING, JobStatus.FAILED),
        (JobStatus.PROCESSING, JobStatus.FAILED),
        (JobStatus.PROCESSING, JobStatus.VERIFYING),
        (JobStatus.VERIFYING, JobStatus.FAILED),
        (JobStatus.VERIFYING, JobStatus.COMPLETED),
        (JobStatus.PENDING, JobStatus.PROCESSING),
        (JobStatus.PROCESSING, JobStatus.PENDING),
        (JobStatus.VERIFYING, JobStatus.PENDING),
    }
    assert required <= set(TRANSITIONS)


def test_every_transition_names_owner_and_invariant() -> None:
    for edge, rule in TRANSITIONS.items():
        assert rule.owner, f"edge {edge} has no owner"
        assert len(rule.invariant) > 20, f"edge {edge} has no real invariant"


def test_terminal_states_have_no_outgoing_edges() -> None:
    assert is_terminal(JobStatus.COMPLETED)
    assert is_terminal(JobStatus.FAILED)
    assert not is_terminal(JobStatus.VERIFYING)
    for source, _ in TRANSITIONS:
        assert not is_terminal(source), f"terminal state {_source_name(source)} has an edge"


def _source_name(status: JobStatus) -> str:
    return status.value


def test_illegal_transition_raises() -> None:
    assert is_legal_transition(JobStatus.VERIFYING, JobStatus.COMPLETED)
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.COMPLETED, JobStatus.PROCESSING)
    with pytest.raises(ValueError, match="illegal job transition"):
        assert_transition(JobStatus.PENDING, JobStatus.COMPLETED)


def test_side_effect_boundary_lists_all_preflight_gates() -> None:
    for gate in (
        "authorization",
        "capability",
        "references",
        "idempotency",
        "revision/precondition",
        "job reservation",
    ):
        assert gate in PREFLIGHT_REQUIREMENTS
    assert "staging" in SIDE_EFFECT_BOUNDARY


# ---------------------------------------------------------------------------
# JobResult — the Result end of the chain
# ---------------------------------------------------------------------------
def test_job_result_assembles_from_durable_facts() -> None:
    verification = {
        "status": "verified",
        "sha256": SHA,
        "size_bytes": 1234,
        "probe": {"duration_us": 500_000, "width": 320, "height": 240, "has_audio": False},
        "logical_identity": {"project_id": "shot-key-1", "output_asset_id": "out"},
        "spec_identity": {"operation": "timeline.trim"},
        "physical_identity": {"path": "/tmp/ws/output.mp4", "size_bytes": 1234},
    }
    result = JobResult.from_parts(
        job_id="job-1",
        payload={"idempotency_key": "key-1", "operation": "trim"},
        result=None,
        attempt=2,
        execution_status="completed",
        verification=verification,
        error=None,
    )
    assert result.command_id == "cmd-key-1-trim"
    assert result.project_id == "shot-key-1"
    assert result.operation_id == "timeline.trim"
    assert result.attempt == 2
    assert result.verification_status == "verified"
    assert result.sha256 == SHA
    assert result.size_bytes == 1234
    assert result.probe is not None and result.probe["width"] == 320
    assert result.failure_reason is None
    as_dict = result.to_dict()
    assert as_dict["physical_identity"]["path"] == "/tmp/ws/output.mp4"


def test_job_result_from_failed_row_carries_failure_reason() -> None:
    result = JobResult.from_parts(
        job_id="job-2",
        payload={"idempotency_key": "k", "operation": "trim"},
        result=None,
        attempt=1,
        execution_status="failed",
        verification=None,
        error="verification_failed:sha256_mismatch",
    )
    assert result.verification_status == "not_applicable"
    assert result.failure_reason == "verification_failed:sha256_mismatch"
    assert result.sha256 is None and result.size_bytes is None


# ---------------------------------------------------------------------------
# ArtifactClaim dialects
# ---------------------------------------------------------------------------
def test_claim_reads_creative_render_dialect(tmp_path: Path) -> None:
    claim = ArtifactClaim.from_handler_result(
        {
            "artifact_path": str(tmp_path / "output.mp4"),
            "artifact_kind": "video",
            "sha256": SHA,
            "duration_us": 10,
        }
    )
    assert claim.kind == "video" and claim.sha256 == SHA


def test_claim_reads_document_kind_by_suffix(tmp_path: Path) -> None:
    srt = ArtifactClaim.from_handler_result(
        {
            "artifact_path": str(tmp_path / "captions.srt"),
            "artifact_kind": "document",
            "sha256": SHA,
        }
    )
    otio = ArtifactClaim.from_handler_result(
        {
            "artifact_path": str(tmp_path / "timeline.otio"),
            "artifact_kind": "document",
            "sha256": SHA,
        }
    )
    assert srt.kind == "srt" and otio.kind == "otio"


def test_claim_success_without_artifact_is_a_contract_error() -> None:
    with pytest.raises(ArtifactClaimError) as excinfo:
        ArtifactClaim.from_handler_result({"success": True})
    assert excinfo.value.args[0] == REASON_NO_CLAIM


# ---------------------------------------------------------------------------
# verification invariants (pure, filesystem-backed)
# ---------------------------------------------------------------------------
def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

    return sha256_file(path)


def test_verify_missing_and_empty(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost.bin"
    outcome = verify_artifact(
        ArtifactClaim(
            path=ghost,
            kind="binary",
            sha256=SHA,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        )
    )
    assert not outcome.ok and outcome.reason_code == REASON_MISSING

    empty = tmp_path / "empty.bin"
    sha = _write(empty, b"")
    outcome = verify_artifact(
        ArtifactClaim(
            path=empty,
            kind="binary",
            sha256=sha,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        )
    )
    assert not outcome.ok and outcome.reason_code == REASON_EMPTY


def test_verify_sha_mismatch_and_size_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    real_sha = _write(target, b"real bytes")
    lie = ArtifactClaim(
        path=target,
        kind="binary",
        sha256=SHA,
        size_bytes=None,
        duration_us=None,
        operation="op",
        output_asset_id="a",
    )
    outcome = verify_artifact(lie)
    assert not outcome.ok and outcome.reason_code == REASON_SHA_MISMATCH

    sized = ArtifactClaim(
        path=target,
        kind="binary",
        sha256=real_sha,
        size_bytes=999,
        duration_us=None,
        operation="op",
        output_asset_id="a",
    )
    outcome = verify_artifact(sized)
    assert not outcome.ok and outcome.reason_code == REASON_SIZE_MISMATCH


def test_verify_rejects_malformed_sha_claim(tmp_path: Path) -> None:
    target = tmp_path / "b.bin"
    _write(target, b"bytes")
    outcome = verify_artifact(
        ArtifactClaim(
            path=target,
            kind="binary",
            sha256="deadbeef",
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        )
    )
    assert not outcome.ok and outcome.reason_code == REASON_SHA_FORMAT


def test_verify_expected_path_and_containment(tmp_path: Path) -> None:
    wrong = tmp_path / "other.bin"
    sha = _write(wrong, b"bytes")
    outcome = verify_artifact(
        ArtifactClaim(
            path=wrong,
            kind="binary",
            sha256=sha,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        ),
        expected_path=tmp_path / "expected.bin",
    )
    assert not outcome.ok and outcome.reason_code == REASON_UNEXPECTED_PATH

    outside = tmp_path.parent / "outside.bin"
    sha_out = _write(outside, b"outside")
    outcome = verify_artifact(
        ArtifactClaim(
            path=outside,
            kind="binary",
            sha256=sha_out,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        ),
        expected_root=tmp_path,
    )
    assert not outcome.ok

    inside = tmp_path / "inside.bin"
    sha_in = _write(inside, b"inside")
    outcome = verify_artifact(
        ArtifactClaim(
            path=inside,
            kind="binary",
            sha256=sha_in,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        ),
        expected_root=tmp_path,
    )
    assert outcome.ok and outcome.summary["status"] == "verified"


def test_verify_media_requires_probe_evidence(tmp_path: Path) -> None:
    fake_video = tmp_path / "output.mp4"
    sha = _write(fake_video, b"this is not a video")

    def _boom(path: Path) -> dict[str, object]:
        raise RuntimeError("no moov atom")

    outcome = verify_artifact(
        ArtifactClaim(
            path=fake_video,
            kind="video",
            sha256=sha,
            size_bytes=None,
            duration_us=None,
            operation="op",
            output_asset_id="a",
        ),
        probe=_boom,
    )
    assert not outcome.ok and outcome.reason_code == REASON_PROBE_FAILED


def test_verify_document_structure_srt_and_otio(tmp_path: Path) -> None:
    good_srt = tmp_path / "captions.srt"
    sha = _write(good_srt, b"1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    outcome = verify_artifact(
        ArtifactClaim(
            path=good_srt,
            kind="srt",
            sha256=sha,
            size_bytes=None,
            duration_us=None,
            operation="caption.transcribe",
            output_asset_id="c",
        )
    )
    assert outcome.ok

    truncated_srt = tmp_path / "broken.srt"
    sha_t = _write(truncated_srt, b"1\n00:00:0")
    outcome = verify_artifact(
        ArtifactClaim(
            path=truncated_srt,
            kind="srt",
            sha256=sha_t,
            size_bytes=None,
            duration_us=None,
            operation="caption.transcribe",
            output_asset_id="c",
        )
    )
    assert not outcome.ok and outcome.reason_code == REASON_PROBE_FAILED

    bad_otio = tmp_path / "timeline.otio"
    sha_b = _write(bad_otio, b'{"tracks": [')  # truncated JSON
    outcome = verify_artifact(
        ArtifactClaim(
            path=bad_otio,
            kind="otio",
            sha256=sha_b,
            size_bytes=None,
            duration_us=None,
            operation="delivery.export_otio",
            output_asset_id="t",
        )
    )
    assert not outcome.ok and outcome.reason_code == REASON_PROBE_FAILED

    good_otio = tmp_path / "ok.otio"
    sha_g = _write(good_otio, json.dumps({"otio": {"tracks": []}}).encode())
    outcome = verify_artifact(
        ArtifactClaim(
            path=good_otio,
            kind="otio",
            sha256=sha_g,
            size_bytes=None,
            duration_us=None,
            operation="delivery.export_otio",
            output_asset_id="t",
        )
    )
    assert outcome.ok


# ---------------------------------------------------------------------------
# creative verifier: operation → expected artifact binding
# ---------------------------------------------------------------------------
def test_creative_verifier_binds_expected_artifact_names(tmp_path: Path) -> None:
    assert EXPECTED_ARTIFACT_NAMES["delivery.export_otio"] == "timeline.otio"
    assert EXPECTED_ARTIFACT_NAMES["caption.transcribe"] == "captions.srt"

    workspace = tmp_path / "creative_x"
    workspace.mkdir()
    # a valid artifact at the WRONG name for the claimed operation
    stray = workspace / "output.mp4"
    sha = _write(stray, b"bytes")
    outcome = creative_render_verifier(
        {"workspace_dir": str(workspace), "idempotency_key": "k"},
        {
            "success": True,
            "artifact_path": str(stray),
            "artifact_kind": "otio",
            "sha256": sha,
            "operation": "delivery.export_otio",
            "output_asset_id": "timeline",
        },
    )
    assert not outcome.ok and outcome.reason_code == REASON_UNEXPECTED_PATH


def test_creative_verifier_typed_user_failure_is_not_applicable() -> None:
    outcome = creative_render_verifier(
        {"workspace_dir": "/tmp/x"},
        {"success": False, "error_code": "unsupported_operation", "operation": "x"},
    )
    assert outcome.ok and outcome.summary["status"] == "not_applicable"


def test_creative_verifier_success_without_claim_fails_closed() -> None:
    outcome = creative_render_verifier(
        {"workspace_dir": "/tmp/x", "idempotency_key": "k"}, {"success": True}
    )
    assert not outcome.ok and outcome.reason_code == REASON_NO_CLAIM
