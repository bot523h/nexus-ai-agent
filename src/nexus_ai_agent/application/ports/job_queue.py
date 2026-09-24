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
    registered for the job type). Failure paths:
    ``pending → failed`` (claim-time structural failure),
    ``processing → failed`` (execution/fail-closed),
    ``processing → verifying → failed`` (verification failure).
    ``processing/verifying → pending`` is recovery (cancellation/resume),
    never success. The historical values ``PROCESSING``/``COMPLETED`` keep
    their persisted spellings; the canonical aliases are ``RUNNING`` and
    ``SUCCEEDED`` (see ``jobs.lifecycle`` — no reasonless rename).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class JobQueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> JobStatus: ...

    async def get_result(self, job_id: str) -> dict[str, object] | None: ...
