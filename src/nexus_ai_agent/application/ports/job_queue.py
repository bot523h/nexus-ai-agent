"""Minimal job contract; the first implementation is intentionally in-process."""

from __future__ import annotations

from typing import Protocol


class JobQueuePort(Protocol):
    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> str: ...
