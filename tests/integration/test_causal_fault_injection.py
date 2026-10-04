"""Fault injection at real boundaries (process, queue row, journal file, bytes).

Every failure below is produced the way it happens in production, not by a
boolean branch inside the code under test:

* **crash after execution, before the terminal commit** — a real child process
  runs the real worker + the real creative verifier and dies (``os._exit``)
  inside the verifier, after FFmpeg produced the artifact and after the
  execution fact was recorded, leaving the row in ``verifying``;
* **lost enqueue acknowledgement** — a real child process makes the enqueue
  durable and dies before the caller could learn the job id; the parent
  retries under the same key and must observe *one* execution;
* **stale execution (fencing)** — a second queue over the same sidecar takes
  the row over; the first execution's completion is refused by the CAS and
  must leave no artifact, no receipt and no success;
* **corrupted input** — a real corrupt media file through the real FFmpeg
  lane must fail closed: a failure receipt, and no artifact node at all;
* **tampered history** — a journal row is edited on disk; the chain
  verification and the passport must both refuse.

The invariant checked after every recovery: a new physical attempt is lawful,
a broken logical history is not — one logical artifact, no unexplained
duplicate, no receipt without evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.causal import (
    CausalJournal,
    CausalObserver,
    PassportBuilder,
    Stage,
    job_facts_from_chain_row,
    verify_passport,
)
from nexus_ai_agent.causal.models import PassportRefused, verification_node_id
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.jobs.verification import ArtifactClaim, verify_artifact
from nexus_ai_agent.worker import default_job_handlers

JOB_TYPE = "creative_render"
PROBE_JOB_TYPE = "fault_probe"
CRASH_EXIT_CODE = 17

#: Child process: run one real creative_render job and die inside the real
#: verifier (the artifact exists on disk by then; the row is left verifying).
_CRASH_CHILD = """
import os
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    default_artifact_verifiers,
)
from nexus_ai_agent.causal import CausalJournal, CausalObserver
from nexus_ai_agent.worker import default_job_handlers
import asyncio

workspace = Path(os.environ["FAULT_WORKSPACE"])
key = os.environ["FAULT_KEY"]
jobs_db = os.environ["FAULT_JOBS_DB"]
journal_db = os.environ["FAULT_JOURNAL_DB"]

real_verifier = default_artifact_verifiers()["creative_render"]


def crashing_verifier(payload, result):
    measured = real_verifier(payload, result)
    # The verification has just measured the artifact bytes; crash before the
    # queue may commit anything about it.
    os._exit(int(os.environ.get("FAULT_EXIT_CODE", "17")))


async def main():
    queue = InProcessJobQueue(jobs_db, causal_observer=CausalObserver(CausalJournal(journal_db)))
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    queue.register_artifact_verifier("creative_render", crashing_verifier)
    payload = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2000000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "en",
        "idempotency_key": key,
    }
    job_id = await queue.enqueue(
        job_type="creative_render", idempotency_key=key, payload=payload
    )
    print(job_id, flush=True)
    while True:
        await asyncio.sleep(0.05)


asyncio.run(main())
"""

#: Child process: make the enqueue durable, then die before acknowledging it.
_LOST_ACK_CHILD = """
import os
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.causal import CausalJournal, CausalObserver
from nexus_ai_agent.worker import default_job_handlers
import asyncio

workspace = Path(os.environ["FAULT_WORKSPACE"])
key = os.environ["FAULT_KEY"]
jobs_db = os.environ["FAULT_JOBS_DB"]
journal_db = os.environ["FAULT_JOURNAL_DB"]


async def main():
    queue = InProcessJobQueue(jobs_db, causal_observer=CausalObserver(CausalJournal(journal_db)))
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    payload = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2000000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "en",
        "idempotency_key": key,
    }
    await queue.enqueue(job_type="creative_render", idempotency_key=key, payload=payload)
    os._exit(0)  # the caller never receives the acknowledgement


