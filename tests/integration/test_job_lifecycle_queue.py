"""Queue-level job-lifecycle enforcement (task-178) — M1..M10 + A/B + §17.

Every test here drives the REAL ``InProcessJobQueue`` (durable SQLite rows,
asyncio execution). Where media probing is exercised, the REAL allow-listed
FFmpeg binary is used — the same evidence path production uses; no test
fakes a probe result to make verification pass.

The mutation matrix:

===  ===================================================================
M1   execution failure → FAILED, no result payload, attempt recorded
M2   ffprobe failure (corrupt media at the expected path) → FAILED
M3   zero-byte output → FAILED (no exception path can bypass it)
M4   verification mismatch (sha/size lie) → FAILED
M5   existing destination survives a failed retry (never deleted first)
M6   idempotency: same key + different payload → first payload wins,
     one effect; same payload → same job id
M7   revision conflict: changed args under the same key cannot create a
     second effect; spec identity binds the recorded operation
M8   valid artifact + retry (re-enqueue of a completed job) → no re-run
M9   successful "encoding" of invalid artifact bytes → FAILED
M10  runtime/verifier fail-closed can never become Job success
A/B  legacy semantics (no verifier) completes a lying artifact claim;
     canonical queue fails it — the bug and its fix in one test
§17  SUCCEEDED ⇒ valid + stable + traceable artifact (real chain)
===  ===================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    VERIFICATION_RESULT_KEY,
    InProcessJobQueue,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.render_jobs import (
    CREATIVE_RENDER_JOB_TYPE,
    CreativeRenderError,
    _run_render_branch,
    creative_render_job,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    RenderError,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.jobs.lifecycle import is_failure
from nexus_ai_agent.jobs.verification import VerificationOutcome


def _clip(path: Path, seconds: float = 1.0) -> None:
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


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 60.0) -> JobStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await queue.get_status(job_id) in {
            JobStatus.COMPLETED,
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
        }:
            return await queue.get_status(job_id)
        await asyncio.sleep(0.02)
    raise TimeoutError(f"job {job_id} never reached a terminal state")


def _attempt(db_path: Path, job_id: str) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT attempt FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _row_error(db_path: Path, job_id: str) -> str | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT error FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
    return None if row is None else row[0]


def _creative_result(artifact: Path, *, duration_us: int = 500_000) -> dict[str, Any]:
    return {
        "success": True,
        "artifact_path": str(artifact),
        "artifact_kind": "video",
        "sha256": sha256_file(artifact),
        "size_bytes": artifact.stat().st_size,
        "duration_us": duration_us,
        "operation": "timeline.trim",
        "output_asset_id": "out",
    }


@pytest.fixture()
def creative_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir()
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    yield tmp_path
    settings_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# M1 — execution failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m1_execution_failure_is_failed_without_result(tmp_path: Path) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", artifact_verifiers={})

    async def boom(payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("runtime exploded")

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, boom)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="m1", payload={"x": 1}
    )
    # an unexpected handler exception is classified RETRYABLE (worker crash
    # class — conservative bounded retry when a scheduler exists; none today)
    assert await _drain(queue, job_id) is JobStatus.FAILED_RETRYABLE
    assert await queue.get_result(job_id) is None
    assert "runtime exploded" in (_row_error(tmp_path / "jobs.sqlite3", job_id) or "")
    assert _attempt(tmp_path / "jobs.sqlite3", job_id) == 1


# ---------------------------------------------------------------------------
# M2 — ffprobe failure on media at the expected path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m2_probe_failure_cannot_complete(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m2"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"\x00\x00\x00\x18ftypmp42corrupt-truncated")  # not probeable

    async def lying_handler(payload: dict[str, object]) -> dict[str, object]:
        return _creative_result(artifact)

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, lying_handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m2",
        payload={"workspace_dir": str(workspace)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert (_row_error(db, job_id) or "") == "verification_failed:probe_failed"
    assert await queue.get_result(job_id) is None


# ---------------------------------------------------------------------------
# M3 — zero-byte output
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m3_zero_byte_artifact_cannot_complete(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m3"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"")

    async def lying_handler(payload: dict[str, object]) -> dict[str, object]:
        claim = _creative_result(artifact)
        claim["size_bytes"] = 0
        claim["duration_us"] = None
        claim["sha256"] = "sha256:" + "0" * 64  # sha of empty
        return claim

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, lying_handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m3",
        payload={"workspace_dir": str(workspace)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert (_row_error(db, job_id) or "") == "verification_failed:empty_artifact"


# ---------------------------------------------------------------------------
# M4 — verification mismatch (handler computed sha, bytes differ)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m4_tampered_artifact_fails_sha_verification(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m4"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    _clip(artifact)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        result = _creative_result(artifact)
        data = artifact.read_bytes()
        # same length, different bytes: a pure content (sha) mismatch
        artifact.write_bytes(b"X" + data[1:])
        return result  # claim no longer matches disk

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m4",
        payload={"workspace_dir": str(workspace)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert (_row_error(db, job_id) or "") == "verification_failed:sha256_mismatch"


# ---------------------------------------------------------------------------
# M5 — existing destination survives a failed retry (render_jobs semantics)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m5_failed_retry_never_deletes_the_existing_destination(
    creative_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = creative_env / "creative_tmp" / "creative_m5"
    workspace.mkdir(parents=True)
    input_clip = workspace / "input.mp4"
    _clip(input_clip)
    destination = workspace / "output.mp4"
    _clip(destination, 0.4)
    previous_bytes = destination.read_bytes()

    import nexus_ai_agent.creative.rendering.executor as executor_module

    real_render_lane = executor_module.render_lane

    def _render_lane_always_fails(*args: Any, **kwargs: Any) -> Any:
        raise RenderError("ffmpeg exited 1: simulated encoder crash")

    monkeypatch.setattr(executor_module, "render_lane", _render_lane_always_fails)
    from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload

    payload = CreativeRenderPayload(
        command="edit",
        operation="trim",
        args=["0", "0.5"],
        workspace_dir=str(workspace),
        input_path=str(input_clip),
        user_id=1,
        chat_id=2,
        idempotency_key="m5",
    )
    with pytest.raises(CreativeRenderError):
        await _run_render_branch(payload, workspace, "timeline.trim")
    # the previous attempt's artifact is untouched — a failed re-render must
    # never delete-then-fail the destination
    assert destination.read_bytes() == previous_bytes
    monkeypatch.setattr(executor_module, "render_lane", real_render_lane)

    # and with the real lane, the retry succeeds through the same entrypoint
    result = await creative_render_job(
        {
            "command": "edit",
            "operation": "trim",
            "args": ["0", "0.5"],
            "workspace_dir": str(workspace),
            "input_path": str(input_clip),
            "media_duration_us": 1_000_000,
            "user_id": 1,
            "chat_id": 2,
            "idempotency_key": "m5",
        }
    )
    assert result["success"] is True
    assert result["artifact_path"] == str(destination)
    assert sha256_file(destination) == result["sha256"]


# ---------------------------------------------------------------------------
# M6 — idempotency: same key, different payload / same payload
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m6_idempotency_first_payload_wins_and_conflict_is_observed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3", artifact_verifiers={})
    calls = 0

    async def echo(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": payload["value"]}

    queue.register_handler("creative_like", echo)
    first = await queue.enqueue(
        job_type="creative_like", idempotency_key="m6", payload={"value": 1}
    )
    with caplog.at_level(logging.WARNING, logger="nexus_ai_agent.adapters.in_process_job_queue"):
        second = await queue.enqueue(
            job_type="creative_like", idempotency_key="m6", payload={"value": 2}
        )
    assert second == first  # one job, first payload wins
    await _drain(queue, first)
    assert calls == 1
    assert await queue.get_result(first) == {"value": 1}
    assert any("job_idempotency_payload_conflict" in r.message for r in caplog.records)

    # exact redelivery (equal payload bytes) is a pure dedupe: no warning
    caplog.clear()
    third = await queue.enqueue(
        job_type="creative_like", idempotency_key="m6", payload={"value": 1}
    )
    assert third == first
    assert not any("job_idempotency_payload_conflict" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# M7 — revision conflict: changed intent under the same key
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m7_revision_change_cannot_smuggle_a_second_effect(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m7"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    _clip(artifact)
    calls = 0

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _creative_result(artifact)

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    payload_v1 = {"workspace_dir": str(workspace), "operation": "trim", "args": ["0", "0.5"]}
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="m7", payload=payload_v1
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    chain = await queue.get_result_chain(job_id)
    assert chain["spec_identity"] == {"operation": "timeline.trim"}

    # a "revised" request under the same identity: deterministic refusal to
    # double-effect — same job, original intent, no second execution
    payload_v2 = {"workspace_dir": str(workspace), "operation": "trim", "args": ["0.1", "0.9"]}
    revised = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="m7", payload=payload_v2
    )
    assert revised == job_id
    await asyncio.sleep(0.1)
    assert calls == 1
    assert await queue.get_result_chain(job_id) == chain  # revision did not land


# ---------------------------------------------------------------------------
# M8 — valid artifact + retry: a completed job never re-executes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m8_completed_job_reenqueue_reuses_without_rerun(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m8"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    _clip(artifact)
    calls = 0

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _creative_result(artifact)

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    payload = {"workspace_dir": str(workspace)}
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="m8", payload=payload
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    first_bytes = artifact.read_bytes()

    again = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE, idempotency_key="m8", payload=payload
    )
    assert again == job_id
    await asyncio.sleep(0.2)
    assert calls == 1, "a completed job must never re-execute on redelivery"
    assert artifact.read_bytes() == first_bytes

    row = await queue.get_result(job_id)
    assert row is not None and row[VERIFICATION_RESULT_KEY]["status"] == "verified"


# ---------------------------------------------------------------------------
# M9 — successful encoding of invalid artifact bytes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m9_execution_success_with_invalid_media_is_failed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    workspace = tmp_path / "creative_m9"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    artifact.write_bytes(b"plain text masquerading as a video")  # "encode" wrote *something*

    async def proud_handler(payload: dict[str, object]) -> dict[str, object]:
        return _creative_result(artifact)

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, proud_handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m9",
        payload={"workspace_dir": str(workspace)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert (_row_error(db, job_id) or "").startswith("verification_failed:")


# ---------------------------------------------------------------------------
# M10 — runtime fail-closed and verifier crashes can never become success
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_m10_verifier_crash_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})

    def crashing_verifier(
        payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        raise RuntimeError("verifier exploded")

    queue.register_artifact_verifier(CREATIVE_RENDER_JOB_TYPE, crashing_verifier)
    workspace = tmp_path / "creative_m10"
    workspace.mkdir()
    artifact = workspace / "output.mp4"
    _clip(artifact)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return _creative_result(artifact)

    queue.register_handler(CREATIVE_RENDER_JOB_TYPE, handler)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m10",
        payload={"workspace_dir": str(workspace)},
    )
    assert is_failure(await _drain(queue, job_id))
    assert (_row_error(db, job_id) or "") == "verification_failed:verifier_crashed"


@pytest.mark.asyncio
async def test_m10b_runtime_fail_closed_after_publish_is_never_success(
    creative_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime that publishes an unverifiable file cannot produce success.

    The worker's own post-encode probe (runtime semantics) fails closed on
    the garbage bytes.  Observed truth (task-181 reconciliation — repository
    behavior wins over the earlier docstring claim of a typed dialect): the
    probe raises ``RenderError`` which escapes the handler into the queue's
    fail-closed conversion — the corrupt-output class, classified RETRYABLE
    (``FAILED_RETRYABLE``).  Never COMPLETED, no result payload, no artifact
    verification block, no claim anyone could mistake for one.
    """
    db = creative_env / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    from nexus_ai_agent.worker import default_job_handlers

    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    workspace = creative_env / "creative_tmp" / "creative_m10b"
    workspace.mkdir(parents=True)
    input_clip = workspace / "input.mp4"
    _clip(input_clip)

    def _publish_garbage(*args: Any, **kwargs: Any) -> Any:
        destination = Path(kwargs.get("output_path") or args[1])
        destination.write_bytes(b"half published, not probeable")
        from nexus_ai_agent.creative.rendering.executor import LaneArtifact

        return LaneArtifact(
            path=str(destination),
            sha256=sha256_file(destination),
            size_bytes=destination.stat().st_size,
            duration_us=0,
            width=None,
            height=None,
            has_audio=False,
            lane_ir_hash="sha256:" + "0" * 64,
            ops=(),
            binary="x",
            encoder={},
        )

    monkeypatch.setattr("nexus_ai_agent.creative.rendering.executor.render_lane", _publish_garbage)
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="m10b",
        payload={
            "command": "edit",
            "operation": "trim",
            "args": ["0", "0.5"],
            "workspace_dir": str(workspace),
            "input_path": str(input_clip),
            "media_duration_us": 1_000_000,
            "user_id": 1,
            "chat_id": 2,
            "idempotency_key": "m10b",
        },
    )
    status = await _drain(queue, job_id)
    # task-181 (GAP-A): corrupt output discovered at execution time is a
    # FAILURE status (RETRYABLE class) — never COMPLETED.
    assert status is JobStatus.FAILED_RETRYABLE, f"corrupt output must be RETRYABLE, got {status}"
    result = await queue.get_result(job_id)
    assert result is None
    assert VERIFICATION_RESULT_KEY not in (result or {})
    assert _row_error(creative_env / "jobs.sqlite3", job_id)
    chain = await queue.get_result_chain(job_id)
    assert chain["verification_status"] != "verified"
    assert chain["sha256"] is None
    assert chain["failure_reason"]


