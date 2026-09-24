"""Integration: the scale-to-zero tier for the job queue (PostgreSQL).

Proves the concurrency contract on a real database: idempotency keys,
concurrent claims (two ``_claim_next`` workers racing — ``SKIP LOCKED``
must give each job to exactly one winner), and the 2 s stale-steal window
(a ``processing`` row dead past the timeout returns to ``pending``).

Idempotency keys carry an integration namespace + millisecond stamp
(``integ-999-<name>-<ms>``) so parallel CI runs never collide.

Skipped unless ``NEXUS_DATABASE_URL`` is set (CI: the ``migrate-postgres``
job provides a local pgvector/pg16 service container).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.stateful import PgJobQueue
from nexus_ai_agent.stateful.job_queue_pg import JOB_QUEUE_PG_TABLE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("NEXUS_DATABASE_URL"), reason="NEXUS_DATABASE_URL not set"
    ),
]

_URL = os.environ.get("NEXUS_DATABASE_URL", "")


def _key(name: str) -> str:
    return f"integ-999-{name}-{int(time.time() * 1000)}"


def _conn():
    import psycopg

    return psycopg.connect(_URL, autocommit=True)


def test_idempotency_key_prevents_a_second_effect() -> None:
    key = _key("idempotency")
    queue = PgJobQueue(_URL)
    try:
        first = asyncio.run(queue.enqueue(job_type="t", idempotency_key=key, payload={"a": 1}))
        second = asyncio.run(queue.enqueue(job_type="t", idempotency_key=key, payload={"a": 2}))
        assert first == second

        conn = _conn()
        count = conn.execute(
            f"SELECT count(*) FROM {JOB_QUEUE_PG_TABLE} WHERE idempotency_key = %s", (key,)
        ).fetchone()[0]
        conn.close()
        assert count == 1
    finally:
        queue.close()


def test_concurrent_claims_never_double_execute() -> None:
    key_a, key_b = _key("race-a"), _key("race-b")
    executions: list[str] = []

    queue = PgJobQueue(
        _URL,
        handlers={
            "race": lambda payload: (executions.append(str(payload["tag"])), {"ok": True})[1]
        },
    )
    try:
        asyncio.run(queue.enqueue(job_type="race", idempotency_key=key_a, payload={"tag": "A"}))
        asyncio.run(queue.enqueue(job_type="race", idempotency_key=key_b, payload={"tag": "B"}))

        async def _race() -> list:
            return await asyncio.gather(
                asyncio.to_thread(queue._claim_next),
                asyncio.to_thread(queue._claim_next),
            )

        completions = [c for c in asyncio.run(_race()) if c is not None]
        assert len(completions) == 2  # each job to exactly one winner
        assert sorted(executions) == ["A", "B"]  # no job ran twice, none lost

        for job_id in (c.job_id for c in completions):
            status = asyncio.run(queue.get_status(job_id))
            assert status is not None
    finally:
        queue.close()


def test_stale_row_dead_past_the_timeout_is_stolen() -> None:
    key = _key("steal")
    queue = PgJobQueue(_URL, processing_timeout_seconds=2.0)
    try:
        job_id = asyncio.run(queue.enqueue(job_type="t", idempotency_key=key, payload={}))

        # Simulate a container that claimed this job and was then killed:
        # status=processing, locked 5s ago, owner long dead.
        conn = _conn()
        five_seconds_ago = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        conn.execute(
            f"UPDATE {JOB_QUEUE_PG_TABLE} "
            "SET status = 'processing', owner_id = 'pgq-killed0000', "
            "locked_at = %s, started_at = %s WHERE id = %s",
            (five_seconds_ago, five_seconds_ago, job_id),
        )
        conn.close()

        completion = queue._claim_next()  # steals the stale row, then claims it
        assert completion is not None
        assert completion.job_id == job_id

        conn = _conn()
        row = conn.execute(
            f"SELECT status, owner_id FROM {JOB_QUEUE_PG_TABLE} WHERE id = %s", (job_id,)
        ).fetchone()
        conn.close()
        assert row[0] in {"completed", "failed"}
        assert row[1] != "pgq-killed0000"  # re-owned by the live container
    finally:
        queue.close()


def test_live_processing_row_is_not_stolen() -> None:
    key = _key("live")
    queue = PgJobQueue(_URL, processing_timeout_seconds=2.0)
    try:
        job_id = asyncio.run(queue.enqueue(job_type="t", idempotency_key=key, payload={}))
        conn = _conn()
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"UPDATE {JOB_QUEUE_PG_TABLE} "
            "SET status = 'processing', owner_id = 'pgq-liveowner0', locked_at = %s WHERE id = %s",
            (now_iso, job_id),
        )
        conn.close()

        # No other pending row: the live row must NOT be taken.
        assert queue._claim_next() is None

        conn = _conn()
        row = conn.execute(
            f"SELECT status, owner_id FROM {JOB_QUEUE_PG_TABLE} WHERE id = %s", (job_id,)
        ).fetchone()
        conn.close()
        assert row == ("processing", "pgq-liveowner0")
    finally:
        queue.close()
