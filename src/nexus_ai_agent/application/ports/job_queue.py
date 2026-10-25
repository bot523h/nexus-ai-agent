"""In-process background-job contract for the Modular Monolith."""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class JobStatus(str, Enum):
    """Durable lifecycle states for an application-owned job.

    Canonical chain: ``Command → Job → Runtime Execution → Artifact
    Verification → Result`` (full contract:
    ``nexus_ai_agent.jobs.lifecycle`` and ``docs/architecture/JOB_LIFECYCLE.md``).

    ``VERIFYING`` is the explicit phase between execution and success:
    execution success ≠ job success — a job may reach ``COMPLETED`` only
    after its artifact verification succeeded (where a verifier is
    registered for the job type) and its artifact was published atomically.

    Failure paths (task-181, GAP-A/GAP-B) always end in one of the two
    failure states — never in ``COMPLETED``:

    ``pending → failed_*``           claim-time structural failure,
    ``processing → failed_*``        typed user failure / execution
                                     (fail-closed conversion),
    ``processing → verifying →
    failed_*``                       verification refusal / verifier crash /
                                     publication failure.

    The split records the classifier's verdict
    (``jobs/failure_semantics.FailureClass``): ``FAILED_RETRYABLE`` is
    retry-eligible (the world can change), ``FAILED_TERMINAL`` must not be
    retried (the identical request must fail again).  There is no retry
    scheduler in this repository: both failure states are terminal as
    implemented; the reserved ``failed_retryable → pending`` scheduler edge
    is deliberately outside the transition matrix (fail-closed).

    ``processing/verifying → pending`` is recovery (cancellation/resume),
    never success.  Persisted spellings of the non-failure states are
    unchanged; rows written by pre-task-181 code as ``"failed"`` read back
    as ``FAILED_TERMINAL`` (``jobs.lifecycle.parse_job_status`` — the old
    contract had one undifferentiated terminal failure).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"


class JobQueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> JobStatus: ...

    async def get_result(self, job_id: str) -> dict[str, object] | None: ...
