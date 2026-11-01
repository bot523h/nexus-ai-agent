"""task-283 — crash *after* publication, *before* the durable completion.

Review finding 4 (Medium): the crash matrix stopped at backup → publish.  The
window that actually matters for data integrity is wider: the artifact has been
renamed into its final namespace, the process dies, and the durable row is still
``verifying`` (or ``processing``).  Two things must then hold:

* **"artifact exists" is never read as "job succeeded"** — the row stays
  non-terminal until a *new owner* re-executes and commits success;
* recovery under a new owner must reconcile artifact and row without touching
  any unrelated output.

The crash is real, not simulated with a mock: a child process publishes and
then ``os._exit``s inside its post-publication re-probe, so the flock is
dropped by the kernel exactly as in a SIGKILL.  The parent process then takes
over the same sidecar — which is only possible because the dead owner's lock is
gone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
from nexus_ai_agent.jobs.verification import VerificationOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}
JOB_TYPE = "crash_publication"

_CHILD = textwrap.dedent(
    """
    import asyncio, os, sys
    from pathlib import Path
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.creative.render_jobs import build_creative_render_publication
    from nexus_ai_agent.jobs.verification import VerificationOutcome

    db, workspace, artifact_root, job_type = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    db, workspace, artifact_root = Path(db), Path(workspace), Path(artifact_root)
    workspace.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact = workspace / "render.mp4"

    probs = {"n": 0}

    async def handler(payload):
        artifact.write_bytes(b"published by the dying owner")
        return {"artifact_path": str(artifact), "workspace_dir": str(workspace)}

    def verifier(payload, result):
        probs["n"] += 1
        if probs["n"] >= 2:
            # The artifact is already renamed into place (publish returned);
            # die before the row may be committed COMPLETED.
            sys.stdout.write("CRASHING_AFTER_PUBLISH\\n")
            sys.stdout.flush()
            os._exit(9)
        return VerificationOutcome(ok=True, reason_code=None, summary={})

    async def main():
        q = InProcessJobQueue(str(db))
        q.register_handler(job_type, handler)
        q.register_artifact_verifier(job_type, verifier)
        q.register_artifact_publication(
            job_type, build_creative_render_publication(artifact_root)
        )
        await q.acquire_ownership()
        await q.enqueue(job_type=job_type, idempotency_key="crash-1", payload={"n": 1})
        await asyncio.sleep(30)

    asyncio.run(main())
    """
)


def _rows(db: Path) -> dict[str, dict[str, object]]:
    import sqlite3

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        return {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM nexus_job_queue").fetchall()
        }
    finally:
        connection.close()


async def _recover_and_complete(
    db: Path, workspace: Path, artifact_root: Path, unrelated: Path
) -> InProcessJobQueue:
    artifact = workspace / "render.mp4"
    queue = InProcessJobQueue(str(db))

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        artifact.write_bytes(b"published by the new owner")
        return {"artifact_path": str(artifact), "workspace_dir": str(workspace)}

    def verifier(payload: dict[str, object], result: dict[str, object]) -> VerificationOutcome:
        return VerificationOutcome(ok=True, reason_code=None, summary={})

    queue.register_handler(JOB_TYPE, handler)
    queue.register_artifact_verifier(JOB_TYPE, verifier)
    queue.register_artifact_publication(JOB_TYPE, build_creative_render_publication(artifact_root))
    # Only a dead owner's flock is free, so this also proves the crash released it.
    await queue.acquire_ownership()
    await queue.resume_pending()
    return queue


async def test_crash_after_publish_before_completion_reconciles_under_a_new_owner(
    tmp_path: Path,
) -> None:
    db = tmp_path / "jobs.sqlite3"
    workspace = tmp_path / "ws"
    artifact_root = tmp_path / "artifacts"
    unrelated = artifact_root / "published" / "other-job" / "keep.mp4"

    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(db), str(workspace), str(artifact_root), JOB_TYPE],
        cwd=str(REPO_ROOT),
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 9, f"child did not crash as intended: {child.stderr}"
    assert "CRASHING_AFTER_PUBLISH" in child.stdout

    # ── the state the crash left behind ─────────────────────────────────────
    rows = _rows(db)
    assert len(rows) == 1
    ((job_id, row),) = rows.items()
    assert row["status"] != JobStatus.COMPLETED.value, (
        "an existing artifact must never be read as a completed job"
    )
    assert row["status"] == JobStatus.VERIFYING.value

    published_dir = artifact_root / "published"
    job_dirs = [p for p in published_dir.iterdir() if p.is_dir()]
    assert len(job_dirs) == 1, "the dying owner published exactly one job namespace"
    published = job_dirs[0] / "render.mp4"
    assert published.exists(), "the artifact really was published before the crash"
    assert published.read_bytes() == b"published by the dying owner"

    # Unrelated output that shares the publication root must survive recovery.
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_bytes(b"unrelated bytes")

    # ── a new owner recovers and re-executes ────────────────────────────────
    queue = await _recover_and_complete(db, workspace, artifact_root, unrelated)
    deadline = 10.0
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        if await queue.get_status(job_id) is JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.02)
    await queue.shutdown()

    rows_after = _rows(db)
    assert rows_after[job_id]["status"] == JobStatus.COMPLETED.value
    assert int(rows_after[job_id]["attempt"]) == 2, (
        "recovery must mint a strictly higher fence, not reuse the dead attempt"
    )
    assert published.read_bytes() == b"published by the new owner"
    assert unrelated.read_bytes() == b"unrelated bytes", "unrelated output was touched"
    # The previous bytes are kept recoverable, never silently destroyed.
    backup = published.parent / ".render.mp4.prev"
    assert backup.exists() and backup.read_bytes() == b"published by the dying owner"
    assert json.loads((published.parent / ".render.mp4.owner").read_text())["attempt"] == "2"


async def test_recovery_does_not_mark_success_without_a_rerun(tmp_path: Path) -> None:
    """The row is never completed by recovery itself — only by an execution."""
    db = tmp_path / "jobs.sqlite3"
    workspace = tmp_path / "ws"
    artifact_root = tmp_path / "artifacts"

    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(db), str(workspace), str(artifact_root), JOB_TYPE],
        cwd=str(REPO_ROOT),
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 9

    queue = InProcessJobQueue(str(db))
    # No handler / verifier registered: recovery requeues, but nothing can
    # execute, so the row must stay non-terminal rather than be optimistically
    # settled because an artifact happens to exist.
    await queue.acquire_ownership()
    reset = await queue.resume_pending()
    assert len(reset) == 1
    _job_id, status = list(_rows(db).items())[0]
    assert status["status"] == JobStatus.PENDING.value
    await asyncio.sleep(0.2)
    # With no handler the requeue fails closed; the point is that *nothing*
    # promotes the row to success just because the artifact is on disk.
    settled = _rows(db)[_job_id]["status"]
    assert settled == JobStatus.FAILED_TERMINAL.value
    assert settled != JobStatus.COMPLETED.value
    await queue.shutdown()
