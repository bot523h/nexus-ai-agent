"""Real OS-process crash recovery for the NEXUS V1 execution core.

The in-process crash matrix (``test_execution_crash_matrix.py``) models a
restart as a *fresh queue over the same sidecar*.  This file goes one step
further: a **separate OS process** actually owns and runs a job, is
``SIGKILL``-ed mid-execution (no cleanup, no cancellation handler), and a
second process recovers the same durable row.  It proves the durability,
fencing and recovery claims hold across real process death, not merely across
constructor calls.

It is deterministic: the worker signals readiness through a marker file (never
a sleep), the crash is an exact ``SIGKILL`` at a controlled phase (the handler
has reserved the attempt and parked), and every post-condition is asserted.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL-based crash recovery"),
]

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}

# A worker process that enqueues one job, reserves it, parks inside the handler
# (the crash window), and writes the durable facts it sees to a marker file.
_WORKER = """
import asyncio, json, os, signal, sqlite3, sys
from pathlib import Path

sys.path.insert(0, {src!r})
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

db_path = Path(sys.argv[1])
marker = Path(sys.argv[2])


async def handler(payload):
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, started_at, attempt FROM nexus_job_queue "
            "WHERE status = 'processing' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    # Write to a temp file and atomically rename: the parent must never observe
    # (and try to parse) a partially written marker.
    payload = json.dumps({{
        "job_id": row["id"],
        "pid": os.getpid(),
        "started_at": row["started_at"],
        "attempt": row["attempt"],
    }})
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(payload)
    os.replace(tmp, marker)
    signal.pause()  # park forever — the parent will SIGKILL us here
    return {{"ok": True}}


async def main():
    queue = InProcessJobQueue(db_path, artifact_verifiers={{}})
    queue.register_handler("probing", handler)
    await queue.enqueue(job_type="probing", idempotency_key="crash-key", payload={{}})
    await asyncio.Event().wait()


asyncio.run(main())
"""


def _row(db: Path, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT status, attempt, started_at, result_json FROM nexus_job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return row


def _wait_for_marker(marker: Path, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return json.loads(marker.read_text())
        if proc.poll() is not None:
            raise AssertionError(f"worker exited early: rc={proc.returncode}")
        time.sleep(0.02)
    raise AssertionError("worker never reached the crash window (no marker)")


def _spawn_worker(tmp_path: Path, db: Path):
    script = tmp_path / "crash_worker.py"
    marker = tmp_path / "marker.json"
    src = str(Path(__file__).resolve().parents[2] / "src")
    script.write_text(_WORKER.format(src=src))
    proc = subprocess.Popen(
        [sys.executable, str(script), str(db), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc, marker


def test_real_process_kill_leaves_a_recoverable_durable_job(tmp_path: Path) -> None:
    """SIGKILL mid-execution: the row is durable and recoverable, and only the
    surviving process may complete it (no fabricated success from a dead owner).
    """
    db = tmp_path / "jobs.sqlite3"
    proc, marker = _spawn_worker(tmp_path, db)
    try:
        ready = _wait_for_marker(marker, proc)
        job_id = ready["job_id"]

        # The owner reserved attempt 1 and stamped started_at before parking.
        before = _row(db, job_id)
        assert before["status"] == JobStatus.PROCESSING.value
        assert before["attempt"] == 1
        assert before["started_at"] is not None
        assert before["result_json"] in (None, "", "null")

        # Crash the owning process hard — no unwinding, no cancellation path.
        os.kill(ready["pid"], 9)
        proc.wait(timeout=15)
        assert proc.returncode != 0

        # The dead worker produced no authoritative completion.
        after_kill = _row(db, job_id)
        assert after_kill["status"] == JobStatus.PROCESSING.value
        assert after_kill["attempt"] == 1
        assert after_kill["result_json"] in (None, "", "null")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    # Backdate the orphan so its staleness is provable regardless of timing
    # (the owning process is dead; a one-hour window then recovers it).
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE nexus_job_queue SET started_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )

    # A second process recovers the orphaned row over the same sidecar.
    async def recover_and_run() -> None:
        queue = InProcessJobQueue(db, artifact_verifiers={})

        async def recovered_handler(payload: dict[str, object]) -> dict[str, object]:
            return {"who": "recovered-process"}

        queue.register_handler("probing", recovered_handler)
        recovered = await queue.recover_job(job_id, stale_after=timedelta(hours=1))
        assert recovered == [job_id], "the genuinely stale row must be recoverable"
        for _ in range(500):
            if await queue.get_status(job_id) in TERMINAL:
                break
            await asyncio.sleep(0.01)
        assert await queue.get_status(job_id) is JobStatus.COMPLETED
        await queue.shutdown()

    asyncio.run(recover_and_run())

    # Recovery advanced the fencing token; the completion belongs to attempt 2.
    final = _row(db, job_id)
    assert final["status"] == JobStatus.COMPLETED.value
    assert final["attempt"] == 2
    assert json.loads(str(final["result_json"]))["who"] == "recovered-process"
