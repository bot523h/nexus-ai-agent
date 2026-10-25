"""Failure-classification contract (task-181, GAP-A/GAP-B) — unit tests.

The classification table of ``jobs/failure_semantics.py`` is the durable
answer to "retryable or terminal?" for every way a job can fail.  Each row of
the mission's failure list gets an explicit, named test with its rationale
encoded in the assertion (RETRYABLE iff the world can change to make the
identical request succeed).
"""

from __future__ import annotations

import errno

import pytest

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.jobs.failure_semantics import (
    TYPED_CODE_CLASSES,
    FailureClass,
    classify_exception,
    classify_typed_code,
    classify_verification_reason,
    failure_status,
    is_typed_user_failure,
    parse_typed_failure_error,
    typed_failure_error,
)
from nexus_ai_agent.jobs.lifecycle import (
    FAILURE_STATES,
    TRANSITIONS,
    is_failure,
    is_terminal,
    parse_job_status,
)


class _TypedDialectError(ValueError):
    """Duck-typed like CreativeRenderError / SlideshowJobError (``.code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# The classification table — one named row per failure family
# ---------------------------------------------------------------------------


def test_temporary_io_is_retryable_the_world_can_change() -> None:
    for number in (errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.ENOSPC):
        exc = OSError(number, "transient")
        assert classify_exception(exc) is FailureClass.RETRYABLE
    assert classify_exception(TimeoutError("slow")) is FailureClass.RETRYABLE
    assert classify_exception(ConnectionError("reset")) is FailureClass.RETRYABLE


def test_dependency_unavailable_is_retryable_a_deploy_can_fix_it() -> None:
    assert classify_exception(ImportError("no pypdf")) is FailureClass.RETRYABLE
    assert classify_exception(ModuleNotFoundError("no pypdf")) is FailureClass.RETRYABLE
    # typed dependency codes (creative / slideshow dialects)
    assert classify_typed_code("ffmpeg_unavailable") is FailureClass.RETRYABLE
    assert classify_typed_code("caption_profile_unavailable") is FailureClass.RETRYABLE


def test_worker_crash_is_retryable_conservative_bounded_default() -> None:
    assert classify_exception(RuntimeError("boom")) is FailureClass.RETRYABLE
    assert classify_exception(Exception("?")) is FailureClass.RETRYABLE  # noqa: B017


def test_invalid_input_is_terminal_a_retry_repeats_the_refusal() -> None:
    assert classify_exception(ValueError("bad args")) is FailureClass.TERMINAL
    assert classify_exception(TypeError("bad shape")) is FailureClass.TERMINAL
    assert classify_exception(KeyError("missing")) is FailureClass.TERMINAL
    assert classify_typed_code("invalid_request") is FailureClass.TERMINAL
    assert classify_typed_code("media_missing") is FailureClass.TERMINAL


def test_unsupported_operation_is_terminal_a_deploy_must_change() -> None:
    assert classify_exception(NotImplementedError("no lane")) is FailureClass.TERMINAL
    assert classify_typed_code("unsupported_operation") is FailureClass.TERMINAL


def test_artifact_verification_failure_split_by_cause() -> None:
    # measurement disagreements ⇒ transient damage — one clean re-execution
    for reason in (
        "missing_artifact",
        "empty_artifact",
        "sha256_mismatch",
        "size_mismatch",
        "duration_mismatch",
        "probe_failed",
        "verifier_crashed",
        "publish_failed",
        "reprobe_failed",
    ):
        assert classify_verification_reason(reason) is FailureClass.RETRYABLE, reason
    # deterministic handler defects ⇒ the identical retry repeats the lie
    for reason in (
        "success_without_artifact_claim",
        "invalid_sha256_claim",
        "outside_expected_root",
        "unexpected_artifact_path",
        "typed_user_failure",
    ):
        assert classify_verification_reason(reason) is FailureClass.TERMINAL, reason


def test_permission_error_is_terminal_same_credentials_fail_identically() -> None:
    assert classify_exception(PermissionError("denied")) is FailureClass.TERMINAL
    assert classify_exception(OSError(errno.EACCES, "denied")) is FailureClass.TERMINAL
    assert classify_exception(OSError(errno.EPERM, "denied")) is FailureClass.TERMINAL


def test_corrupt_output_is_retryable_a_clean_rerun_may_fix_it() -> None:
    # corrupt output discovered at verification (probe of the artifact)
    assert classify_verification_reason("probe_failed") is FailureClass.RETRYABLE

    # corrupt output discovered at execution (the handler's own post-encode
    # probe raises a RenderError(RuntimeError) — crash-class ⇒ RETRYABLE)
    class RenderError(RuntimeError):
        pass

    assert classify_exception(RenderError("garbage")) is FailureClass.RETRYABLE


def test_typed_render_failed_is_terminal_same_inputs_fail_identically() -> None:
    assert classify_typed_code("render_failed") is FailureClass.TERMINAL
    assert classify_exception(_TypedDialectError("render_failed")) is FailureClass.TERMINAL


def test_unknown_typed_code_is_terminal_fail_closed_toward_visibility() -> None:
    assert classify_typed_code("brand_new_code") is FailureClass.TERMINAL
    assert classify_verification_reason("brand_new_reason") is FailureClass.TERMINAL


def test_every_open_vocabulary_code_is_classified() -> None:
    # creative (creative/render_jobs.ERROR_CODES) ∪ slideshow
    # (creative/slideshow/worker_adapter.ERROR_CODES) — a code without a
    # classification row is contract drift.
    for code in (
        "invalid_request",
        "media_missing",
        "unsupported_operation",
        "render_failed",
        "caption_profile_unavailable",
        "ffmpeg_unavailable",
        "unusable_image",
        "image_generation_failed",
        "internal",
    ):
        assert code in TYPED_CODE_CLASSES, code


# ---------------------------------------------------------------------------
# Typed user-failure dialect + durable mapping
# ---------------------------------------------------------------------------


def test_typed_user_failure_dialect_detection() -> None:
    assert is_typed_user_failure({"success": False, "error_code": "render_failed"})
    assert not is_typed_user_failure({"success": True})
    assert not is_typed_user_failure({"success": False})  # no code ⇒ not the dialect
    assert not is_typed_user_failure({"success": False, "error_code": ""})
    assert not is_typed_user_failure({"error_code": "render_failed"})


def test_typed_failure_error_round_trip() -> None:
    assert typed_failure_error("render_failed") == "typed_failure:render_failed"
    assert parse_typed_failure_error("typed_failure:render_failed") == "render_failed"
    assert parse_typed_failure_error("verification_failed:empty_artifact") is None
    assert parse_typed_failure_error(None) is None


def test_failure_status_maps_class_to_durable_state() -> None:
    assert failure_status(FailureClass.RETRYABLE) is JobStatus.FAILED_RETRYABLE
    assert failure_status(FailureClass.TERMINAL) is JobStatus.FAILED_TERMINAL
    assert is_failure(JobStatus.FAILED_RETRYABLE)
    assert is_failure(JobStatus.FAILED_TERMINAL)
    assert not is_failure(JobStatus.COMPLETED)


# ---------------------------------------------------------------------------
# 6-state taxonomy invariants (GAP-B)
# ---------------------------------------------------------------------------


def test_six_states_total_with_two_failure_states() -> None:
    assert set(JobStatus) == {
        JobStatus.PENDING,
        JobStatus.PROCESSING,
        JobStatus.VERIFYING,
        JobStatus.COMPLETED,
        JobStatus.FAILED_RETRYABLE,
        JobStatus.FAILED_TERMINAL,
    }
    assert FAILURE_STATES == {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}


def test_failure_states_are_terminal_and_have_no_outgoing_edges() -> None:
    # SCHEDULER NOT IMPLEMENTED: the reserved failed_retryable → pending
    # retry edge is deliberately absent (fail-closed).
    assert is_terminal(JobStatus.FAILED_RETRYABLE)
    assert is_terminal(JobStatus.FAILED_TERMINAL)
    for source, _target in TRANSITIONS:
        assert source not in FAILURE_STATES
    with pytest.raises(ValueError, match="illegal job transition"):
        from nexus_ai_agent.jobs.lifecycle import assert_transition

        assert_transition(JobStatus.FAILED_RETRYABLE, JobStatus.PENDING)


def test_legacy_failed_rows_read_back_deterministically() -> None:
    # pre-task-181 sidecar rows carried one undifferentiated "failed"
    assert parse_job_status("failed") is JobStatus.FAILED_TERMINAL
    assert parse_job_status("completed") is JobStatus.COMPLETED
    assert parse_job_status("failed_retryable") is JobStatus.FAILED_RETRYABLE
    with pytest.raises(ValueError):
        parse_job_status("bogus")