asyncio.run(main())
"""


def _clip(path: Path, seconds: int = 2) -> None:
    subprocess.run(
        [
            resolve_ffmpeg_bin(),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _payload(workspace: Path, key: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "en",
        "idempotency_key": key,
    }
    base.update(over)
    return base


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        status = await queue.get_status(job_id)
        if status in {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not settle")


async def _passport_builder(
    queue: InProcessJobQueue, journal: CausalJournal, root: Path, job_ids: list[str]
) -> PassportBuilder:
    facts = {
        job_id: job_facts_from_chain_row(await queue.get_result_chain(job_id)) for job_id in job_ids
    }
    return PassportBuilder(journal, lambda job_id: facts.get(job_id), allowed_roots=[root])


def _child_env(tmp_path: Path, workspace: Path, key: str, exit_code: int = CRASH_EXIT_CODE) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "FAULT_WORKSPACE": str(workspace),
            "FAULT_KEY": key,
            "FAULT_JOBS_DB": str(tmp_path / "jobs.sqlite3"),
            "FAULT_JOURNAL_DB": str(tmp_path / "causal.sqlite"),
            "FAULT_EXIT_CODE": str(exit_code),
            "CREATIVE_TEMP_DIR": str(tmp_path / "creative_tmp"),
        }
    )
    return env


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temp = tmp_path / "creative_tmp"
    temp.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(temp))
    settings_module.get_settings.cache_clear()
    yield temp
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_crash_after_execution_before_commit_recovers_without_second_artifact(
    tmp_path: Path, workspace: Path
) -> None:
    key = "creative:42:4242:crash-1"
    job_workspace = workspace / "creative_crash1"
    job_workspace.mkdir()
    _clip(job_workspace / "input.mp4")

    child = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD],
        env=_child_env(tmp_path, job_workspace, key),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert child.returncode == CRASH_EXIT_CODE, child.stderr
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        row = connection.execute(
            "SELECT id FROM nexus_job_queue WHERE idempotency_key = ?", (key,)
        ).fetchone()
    assert row is not None, "the crashed child must have left a durable row"
    job_id = str(row[0])

    # ── pre-recovery state: execution happened, but nothing was certified ──
    journal = CausalJournal(tmp_path / "causal.sqlite")
    assert journal.verify_chain().ok
    stages = [record.stage for record in journal.records()]
    assert stages == [Stage.REQUEST, Stage.JOB, Stage.ATTEMPT, Stage.EXECUTION]
    artifact_candidates = list(job_workspace.glob("output.mp4"))
    assert artifact_candidates, "the crashed attempt really did produce bytes"

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    assert await queue.get_status(job_id) is JobStatus.VERIFYING
    builder = await _passport_builder(queue, journal, workspace, [job_id])
    with pytest.raises(PassportRefused, match="evidence_missing"):
        # The bytes exist, but no verification ever certified them: refusing
        # is the only honest answer.
        builder.for_artifact_path(artifact_candidates[0])

    # ── recovery: takeover re-runs the attempt under a higher fence ──
    recovered = InProcessJobQueue(
        tmp_path / "jobs.sqlite3",
        causal_observer=CausalObserver(journal),
    )
    for job_type, handler in default_job_handlers().items():
        recovered.register_handler(job_type, handler)
    assert job_id in await recovered.resume_pending()
    assert await _drain(recovered, job_id) is JobStatus.COMPLETED

    result = await recovered.get_result(job_id)
    assert result is not None and result["sha256"].startswith("sha256:")
    records = journal.records()
    attempts = [record for record in records if record.stage is Stage.ATTEMPT]
    assert [record.facts.get("attempt") for record in attempts] == [1, 2]
    artifacts = [record for record in records if record.stage is Stage.ARTIFACT]
    assert len(artifacts) == 1, "a second physical attempt is lawful; a second artifact is not"
    receipts = [record for record in records if record.stage is Stage.RECEIPT]
    assert [record.facts.get("status") for record in receipts] == ["completed"]

    # The one lawful artifact belongs to the attempt that actually settled.
    passport = (await _passport_builder(recovered, journal, workspace, [job_id])).for_artifact(
        str(result["sha256"])
    )
    assert passport.completeness == "COMPLETE", passport.divergences
    assert passport.attempt == 2
    assert verify_passport(passport).ok


@pytest.mark.asyncio
async def test_lost_enqueue_acknowledgement_produces_exactly_one_execution(
    tmp_path: Path, workspace: Path
) -> None:
    key = "creative:42:4242:ack-loss"
    job_workspace = workspace / "creative_ack"
    job_workspace.mkdir()
    _clip(job_workspace / "input.mp4")
    counter = tmp_path / "executions.txt"

    child = subprocess.run(
        [sys.executable, "-c", _LOST_ACK_CHILD],
        env=_child_env(tmp_path, job_workspace, key),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert child.returncode == 0, child.stderr

    # The enqueue is durable even though no caller ever saw the job id.
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        row = connection.execute(
            "SELECT id, status FROM nexus_job_queue WHERE idempotency_key = ?", (key,)
        ).fetchone()
    assert row is not None
    durable_job_id, status = str(row[0]), str(row[1])
    assert status == JobStatus.PENDING.value

    # The caller retries; the durable key collapses the retry.  The retrying
    # process carries its own observer, so the execution it runs is recorded.
    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3",
        causal_observer=CausalObserver(CausalJournal(tmp_path / "causal.sqlite")),
    )

    async def counting_handler(payload: dict[str, object]) -> dict[str, object]:
        # ``.bin`` keeps the verifier's binary dialect (sha256 + size), which
        # is what this test is about; real media bytes are exercised by the
        # end-to-end FFmpeg test.
        out = Path(str(payload["workspace_dir"])) / "output.bin"
        data = b"fault-injection-artifact-bytes"
        out.write_bytes(data)
        with counter.open("a", encoding="utf-8") as handle:
            handle.write("run\n")
        return {
            "success": True,
            "artifact_path": str(out),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "operation": "timeline.trim",
        }

    queue.register_handler(JOB_TYPE, counting_handler)
    queue.register_artifact_verifier(JOB_TYPE, _minimal_verifier)
    retried = await queue.enqueue(
        job_type=JOB_TYPE,
        idempotency_key=key,
        payload=_payload(job_workspace, key),
    )
    assert retried == durable_job_id, "the retry must observe the already-durable job"
    status = await _drain(queue, retried)
    assert status is JobStatus.COMPLETED, await queue.get_result(retried)

    async with asyncio.timeout(10):
        while not counter.exists():
            await asyncio.sleep(0.05)
    assert counter.read_text(encoding="utf-8").count("run") == 1, "one logical execution"

    journal = CausalJournal(tmp_path / "causal.sqlite")
    assert journal.verify_chain().ok
    assert len([r for r in journal.records() if r.stage is Stage.EXECUTION]) == 1
    assert len([r for r in journal.records() if r.stage is Stage.ARTIFACT]) == 1


def _minimal_verifier(payload: dict[str, object], result: dict[str, object]):
    """The repository's own verifier, pointed at this test's artifact name."""
    claim = ArtifactClaim.from_handler_result(dict(result), default_kind="binary")
    root = Path(str(payload["workspace_dir"]))
    return verify_artifact(claim, expected_root=root)


@pytest.mark.asyncio
async def test_stale_execution_cannot_settle_the_row_or_record_an_artifact(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two owners over one sidecar: the stale one is fenced out (real CAS)."""
    key = "creative:42:4242:fence"
    job_workspace = workspace / "creative_fence"
    job_workspace.mkdir()
    _clip(job_workspace / "input.mp4")
    journal = CausalJournal(tmp_path / "causal.sqlite")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        # A worker that never publishes: it stages its own file and returns.
        # (The real lane publishes by atomic rename under a fence; this stand-in
        # keeps the test focused on the fenced *claims*, not on filesystem
        # traffic.)
        out = Path(str(payload["workspace_dir"])) / "stale-attempt-1.bin"
        data = b"stale-attempt-output"
        out.write_bytes(data)
        return {
            "success": True,
            "artifact_path": str(out),
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "operation": "timeline.trim",
        }

    first = InProcessJobQueue(tmp_path / "jobs.sqlite3", causal_observer=CausalObserver(journal))
    first.register_handler(JOB_TYPE, slow_handler)
    first.register_artifact_verifier(JOB_TYPE, _minimal_verifier)
    job_id = await first.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(job_workspace, key)
    )
    async with asyncio.timeout(30):
        await started.wait()

    # A second owner appears (restart / another process over the same sidecar)
    # and takes the abandoned row over: the reservation mints attempt 2.
    second = InProcessJobQueue(tmp_path / "jobs.sqlite3", causal_observer=CausalObserver(journal))
    for job_type, handler in default_job_handlers().items():
        second.register_handler(job_type, handler)
    assert job_id in await second.resume_pending()
    assert await _drain(second, job_id) is JobStatus.COMPLETED
    result = await second.get_result(job_id)
    assert result is not None and result.get("success") is True

    # Now let the stale execution finish: every fenced CAS rejects it.
    release.set()
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        if first._tasks.get(job_id) is None and job_id not in first._tasks:  # noqa: SLF001
            break
        await asyncio.sleep(0.05)
    await first.shutdown()
    await second.shutdown()

    assert await first.get_status(job_id) is JobStatus.COMPLETED
    records = journal.records()
    executions = [record for record in records if record.stage is Stage.EXECUTION]
    # Both physical executions are in the history (the fenced one really ran),
    # but only the attempt that owned the row produced certified evidence.
    assert sorted(str(record.facts.get("attempt")) for record in executions) == ["1", "2"]
    artifacts = [record for record in records if record.stage is Stage.ARTIFACT]
    assert len(artifacts) == 1, "the fenced attempt certified nothing"
    verifications = [record for record in records if record.stage is Stage.VERIFICATION]
    receipts = [record for record in records if record.stage is Stage.RECEIPT]
    assert [record.facts.get("attempt") for record in verifications] == [2]
    assert [record.facts.get("attempt") for record in receipts] == [2]
    assert not journal.records_for(verification_node_id(job_id, 1))

    result_digest = str(result["sha256"])
    passport = (await _passport_builder(second, journal, workspace, [job_id])).for_artifact(
        result_digest
    )
    assert passport.completeness == "COMPLETE", passport.divergences
    assert passport.attempt == 2
    assert verify_passport(passport).ok