# ---------------------------------------------------------------------------
# A/B — the §17 question, demonstrated on both sides
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ab_zero_byte_claim_completes_without_verifier_fails_with_it(
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "ws_a"
    workspace_b = tmp_path / "ws_b"
    for ws in (workspace_a, workspace_b):
        ws.mkdir()
        (ws / "output.mp4").write_bytes(b"")
    calls = 0

    def make_handler() -> Any:
        async def handler(payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            artifact = Path(str(payload["workspace_dir"])) / "output.mp4"
            return {
                "success": True,
                "artifact_path": str(artifact),
                "artifact_kind": "video",
                "sha256": "sha256:" + "0" * 64,
                "size_bytes": 0,
                "duration_us": None,
                "operation": "timeline.trim",
                "output_asset_id": "out",
            }

        return handler

    # A — legacy semantics: verifier registry opted out; the lying claim
    # completes the job (this was the pre-task-178 hole).
    queue_a = InProcessJobQueue(tmp_path / "a.sqlite3", artifact_verifiers={})
    handler_a = make_handler()
    queue_a.register_handler(CREATIVE_RENDER_JOB_TYPE, handler_a)
    job_a = await queue_a.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="ab-a",
        payload={"workspace_dir": str(workspace_a)},
    )
    assert await _drain(queue_a, job_a) is JobStatus.COMPLETED, "legacy side must show the bug"

    # B — canonical queue: the same claim is independently re-measured and
    # the job fails closed.
    queue_b = InProcessJobQueue(tmp_path / "b.sqlite3")
    queue_b.register_handler(CREATIVE_RENDER_JOB_TYPE, make_handler())
    job_b = await queue_b.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key="ab-b",
        payload={"workspace_dir": str(workspace_b)},
    )
    assert is_failure(await _drain(queue_b, job_b)), "canonical side must fail the lie"
    assert (_row_error(tmp_path / "b.sqlite3", job_b) or "") == "verification_failed:empty_artifact"


