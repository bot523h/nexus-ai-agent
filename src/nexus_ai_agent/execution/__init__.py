"""The provider-neutral execution core (NEXUS V1).

Two pieces, one authority:

* :mod:`nexus_ai_agent.execution.contract` — the pure, provider-neutral
  Execution Contract (identity, failure, policy, request, observation, result,
  and the four-verb :class:`ExecutionBackend` protocol).  It imports no provider
  SDK and no adapter.
* :mod:`nexus_ai_agent.execution.staging` — attempt-scoped staging isolation,
  the filesystem half of the two-phase publication the queue already enforces.

The concrete backend lives in ``adapters/native_local_backend.py`` and
delegates to the single execution authority (``InProcessJobQueue``).
"""

from __future__ import annotations

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
from nexus_ai_agent.execution.staging import (
    AttemptStaging,
    StagingBoundaryError,
)

__all__ = [
    "AttemptStaging",
    "ExecutionBackend",
    "ExecutionContext",
    "ExecutionFailure",
    "ExecutionIdentity",
    "ExecutionObservation",
    "ExecutionPolicy",
    "ExecutionRequest",
    "ExecutionResult",
    "FailureDisposition",
    "ObservationState",
    "RetryPolicy",
    "StagingBoundaryError",
    "disposition_from_failure_class",
    "disposition_from_status",
    "failure_from_status",
    "observation_state_from_status",
    "stale_failure",
    "unknown_failure",
]
