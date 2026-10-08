"""Execution Contract unit tests (NEXUS V1).

Pure, provider-neutral: no queue, no database, no filesystem, no provider SDK.
Each test names the invariant it proves (see the V1 invariant table):

    I1  one business request  -> one authoritative Nexus job identity
    I2  a Nexus attempt       != a provider retry
    I3  only the current fencing token may authoritatively complete
    I4  a stale attempt can never produce authoritative completion
    I5  provider_run_id is never authority
    I6  verification is independent from execution
    I7  UNKNOWN is not automatically FAILED
    I8  idempotency remains Nexus-owned
    I9  NativeLocalBackend creates no second queue
    I10 success notification occurs only after the authoritative commit
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.execution.contract import (
    ExecutionBackend,
    ExecutionContext,
    ExecutionFailure,
    ExecutionIdentity,
    ExecutionObservation,
    ExecutionPolicy,
    ExecutionRequest,
    ExecutionResult,
    FailureDisposition,
    ObservationState,
    RetryPolicy,
    disposition_from_failure_class,
    disposition_from_status,
    failure_from_status,
    observation_state_from_status,
    stale_failure,
    unknown_failure,
)
from nexus_ai_agent.jobs.failure_semantics import FailureClass


# --------------------------------------------------------------------------- #
# I2 / I5 — identity separation
# --------------------------------------------------------------------------- #
def test_identity_keeps_every_identity_distinct() -> None:
    """I2/I5: request, job, attempt, fencing, worker, backend, provider run."""
    identity = ExecutionIdentity(
        request_id="req-1",
        idempotency_key="idem-1",
        job_id="job-1",
        attempt_id="job-1#1",
        fencing_token=1,
        worker_id="worker-a",
        backend="native_local",
        provider_run_id="provider-run-xyz",
    )
    assert identity.request_id == "req-1"
    assert identity.job_id == "job-1"
    assert identity.attempt_id == "job-1#1"
    assert identity.fencing_token == 1
    assert identity.backend == "native_local"
    assert identity.provider_run_id == "provider-run-xyz"
    assert identity.has_fencing_token


def test_provider_run_id_is_optional_and_never_a_fencing_token() -> None:
    """I5: a provider run id is an observation, never authority."""
    identity = ExecutionIdentity(
        request_id="req-1", idempotency_key="idem-1", job_id="job-1", provider_run_id="run-9"
    )
    assert identity.provider_run_id == "run-9"
    assert identity.fencing_token is None
    assert not identity.has_fencing_token


def test_with_attempt_returns_a_new_bound_copy() -> None:
    """I3: binding an attempt never mutates the original identity."""
    base = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="j")
    bound = base.with_attempt(attempt_id="j#2", fencing_token=2)
    assert base.attempt_id is None and base.fencing_token is None
    assert bound.attempt_id == "j#2" and bound.fencing_token == 2
    assert bound.request_id == base.request_id and bound.job_id == base.job_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_id": "", "idempotency_key": "k", "job_id": "j"},
        {"request_id": "r", "idempotency_key": " ", "job_id": "j"},
        {"request_id": "r", "idempotency_key": "k", "job_id": ""},
        {"request_id": "r", "idempotency_key": "k", "job_id": "j", "backend": ""},
        {"request_id": "r", "idempotency_key": "k", "job_id": "j", "fencing_token": 0},
        {"request_id": "r", "idempotency_key": "k", "job_id": "j", "fencing_token": -1},
    ],
)
def test_identity_rejects_invalid_tokens(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ExecutionIdentity(**kwargs)  # type: ignore[arg-type]


def test_identity_is_frozen() -> None:
    identity = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="j")
    with pytest.raises(FrozenInstanceError):
        identity.job_id = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# I7 — failure dispositions; UNKNOWN/STALE are never terminal business failures
# --------------------------------------------------------------------------- #
def test_only_non_retryable_is_a_terminal_business_failure() -> None:
    """I7: UNKNOWN and STALE (and RETRYABLE/CANCELLED) are never terminal."""
    assert FailureDisposition.NON_RETRYABLE.is_terminal_business_failure
    for disposition in (
        FailureDisposition.RETRYABLE,
        FailureDisposition.UNKNOWN,
        FailureDisposition.CANCELLED,
        FailureDisposition.STALE,
    ):
        assert not disposition.is_terminal_business_failure, disposition


def test_unknown_and_stale_failures_are_non_terminal() -> None:
    """I7: an unresolved outcome must be reconciled, never recorded as failed."""
    unknown = unknown_failure("status_unreadable")
    stale = stale_failure("stale_execution")
    assert unknown.disposition is FailureDisposition.UNKNOWN
    assert stale.disposition is FailureDisposition.STALE
    assert not unknown.is_terminal_business_failure
    assert not stale.is_terminal_business_failure


def test_failure_requires_a_non_empty_code() -> None:
    with pytest.raises(ValueError):
        ExecutionFailure(disposition=FailureDisposition.RETRYABLE, code="")


def test_disposition_bridges_the_single_business_taxonomy() -> None:
    """The contract projects the existing FailureClass; it is not a second one."""
    assert disposition_from_failure_class(FailureClass.RETRYABLE) is FailureDisposition.RETRYABLE
    assert disposition_from_failure_class(FailureClass.TERMINAL) is FailureDisposition.NON_RETRYABLE


def test_disposition_from_status_maps_only_failure_statuses() -> None:
    assert disposition_from_status(JobStatus.FAILED_RETRYABLE) is FailureDisposition.RETRYABLE
    assert disposition_from_status(JobStatus.FAILED_TERMINAL) is FailureDisposition.NON_RETRYABLE
    non_failure = (
        JobStatus.PENDING,
        JobStatus.PROCESSING,
        JobStatus.VERIFYING,
        JobStatus.COMPLETED,
    )
    for status in non_failure:
        assert disposition_from_status(status) is None


def test_failure_from_status_is_none_for_non_failure_status() -> None:
    assert failure_from_status(JobStatus.COMPLETED, code="x") is None
    failure = failure_from_status(
        JobStatus.FAILED_TERMINAL, code="typed_failure:bad", message="bad"
    )
    assert failure is not None and failure.disposition is FailureDisposition.NON_RETRYABLE


# --------------------------------------------------------------------------- #
# Retry policy — a Nexus re-attempt, never a provider retry
# --------------------------------------------------------------------------- #
def test_retry_policy_defaults_and_allows_retry() -> None:
    policy = RetryPolicy()
    assert policy.max_attempts == 1
    assert policy.allows_retry(FailureDisposition.RETRYABLE)
    assert not policy.allows_retry(FailureDisposition.NON_RETRYABLE)
    assert not policy.allows_retry(FailureDisposition.STALE)


def test_retry_policy_rejects_invalid_combinations() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_seconds=-1.0)
    with pytest.raises(ValueError):
        RetryPolicy(retry_on=frozenset({FailureDisposition.NON_RETRYABLE}))


def test_execution_policy_rejects_non_positive_timeout() -> None:
    assert ExecutionPolicy().requires_verification is True
    with pytest.raises(ValueError):
        ExecutionPolicy(timeout_seconds=0)


# --------------------------------------------------------------------------- #
# Request / context — immutability of caller-owned mappings
# --------------------------------------------------------------------------- #
def test_request_freezes_payload() -> None:
    source = {"a": 1}
    request = ExecutionRequest(job_type="t", idempotency_key="k", payload=source)
    source["a"] = 999  # mutating the caller's dict must not change the request
    assert request.payload["a"] == 1
    with pytest.raises(TypeError):
        request.payload["b"] = 2  # type: ignore[index]


def test_request_rejects_empty_tokens() -> None:
    with pytest.raises(ValueError):
        ExecutionRequest(job_type="", idempotency_key="k")
    with pytest.raises(ValueError):
        ExecutionRequest(job_type="t", idempotency_key=" ")


def test_context_freezes_metadata() -> None:
    context = ExecutionContext(
        identity=ExecutionIdentity(request_id="r", idempotency_key="k", job_id="j"),
        policy=ExecutionPolicy(),
        submitted_at="2026-01-01T00:00:00+00:00",
        metadata={"trace": "abc"},
    )
    assert context.metadata["trace"] == "abc"
    with pytest.raises(TypeError):
        context.metadata["x"] = "y"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Observation states — projection of the durable status
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "state"),
    [
        (JobStatus.PENDING, ObservationState.PENDING),
        (JobStatus.PROCESSING, ObservationState.PROCESSING),
        (JobStatus.VERIFYING, ObservationState.VERIFYING),
        (JobStatus.COMPLETED, ObservationState.SUCCEEDED),
        (JobStatus.FAILED_RETRYABLE, ObservationState.FAILED),
        (JobStatus.FAILED_TERMINAL, ObservationState.FAILED),
    ],
)
def test_observation_state_projects_every_status(
    status: JobStatus, state: ObservationState
) -> None:
    assert observation_state_from_status(status) is state


def test_observation_terminal_states() -> None:
    assert ObservationState.SUCCEEDED.is_terminal
    assert ObservationState.FAILED.is_terminal
    assert ObservationState.CANCELLED.is_terminal
    assert not ObservationState.PENDING.is_terminal
    assert not ObservationState.PROCESSING.is_terminal
    assert not ObservationState.VERIFYING.is_terminal
    assert not ObservationState.UNKNOWN.is_terminal


def test_observation_freezes_result() -> None:
    identity = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="j")
    observation = ExecutionObservation(
        identity=identity, state=ObservationState.SUCCEEDED, result={"who": "w"}
    )
    assert observation.result == {"who": "w"}
    with pytest.raises(TypeError):
        observation.result["x"] = 1  # type: ignore[index]


def test_result_carries_independent_verification_evidence() -> None:
    """I6: an execution result is never itself verified evidence."""
    identity = ExecutionIdentity(request_id="r", idempotency_key="k", job_id="j")
    result = ExecutionResult(
        identity=identity,
        state=ObservationState.SUCCEEDED,
        result={"artifact_path": "a"},
        verification={"verified": True, "sha256": "abc"},
    )
    assert result.verification == {"verified": True, "sha256": "abc"}
    with pytest.raises(TypeError):
        result.verification["verified"] = False  # type: ignore[index]


# --------------------------------------------------------------------------- #
# The backend contract — four verbs, never execute
# --------------------------------------------------------------------------- #
def test_backend_protocol_exposes_exactly_four_verbs() -> None:
    """No combined ``execute``: intent must never collapse into authority."""
    assert hasattr(ExecutionBackend, "submit")
    assert hasattr(ExecutionBackend, "observe")
    assert hasattr(ExecutionBackend, "cancel")
    assert hasattr(ExecutionBackend, "reconcile")
    assert not hasattr(ExecutionBackend, "execute")


def test_backend_protocol_is_runtime_checkable_shape() -> None:
    class _Backend:
        async def submit(self, request: ExecutionRequest) -> ExecutionIdentity: ...
        async def observe(self, identity: ExecutionIdentity) -> ExecutionObservation: ...
        async def cancel(self, identity: ExecutionIdentity) -> bool: ...
        async def reconcile(self, identity: ExecutionIdentity) -> ExecutionObservation: ...

    assert isinstance(_Backend(), ExecutionBackend)