# ---------------------------------------------------------------------------
# VERIFYING visibility, recovery, attempt accounting
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verifying_state_is_observable_and_cancellation_recovers(
    tmp_path: Path,
) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})
    release = asyncio.Event()

    def blocking_verifier(
        payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        # sync context (worker thread): park until the test observed the state
        import time

        while not release.is_set():
            time.sleep(0.01)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "verified"})

    queue.register_artifact_verifier("slow", blocking_verifier)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("slow", handler)
    job_id = await queue.enqueue(job_type="slow", idempotency_key="v1", payload={})
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        if await queue.get_status(job_id) is JobStatus.VERIFYING:
            break
        await asyncio.sleep(0.01)
    assert await queue.get_status(job_id) is JobStatus.VERIFYING

    release.set()
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result[VERIFICATION_RESULT_KEY]["status"] == "verified"


@pytest.mark.asyncio
async def test_shutdown_during_verification_recovers_on_resume(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})
    release = asyncio.Event()
    import threading

    def blocking_verifier(
        payload: dict[str, object], result: dict[str, object]
    ) -> VerificationOutcome:
        while not release.is_set():
            threading.Event().wait(0.01)
        return VerificationOutcome(ok=True, reason_code=None, summary={"status": "verified"})

    queue.register_artifact_verifier("rec", blocking_verifier)

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("rec", handler)
    job_id = await queue.enqueue(job_type="rec", idempotency_key="r1", payload={})
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        if await queue.get_status(job_id) is JobStatus.VERIFYING:
            break
        await asyncio.sleep(0.01)

    await queue.shutdown()  # cancels mid-verification → row recoverable
    with sqlite3.connect(db) as connection:
        status = connection.execute(
            "SELECT status FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert status == JobStatus.PENDING.value, "cancelled verification must stay recoverable"

    release.set()
    resumed = await queue.resume_pending()
    assert resumed == [job_id]
    assert await _drain(queue, job_id) is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_attempt_counts_executions_and_chain_reports_it(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    queue.register_handler("counted", handler)
    job_id = await queue.enqueue(job_type="counted", idempotency_key="att", payload={"k": 1})
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    assert _attempt(db, job_id) == 1

    # operator requeue of the row (simulated crash recovery) → attempt 2
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE nexus_job_queue SET status = 'pending' WHERE id = ?", (job_id,))
    await queue.resume_pending()
    await _drain(queue, job_id)
    assert _attempt(db, job_id) == 2
    chain = await queue.get_result_chain(job_id)
    assert chain["attempt"] == 2


# ---------------------------------------------------------------------------
# claim-time structural failure: PENDING → FAILED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_job_type_fails_before_reservation(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db, artifact_verifiers={})
    job_id = await queue.enqueue(job_type="ghost", idempotency_key="g1", payload={})
    assert is_failure(await _drain(queue, job_id))
    assert "no handler registered" in (_row_error(db, job_id) or "")
    with sqlite3.connect(db) as connection:
        started_at, attempt = connection.execute(
            "SELECT started_at, attempt FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
    assert started_at is None, "a job that never ran must not carry a start stamp"
    assert attempt == 0


# ---------------------------------------------------------------------------
# §17 — the mandatory invariant, proven on the REAL chain
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_succeeded_implies_valid_stable_traceable_artifact(creative_env: Path) -> None:
    """SUCCEEDED ⇒ stable + valid + traceable artifact, on the real path.

    Real queue + real creative_render handler + real FFmpeg. The job may
    only complete with a verification block that re-measures the artifact
    on disk; the digest in the Result must equal the bytes on disk.
    """
    from nexus_ai_agent.worker import default_job_handlers

    db = creative_env / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    workspace = creative_env / "creative_tmp" / "creative_s17"
    workspace.mkdir(parents=True)
    input_clip = workspace / "input.mp4"
    _clip(input_clip, 2)

    key = "creative:7:8:17"
    job_id = await queue.enqueue(
        job_type=CREATIVE_RENDER_JOB_TYPE,
        idempotency_key=key,
        payload={
            "command": "edit",
            "operation": "trim",
            "args": ["0", "1"],
            "workspace_dir": str(workspace),
            "input_path": str(input_clip),
            "media_duration_us": 2_000_000,
            "user_id": 7,
            "chat_id": 8,
            "lang": "en",
            "idempotency_key": key,
        },
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED

    # the Job Result carries the whole chain back to the command
    chain = await queue.get_result_chain(job_id)
    assert chain["verification_status"] == "verified"
    assert chain["project_id"] == f"shot-{key}"
    assert chain["operation_id"] == "timeline.trim"
    assert chain["command_id"].startswith("cmd-")
    assert chain["attempt"] == 1
    assert isinstance(chain["spec_identity"], dict) and chain["spec_identity"]["operation"] == (
        "timeline.trim"
    )
    artifact = Path(str(chain["physical_identity"]["path"]))
    assert artifact == workspace / "output.mp4"
    assert artifact.is_file(), "the verified artifact must exist"
    assert (int(chain["size_bytes"] or 0)) > 0
    assert chain["sha256"] == sha256_file(artifact), "stable identity: bytes == recorded digest"
    probe = chain["probe"]
    assert isinstance(probe, dict) and probe["duration_us"] > 0

    result = await queue.get_result(job_id)
    assert result is not None
    verification = result[VERIFICATION_RESULT_KEY]
    assert verification["logical_identity"]["project_id"] == f"shot-{key}"
    assert verification["physical_identity"]["sha256"] == chain["sha256"]
    assert isinstance(json.dumps(result), str)  # result is durable/serializable
