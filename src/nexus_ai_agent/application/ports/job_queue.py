"""In-process background-job contract for the Modular Monolith."""

from __future__ import annotations

from enum import Enum
from typing import Protocol


class JobStatus(str, Enum):
    """Durable lifecycle states for an application-owned job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobQueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> JobStatus: ...

    async def get_result(self, job_id: str) -> dict[str, object] | None: ...
