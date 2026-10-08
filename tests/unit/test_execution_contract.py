"""Contract tests for the provider-neutral Execution Fabric (task-255).

These prove the *seam*, not a provider: identity separation, provider-retry vs
Nexus-fencing, stale-result refusal, missing-result fail-closure, and staging
containment. They run with no provider and no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.integrations.execution import (
    BackendKind,
    ExecutionAttempt,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionRequest,
    ExecutionResult,
    OutcomeState,
    assert_staged,
    identity_matches,
    is_stale,
    reconcile_result,
    staging_dir_for,
    state_from_provider,
)


def _identity(**over: object) -> ExecutionIdentity:
    base = dict(
        request_id="req-1",
        idempotency_key="idem-1",
        job_id="job-1",
        try_id="job-1::try-1",
        fencing_token=1,
        backend=BackendKind.HATCHET,
    )
    base.update(over)
    return ExecutionIdentity(**base)  # type: ignore[arg-type]


def test_identity_keeps_every_axis_distinct() -> None:
    ident = _identity(provider_run_id="prov-run-1")
    meta = ident.to_metadata()
    # request_key/idempotency/job/try/fencing/backend are all present and distinct.
    assert meta["request_id"] == "req-1"
    assert meta["idempotency_key"] == "idem-1"
    assert meta["job_id"] == "job-1"
    assert meta["try_id"] == "job-1::try-1"
    assert meta["fencing_token"] == 1
    assert meta["backend"] == "hatchet"
    # provider_run_id is evidence-only: it is not part of the authority metadata.
    assert "provider_run_id" not in meta


def test_provider_run_id_is_evidence_and_survives_a_copy() -> None:
    ident = _identity()
    carried = ident.with_provider_run("prov-run-9")
    assert carried.provider_run_id == "prov-run-9"
    assert carried.job_id == ident.job_id
    assert identity_matches(ident, carried)  # provider handle not an authority key


def test_fencing_token_must_be_positive() -> None:
    with pytest.raises(ValueError):
        _identity(fencing_token=0)


def test_provider_retry_is_not_a_nexus_try() -> None:
    """PHASE E: many provider attempts share ONE Nexus try/fencing identity."""
    ident = _identity(fencing_token=4)
    a1 = ExecutionAttempt(identity=ident, provider_attempt=1)
    a3 = ExecutionAttempt(identity=ident, provider_attempt=3)
    assert a1.identity.fencing_token == a3.identity.fencing_token == 4
    assert a1.provider_attempt != a3.provider_attempt


def test_identity_matches_on_job_and_fence_only() -> None:
    a = _identity(fencing_token=2)
    b = _identity(fencing_token=2, provider_run_id="other")
    c = _identity(fencing_token=3)
    assert identity_matches(a, b)
    assert not identity_matches(a, c)


def test_stale_result_is_refused() -> None:
    expected = _identity(fencing_token=2)
    superseded = _identity(fencing_token=1)
    result = ExecutionResult(
        identity=superseded,
        attempt=ExecutionAttempt(identity=superseded, provider_attempt=1),
        state=OutcomeState.SUCCEEDED,
    )
    assert is_stale(expected, result)


def test_missing_provider_result_is_unknown_fail_closed() -> None:
    expected = _identity()
    verdict = reconcile_result(expected, None)
    assert isinstance(verdict, ExecutionFailure)
    assert verdict.state is OutcomeState.UNKNOWN
    assert verdict.retryable is True
    assert not isinstance(verdict, ExecutionResult)


def test_unknown_provider_status_maps_to_unknown() -> None:
    assert state_from_provider("weird-new-state") is OutcomeState.UNKNOWN
    assert state_from_provider(None) is OutcomeState.UNKNOWN
    assert state_from_provider("completed") is OutcomeState.SUCCEEDED
    assert state_from_provider("FAILED") is OutcomeState.FAILED
    assert state_from_provider("cancelled") is OutcomeState.CANCELLED


def test_reconcile_success_is_a_claim_not_a_commit() -> None:
    expected = _identity()
    result = reconcile_result(
        expected,
        {
            "status": "completed",
            "witness": {"success": True},
            "staged_artifact_path": "/tmp/x/output.mp4",
            "artifact_sha256": "abc",
            "provider_attempt": 2,
        },
    )
    assert isinstance(result, ExecutionResult)
    assert result.is_success_claim  # still only a *claim* for the verifier
    assert result.attempt.provider_attempt == 2
    assert result.identity.fencing_token == expected.fencing_token


def test_staging_is_per_attempt_and_contained(tmp_path: Path) -> None:
    ident = _identity()
    d1 = staging_dir_for(tmp_path, ident, provider_attempt=1)
    d2 = staging_dir_for(tmp_path, ident, provider_attempt=2)
    assert d1 != d2
    assert d1.is_dir() and d2.is_dir()
    assert try_id_slug(ident.try_id) in d1.name


def test_assert_staged_refuses_paths_outside_staging(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    req = ExecutionRequest(
        identity=_identity(),
        operation="creative_render",
        payload={},
        staging_dir=str(staging),
    )
    inside = staging / "output.mp4"
    inside.write_bytes(b"x")
    assert assert_staged(req, inside) == inside.resolve()
    outside = tmp_path / "escape.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        assert_staged(req, outside)


def try_id_slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)
