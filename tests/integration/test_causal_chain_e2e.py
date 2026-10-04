"""The causal ledger on the real creative chain (no mock at the seam).

Chain under test, with every participant real:

    enqueue(creative_render)
      → InProcessJobQueue persists the row (idempotency key, attempt fencing)
      → CreativeObserver records the committed facts (append-only journal)
      → worker handler → packs registry → CommandBus → render lane → FFmpeg
      → queue-owned independent verification re-measures the artifact
      → terminal row + terminal receipt
      → PassportBuilder reconstructs the history from journal + row + bytes
      → verify_passport re-measures the bytes (independent of the producer)

The assertions that matter are on *evidence*, not on flags: the passport's
artifact identity must equal a digest this test computes from the file, the
journal must hold exactly one artifact record, and a duplicate request must
add no second logical work.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
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
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.worker import default_job_handlers

JOB_TYPE = "creative_render"


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
        await asyncio.sleep(0.1)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


class _Harness:
    def __init__(self, queue: InProcessJobQueue, journal: CausalJournal, root: Path) -> None:
        self.queue = queue
        self.journal = journal
        self.root = root

    async def passport_builder(self, job_ids: list[str]) -> PassportBuilder:
        """Compose the async durable reader with the sync passport builder.

        The row facts come from the queue's own ``get_result_chain`` (the
        durable projection, never handler values); they are snapshotted here
        because :class:`PassportBuilder` is deliberately synchronous (it only
        reads the journal, the given row facts and the bytes).
        """
        facts = {
            job_id: job_facts_from_chain_row(await self.queue.get_result_chain(job_id))
            for job_id in job_ids
        }

        def reader(job_id: str):  # noqa: ANN202 - test-local adapter
            return facts.get(job_id)

        return PassportBuilder(self.journal, reader, allowed_roots=[self.root])


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    temp = tmp_path / "creative_tmp"
    temp.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(temp))
    settings_module.get_settings.cache_clear()
    journal = CausalJournal(tmp_path / "causal.sqlite")
    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3",
        causal_observer=CausalObserver(journal),
        on_job_finished=None,  # keep the artifact for measurement
    )
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield _Harness(queue, journal, temp)
    settings_module.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_trim_produces_a_complete_evidence_backed_passport(harness: _Harness) -> None:
    key = "creative:42:4242:e2e-1"
    workspace = harness.root / "creative_e2e1"
    workspace.mkdir()
    _clip(workspace / "input.mp4")

    job_id = await harness.queue.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(workspace, key)
    )
    assert await _drain(harness.queue, job_id) is JobStatus.COMPLETED

    result = await harness.queue.get_result(job_id)
    assert result is not None and result["success"] is True
    artifact = Path(str(result["artifact_path"]))
    measured = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert measured == result["sha256"], "the worker's claim must match the bytes"
    assert artifact.stat().st_size > 0

    passport = (await harness.passport_builder([job_id])).for_artifact(measured)

    assert passport.completeness == "COMPLETE", passport.divergences
    assert passport.artifact_id == measured
    assert passport.job_id == job_id
    assert passport.attempt == 1
    assert passport.operation == "timeline.trim"
    assert passport.project_id == f"shot-{key}"
    assert passport.measured_artifact is not None
    assert passport.measured_artifact.sha256 == measured
    assert passport.measured_artifact.size_bytes == artifact.stat().st_size
    recorded = {item.stage for item in passport.stages if item.record_seqs}
    assert {Stage.REQUEST, Stage.JOB, Stage.ATTEMPT, Stage.EXECUTION, Stage.ARTIFACT} <= recorded
    assert {Stage.VERIFICATION, Stage.RECEIPT} <= recorded
    # The unproduced spine stages are reported honestly, not filled in.
    for stage in (Stage.INTENT, Stage.STRATEGY, Stage.WORK, Stage.PLAN, Stage.TRANSACTION):
        assert all(item.stage is not stage for item in passport.stages if item.record_seqs)

    verification = verify_passport(passport)
    assert verification.ok, verification
    assert verification.measured_sha256 == measured

    # Durable: a fresh reader over the same file sees the same history.
    reopened = CausalJournal(harness.journal.path)
    assert reopened.verify_chain().ok
    assert reopened.head_hash() == passport.journal_head_hash


@pytest.mark.asyncio
async def test_duplicate_request_adds_no_second_logical_work(harness: _Harness) -> None:
    key = "creative:42:4242:e2e-2"
    workspace = harness.root / "creative_e2e2"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    payload = _payload(workspace, key)

    first = await harness.queue.enqueue(job_type=JOB_TYPE, idempotency_key=key, payload=payload)
    second = await harness.queue.enqueue(job_type=JOB_TYPE, idempotency_key=key, payload=payload)
    assert second == first, "the durable idempotency key must collapse the replay"
    assert await _drain(harness.queue, first) is JobStatus.COMPLETED
    async with asyncio.timeout(10):
        while True:
            artifacts = [
                record for record in harness.journal.records() if record.stage is Stage.ARTIFACT
            ]
            if artifacts:
                break
            await asyncio.sleep(0.05)

    records = harness.journal.records()
    request_records = [record for record in records if record.stage is Stage.REQUEST]
    assert len(request_records) == 1, "a replayed request is one logical observation"
    assert len([record for record in records if record.stage is Stage.ARTIFACT]) == 1
    assert len([record for record in records if record.stage is Stage.EXECUTION]) == 1

    digest = str((await harness.queue.get_result(first))["sha256"])  # type: ignore[index]
    passport = (await harness.passport_builder([first])).for_artifact(digest)
    assert passport.completeness == "COMPLETE"
    assert passport.divergences == ()


@pytest.mark.asyncio
async def test_conflicting_replay_is_explained_not_hidden(harness: _Harness) -> None:
    """Two requests, one key, different payloads: first wins, ledger says why."""
    key = "creative:42:4242:e2e-3"
    workspace = harness.root / "creative_e2e3"
    workspace.mkdir()
    _clip(workspace / "input.mp4")

    first = await harness.queue.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(workspace, key)
    )
    conflicting = await harness.queue.enqueue(
        job_type=JOB_TYPE,
        idempotency_key=key,
        payload=_payload(workspace, key, args=["0", "2"], media_duration_us=2_000_000),
    )
    assert conflicting == first
    assert await _drain(harness.queue, first) is JobStatus.COMPLETED

    digest = str((await harness.queue.get_result(first))["sha256"])  # type: ignore[index]
    passport = (await harness.passport_builder([first])).for_artifact(digest)

    codes = [item.code for item in passport.divergences]
    assert "duplicate_request_with_different_payload" in codes
    assert passport.completeness == "COMPLETE", "an explained replay is not a divergence"
    assert passport.divergences[0].severity == "info"


@pytest.mark.asyncio
async def test_artifact_path_lookup_and_tamper_detection(harness: _Harness) -> None:
    key = "creative:42:4242:e2e-4"
    workspace = harness.root / "creative_e2e4"
    workspace.mkdir()
    _clip(workspace / "input.mp4")
    job_id = await harness.queue.enqueue(
        job_type=JOB_TYPE, idempotency_key=key, payload=_payload(workspace, key)
    )
    assert await _drain(harness.queue, job_id) is JobStatus.COMPLETED
    result = await harness.queue.get_result(job_id)
    assert result is not None
    artifact = Path(str(result["artifact_path"]))

    builder = await harness.passport_builder([job_id])
    passport = builder.for_artifact_path(artifact)
    assert passport.completeness == "COMPLETE"
    assert verify_passport(passport).ok

    # Corrupt the bytes after the fact: nothing stored changes, but the
    # passport and its independent verifier must both refuse to certify.
    artifact.write_bytes(b"not-the-video-anymore")

    rebuilt = builder.for_artifact_path(artifact)
    assert rebuilt.completeness == "DIVERGED"
    assert "artifact_bytes_diverged" in [item.code for item in rebuilt.divergences]
    verdict = verify_passport(rebuilt)
    assert verdict.ok is False

    # The historical (pre-corruption) passport is also caught, because the
    # verifier re-measures the bytes instead of trusting the record.
    stale = verify_passport(passport)
    assert stale.ok is False
    assert stale.reason_code == "artifact_digest_mismatch"
