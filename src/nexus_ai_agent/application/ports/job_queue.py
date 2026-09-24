"""In-process background-job contract for the Modular Monolith."""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class JobStatus(str, Enum):
    """Durable lifecycle states for an application-owned job.

    Canonical lifecycle (the full contract with entry/exit conditions, side
    effects and retry semantics lives in
    :mod:`nexus_ai_agent.application.job_lifecycle`)::

        PENDING ──► PROCESSING ──► COMPLETED        (the only success)
           │            │
           └────────────┴────────► FAILED_RETRYABLE | TERMINAL_FAILED | FAILED

    * ``PENDING`` — admitted row, no process owns it yet.
    * ``PROCESSING`` — the RUNNING state: a process owns the row and the handler
      is executing. The machine starts its side effects only after this mark.
    * ``COMPLETED`` — **success**: it may only be persisted for a result whose
      artifact was verified (see ``docs/architecture/JOB_LIFECYCLE_CONTRACT.md``).
      Execution success alone is not job success.
    * ``FAILED_RETRYABLE`` — classified transient failure (missing engine,
      crashed encode, failed verification); a retry is meaningful.
    * ``TERMINAL_FAILED`` — classified permanent failure (bad payload,
      unsupported operation, unattributable destination); retrying cannot help.
    * ``FAILED`` — *unclassified* failure: the handler crashed without declaring
      a class, so the class is unknown and the row is **not** advertised as
      retryable. Retained for rows persisted by earlier versions.

    Values are stable strings: they are persisted in SQLite and are part of the
    durable contract (``FAILED`` keeps its historical value forever).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_RETRYABLE = "failed_retryable"
    TERMINAL_FAILED = "terminal_failed"


class JobQueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> JobStatus: ...

    async def get_result(self, job_id: str) -> dict[str, object] | None: ...