@pytest.mark.asyncio
async def test_corrupted_input_fails_closed_without_an_artifact_node(
    tmp_path: Path, workspace: Path
) -> None:
    """A real corrupt media file: failure receipt, and no artifact in history."""
    key = "creative:42:4242:corrupt"
    job_workspace = workspace / "creative_corrupt"
    job_workspace.mkdir()
    (job_workspace / "input.mp4").write_bytes(b"this is not a video at all")

    journal = CausalJournal(tmp_path / "causal.sqlite")
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", causal_observer=CausalObserver(journal))
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    job_id = await queue.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(job_workspace, key)
    )
    status = await _drain(queue, job_id)

    assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    records = journal.records()
    assert not [record for record in records if record.stage is Stage.ARTIFACT]
    receipts = [record for record in records if record.stage is Stage.RECEIPT]
    assert receipts and receipts[-1].facts.get("status") == status.value
    assert receipts[-1].facts.get("error_present") is True


@pytest.mark.asyncio
async def test_tampered_journal_refuses_and_leaves_the_artifact_intact(
    tmp_path: Path, workspace: Path
) -> None:
    key = "creative:42:4242:tamper"
    job_workspace = workspace / "creative_tamper"
    job_workspace.mkdir()
    _clip(job_workspace / "input.mp4")
    journal = CausalJournal(tmp_path / "causal.sqlite")
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", causal_observer=CausalObserver(journal))
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    job_id = await queue.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(job_workspace, key)
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None
    artifact = Path(str(result["artifact_path"]))
    before = artifact.read_bytes()

    journal_db = journal.path
    assert journal_db is not None
    with sqlite3.connect(journal_db) as connection:
        connection.execute(
            "UPDATE causal_journal SET record_json = replace(record_json, ?, ?) WHERE seq = 2",
            ('"created":true', '"created":false'),
        )

    tampered = CausalJournal(journal_db)
    assert tampered.verify_chain().ok is False
    builder = await _passport_builder(queue, tampered, workspace, [job_id])
    with pytest.raises(PassportRefused, match="journal_tampered"):
        builder.for_artifact(str(result["sha256"]))

    # Refusing to certify changed nothing about the artifact itself.
    assert artifact.read_bytes() == before
    assert await queue.get_status(job_id) is JobStatus.COMPLETED
