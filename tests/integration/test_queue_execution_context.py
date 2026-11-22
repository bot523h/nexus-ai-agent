"""Queue-minted execution claims stay scoped to each real queue handler."""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    current_execution_claim,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus


@pytest.mark.asyncio
async def test_concurrent_handlers_receive_distinct_claims_in_threads(tmp_path) -> None:
    seen = {}
    both_started = asyncio.Event()

    async def handler(payload):
        claim = current_execution_claim()
        assert claim is not None
        seen[payload["label"]] = claim
        if len(seen) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        assert current_execution_claim() == claim
        assert await asyncio.to_thread(current_execution_claim) == claim
        return {"ok": True}

    queue = InProcessJobQueue(tmp_path / "queue.sqlite3", artifact_verifiers={})
    queue.register_handler("context_test", handler)
    await queue.acquire_ownership()
    try:
        assert current_execution_claim() is None
        ids = [
            await queue.enqueue(
                job_type="context_test",
                idempotency_key=label,
                payload={"label": label, "_queue_job_id": "untrusted", "_queue_attempt": 999},
            )
            for label in ("first", "second")
        ]
        for _ in range(200):
            statuses = [await queue.get_status(job_id) for job_id in ids]
            if all(status == JobStatus.COMPLETED for status in statuses):
                break
            await asyncio.sleep(0.02)
        assert statuses == [JobStatus.COMPLETED, JobStatus.COMPLETED]
        assert [seen[label].job_id for label in ("first", "second")] == ids
        assert all(seen[label].attempt == 1 for label in ("first", "second"))
        assert current_execution_claim() is None
    finally:
        await queue.shutdown()
