"""Gate 5 — the golden job lifecycle, end to end through the real queue.

Everything here drives the *real* seam: ``InProcessJobQueue`` → ``worker`` →
``creative.render_jobs.creative_render_job`` → the canonical Runtime renderer and
its ``verify_artifact`` probe path → atomic publication. Nothing is stubbed
except the media (a tiny lavfi clip) and, where an attack needs it, the verifier
or the renderer call the pipeline is about to trust.

Organisation by boundary:

1. boundaries — the durable running mark, the side-effect gate, and the rule that
   success is only claimable after verification;
2. verification-gate attacks — encoding success + verification failure, ffprobe
   failure, zero-byte artifact, hash mismatch;
3. traceability + reopen — the identity chain and a fresh-process reopen;
4. retry safety — idempotent replay, payload conflict, revision/identity conflict,
   preservation of a verified artifact, retry after a partial write;
5. runtime failure semantics — a failing runtime never becomes SUCCEEDED, and the
   retryable/terminal split follows the documented classification table.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.artifact_publication import (
    SIDECAR_SUFFIX,
    sha256_file,
)
from nexus_ai_agent.application.job_lifecycle import ArtifactTrace, FailureClass, is_terminal
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import ERROR_CODES, creative_render_job
from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video, resolve_ffmpeg_bin

TIMEOUT = 180.0
WORKSPACE_NAME = "creative_ab12cd34ef56"


# ── harness ────────────────────────────────────────────────────────────────


@pytest.fixture()
def client_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The job workspace the payload trusts: under CREATIVE_TEMP_DIR, prefixed."""
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path))
    settings_module.get_settings.cache_clear()
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir(parents=True)
    _make_clip(workspace / "input.mp4")
    yield workspace
    settings_module.get_settings.cache_clear()


def _make_clip(path: Path, *, seconds: int = 2) -> None:
    """Deterministic source media (same recipe as the render-jobs unit harness)."""
    subprocess.run(
        [
            str(resolve_ffmpeg_bin()),
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


def _payload(workspace: Path, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "fa",
        "idempotency_key": "creative:42:4242:111",
    }
    base.update(overrides)
    return base


def _drive(
    queue: InProcessJobQueue,
    payload: dict[str, Any],
    *,
    key: str,
    job_type: str = "creative_render",
) -> tuple[JobStatus, dict[str, Any] | None]:
    """Enqueue + drain inside ONE event loop.

    The adapter spawns the worker as an ``asyncio`` task on the enqueueing loop,
    so splitting enqueue and drain across two ``asyncio.run`` calls would kill
    the worker with its loop (the job would never leave PENDING).
    """

    async def _scenario() -> tuple[JobStatus, dict[str, Any] | None]:
        job_id = await queue.enqueue(job_type=job_type, idempotency_key=key, payload=payload)
        while not is_terminal(await queue.get_status(job_id)):
            await asyncio.sleep(0.02)
        return await queue.get_status(job_id), await queue.get_result(job_id)

    return asyncio.run(asyncio.wait_for(_scenario(), TIMEOUT))


def _run_job(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    key: str,
    queue_dir: str = "jobs",
) -> tuple[JobStatus, dict[str, Any] | None]:
    """One full job: enqueue through the real queue, drain, return status+result."""
    queue = InProcessJobQueue(tmp_path / queue_dir / "jobs.sqlite3")
    queue.register_handler("creative_render", creative_render_job)
    return _drive(queue, payload, key=key)


def _assert_failure(
    status: JobStatus,
    result: dict[str, Any] | None,
    *,
    code: str,
    failure_class: FailureClass,
) -> None:
    """The failure contract: never SUCCEEDED, a known code, a declared class,
    and no artifact claim anywhere in the envelope."""
    assert status is not JobStatus.COMPLETED
    assert result is not None
    assert result["success"] is False
    assert result["error_code"] == code
    assert result["failure_class"] == failure_class.value
    assert result.get("artifact_path") is None
    assert result.get("sha256") is None
    assert result.get("traceability") is None


# ── 1. boundaries ──────────────────────────────────────────────────────────


def test_running_boundary_row_is_processing_while_handler_runs(tmp_path: Path) -> None:
    """PROCESSING is durable *before* the handler body executes (no PENDING work)."""
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"ok": True}

    async def scenario() -> tuple[JobStatus, JobStatus]:
        queue.register_handler("slow", handler)
        job_id = await queue.enqueue(
            job_type="slow", idempotency_key="running-boundary", payload={"n": 1}
        )
        await asyncio.wait_for(started.wait(), 30)
        during = await queue.get_status(job_id)
        release.set()
        while not is_terminal(await queue.get_status(job_id)):
            await asyncio.sleep(0.01)
        return during, await queue.get_status(job_id)

    during, after = asyncio.run(asyncio.wait_for(scenario(), 60))
    assert during is JobStatus.PROCESSING
    assert after is JobStatus.COMPLETED


def test_side_effect_boundary_nothing_at_the_destination_while_running(
    client_workspace: Path, tmp_path: Path
) -> None:
    """No partial artifact at the final path: the destination only ever appears
    through the publication step, and the phase trail proves the order."""
    destination = client_workspace / "output.mp4"
    observed: dict[str, bool] = {}

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        observed["before"] = destination.exists()
        return await creative_render_job(payload)

    async def scenario() -> tuple[list[str], tuple[JobStatus, dict[str, Any]]]:
        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
        queue.register_handler("creative_render", handler)
        job_id = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="side-effect-1",
            payload=_payload(client_workspace),
        )
        while not is_terminal(await queue.get_status(job_id)):
            await asyncio.sleep(0.02)
        result = await queue.get_result(job_id)
        assert result is not None
        return list(result["phase_trail"]), (await queue.get_status(job_id), result)

    trail, (status, result) = asyncio.run(asyncio.wait_for(scenario(), TIMEOUT))
    assert observed["before"] is False, "nothing may exist before the run publishes"
    assert status is JobStatus.COMPLETED
    assert trail == ["pending", "running", "verifying", "succeeded"]
    assert result["traceability"]["job_id"], "the queue stamps the row identity into the trace"
    assert destination.is_file()


def test_success_requires_verification_and_is_fully_evidenced(
    client_workspace: Path, tmp_path: Path
) -> None:
    """SUCCEEDED ⇔ verified bytes: measured hash, probe facts, identity chain."""
    status, result = _run_job(tmp_path, _payload(client_workspace), key="success-1")
    assert status is JobStatus.COMPLETED
    assert result is not None
    destination = Path(result["artifact_path"])
    assert result["success"] is True
    assert result["artifact_kind"] == "video"
    assert destination.is_file() and destination.stat().st_size > 0
    assert result["sha256"] == sha256_file(destination), "the claim is measured, not copied"

    info = probe_video(destination, binary=resolve_ffmpeg_bin())
    assert info.duration_us == result["duration_us"] == 1_000_000
    # the lane normalizes every master to the documented profile (1280x720)
    assert info.width == 1280 and info.height == 720 == result["height"]

    trace = ArtifactTrace.reconstruct(str(result["traceability"]["job_id"]), result)
    assert trace.physical_sha256 == result["sha256"]
    assert trace.size_bytes == destination.stat().st_size
    assert trace.artifact_path == str(destination)
    assert trace.operation_id == "timeline.trim"
    assert trace.revision is not None
    assert trace.verification["prover"], "the verification evidence names its prover"
    assert trace.verification["expected_sha256_cross_checked"] == result["sha256"]


# ── 2. verification-gate attacks ───────────────────────────────────────────


def test_encoding_success_but_verification_failure_is_not_success(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-B closed: the encoder's success is not the job's success. The worker
    re-verifies the staged bytes with the canonical verifier; when that refuses,
    the job is a typed failure and nothing is published."""
    import nexus_ai_agent.creative.artifacts as artifacts_mod
    from nexus_ai_agent.creative.artifacts import ArtifactVerificationError

    def _lying_probe(path: Path, **kwargs: Any) -> Any:
        raise ArtifactVerificationError(f"ffprobe rejected the staged artifact: {path}")

    monkeypatch.setattr(artifacts_mod, "verify_artifact", _lying_probe)

    status, result = _run_job(tmp_path, _payload(client_workspace), key="verify-fail-1")
    _assert_failure(
        status,
        result,
        code="artifact_verification_failed",
        failure_class=FailureClass.RETRYABLE,
    )
    assert result is not None and "ffprobe rejected" in str(result["error_detail"])
    assert not (client_workspace / "output.mp4").exists(), "a refused publish writes nothing"
    assert not (client_workspace / f"output.mp4{SIDECAR_SUFFIX}").exists()
    assert not list(client_workspace.glob(".staging-*")), "staging is cleaned up"


def test_ffprobe_failure_never_launders_an_existing_artifact(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing prover can neither succeed nor damage the artifact already on
    disk: the failure is typed, and the previous verified bytes stay identical."""
    status, result = _run_job(tmp_path, _payload(client_workspace), key="probe-1")
    assert status is JobStatus.COMPLETED and result is not None
    destination = Path(result["artifact_path"])
    healthy_sha = sha256_file(destination)
    sidecar = destination.with_name(destination.name + SIDECAR_SUFFIX)
    sidecar_bytes = sidecar.read_bytes()

    import nexus_ai_agent.creative.artifacts as artifacts_mod
    from nexus_ai_agent.creative.artifacts import ArtifactVerificationError

    def _refuse(path: Path, **kwargs: Any) -> Any:
        raise ArtifactVerificationError(f"ffprobe cannot read {path}")

    monkeypatch.setattr(artifacts_mod, "verify_artifact", _refuse)
    status2, result2 = _run_job(
        tmp_path, _payload(client_workspace), key="probe-2", queue_dir="jobs2"
    )
    _assert_failure(
        status2,
        result2,
        code="artifact_verification_failed",
        failure_class=FailureClass.RETRYABLE,
    )
    assert sha256_file(destination) == healthy_sha, "the verified artifact is untouched"
    assert sidecar.read_bytes() == sidecar_bytes, "the identity record is untouched"
    assert not list(client_workspace.glob("*.invalid-*")), "nothing was condemned"


def test_zero_byte_artifact_is_not_success(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The encoder returns a zero-byte file: the canonical verifier refuses it and
    the job can never be SUCCEEDED (no optimistic size checks at the seam)."""
    import nexus_ai_agent.creative.rendering.executor as executor_mod

    real_render_lane = executor_mod.render_lane

    def _truncating_render_lane(ir: Any, output: Path, **kwargs: Any) -> Any:
        artifact = real_render_lane(ir, output, **kwargs)
        Path(output).write_bytes(b"")  # a crashed/empty encoder product
        return artifact

    monkeypatch.setattr(executor_mod, "render_lane", _truncating_render_lane)
    status, result = _run_job(tmp_path, _payload(client_workspace), key="zero-1")
    _assert_failure(
        status,
        result,
        code="artifact_verification_failed",
        failure_class=FailureClass.RETRYABLE,
    )
    assert result is not None and "empty" in str(result["error_detail"])
    assert not (client_workspace / "output.mp4").exists()


def test_artifact_hash_mismatch_is_refused(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane's own digest is pinned into the verifier call: bytes that changed
    between encode and verification can never be published as SUCCEEDED."""
    import nexus_ai_agent.creative.rendering.executor as executor_mod

    real_render_lane = executor_mod.render_lane

    def _swapping_render_lane(ir: Any, output: Path, **kwargs: Any) -> Any:
        artifact = real_render_lane(ir, output, **kwargs)
        Path(output).write_bytes(b"substituted bytes that are not the render")
        return artifact

    monkeypatch.setattr(executor_mod, "render_lane", _swapping_render_lane)
    status, result = _run_job(tmp_path, _payload(client_workspace), key="swap-1")
    _assert_failure(
        status,
        result,
        code="artifact_verification_failed",
        failure_class=FailureClass.RETRYABLE,
    )
    assert not (client_workspace / "output.mp4").exists()


# ── 3. traceability + reopen ───────────────────────────────────────────────


def test_identity_chain_is_traceable_and_reconstructible(
    client_workspace: Path, tmp_path: Path
) -> None:
    """command → job → project → operation → revision → identities → sha → evidence."""
    payload = _payload(client_workspace)
    status, result = _run_job(tmp_path, payload, key="trace-1")
    assert status is JobStatus.COMPLETED and result is not None

    job_id = str(result["traceability"]["job_id"])
    trace = ArtifactTrace.reconstruct(job_id, result)
    assert trace.command_id == f"cmd-{payload['idempotency_key']}-timeline.trim"
    assert trace.project_id == f"shot-{payload['idempotency_key']}"
    assert trace.operation_id == "timeline.trim"
    assert trace.job_id == job_id
    assert isinstance(trace.revision, int)
    assert trace.state_hash is not None
    assert trace.logical_content_identity.startswith("sha256:")
    assert trace.render_spec_hash is not None
    assert trace.physical_sha256 == sha256_file(Path(trace.artifact_path))
    assert trace.size_bytes == Path(trace.artifact_path).stat().st_size
    assert trace.verification["prover"]

    # the destination is self-describing for the next process: the sidecar
    # records the exact identity the publisher used
    record = json.loads(Path(str(trace.artifact_path) + SIDECAR_SUFFIX).read_text(encoding="utf-8"))
    assert record["identity"]["idempotency_key"] == payload["idempotency_key"]
    assert record["identity"]["operation"] == "timeline.trim"
    assert record["identity"]["state_revision"] == trace.revision
    assert record["artifact"]["physical_artifact_sha256"] == trace.physical_sha256


def test_fresh_process_reopen_keeps_identity_and_evidence(
    client_workspace: Path, tmp_path: Path
) -> None:
    """A new queue instance (process restart) sees the same terminal row and the
    same artifact evidence, and re-running is a no-op for the bytes."""
    db = tmp_path / "jobs.sqlite3"
    queue = InProcessJobQueue(db)
    queue.register_handler("creative_render", creative_render_job)

    async def _scenario() -> tuple[JobStatus, dict[str, Any] | None, str]:
        job_id = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="reopen-1",
            payload=_payload(client_workspace),
        )
        while not is_terminal(await queue.get_status(job_id)):
            await asyncio.sleep(0.02)
        return await queue.get_status(job_id), await queue.get_result(job_id), job_id

    status, result, job_id = asyncio.run(asyncio.wait_for(_scenario(), TIMEOUT))
    assert status is JobStatus.COMPLETED
    assert result is not None
    digest_before = result["sha256"]

    reopened = InProcessJobQueue(db)
    assert asyncio.run(reopened.get_status(job_id)) is JobStatus.COMPLETED
    reopened_result = asyncio.run(reopened.get_result(job_id))
    assert reopened_result == result
    trace = ArtifactTrace.reconstruct(job_id, reopened_result or {})
    assert Path(trace.artifact_path).is_file()
    assert sha256_file(Path(trace.artifact_path)) == digest_before


# ── 4. retry safety / idempotency ──────────────────────────────────────────


def test_idempotent_replay_is_byte_stable(client_workspace: Path, tmp_path: Path) -> None:
    """Same key + same payload in two processes → identical artifact evidence."""
    payload = _payload(client_workspace)
    first_status, first = _run_job(tmp_path, payload, key="replay-1", queue_dir="a")
    second_status, second = _run_job(tmp_path, payload, key="replay-1", queue_dir="b")
    assert first_status is JobStatus.COMPLETED and second_status is JobStatus.COMPLETED
    assert first is not None and second is not None
    assert first["sha256"] == second["sha256"], "the encoder is deterministic for one spec"
    for field in ("logical_content_identity", "render_spec_hash"):
        assert first["traceability"][field] == second["traceability"][field]
    assert first["sha256"] == first["traceability"]["physical_sha256"] == second["sha256"]

    # replay inside one process: the same key returns the same row
    queue = InProcessJobQueue(tmp_path / "c" / "jobs.sqlite3")
    queue.register_handler("creative_render", creative_render_job)

    async def _replay() -> tuple[str, str, JobStatus]:
        first = await queue.enqueue(
            job_type="creative_render", idempotency_key="replay-1b", payload=payload
        )
        second = await queue.enqueue(
            job_type="creative_render", idempotency_key="replay-1b", payload=payload
        )
        while not is_terminal(await queue.get_status(first)):
            await asyncio.sleep(0.02)
        return first, second, await queue.get_status(first)

    first_id, second_id, replay_status = asyncio.run(asyncio.wait_for(_replay(), TIMEOUT))
    assert first_id == second_id
    assert replay_status is JobStatus.COMPLETED


def test_payload_conflict_same_key_is_refused(client_workspace: Path, tmp_path: Path) -> None:
    """Same key + different payload is a typed conflict, never a second render."""
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    queue.register_handler("creative_render", creative_render_job)
    changed = _payload(client_workspace, args=["0", "2"])

    async def _conflict() -> tuple[JobStatus, dict[str, Any] | None]:
        job_id = await queue.enqueue(
            job_type="creative_render",
            idempotency_key="conflict-1",
            payload=_payload(client_workspace),
        )
        with pytest.raises(ValueError, match="idempotency key reused"):
            await queue.enqueue(
                job_type="creative_render", idempotency_key="conflict-1", payload=changed
            )
        while not is_terminal(await queue.get_status(job_id)):
            await asyncio.sleep(0.02)
        return await queue.get_status(job_id), await queue.get_result(job_id)

    status, result = asyncio.run(asyncio.wait_for(_conflict(), TIMEOUT))
    assert status is JobStatus.COMPLETED
    assert result is not None and result["success"] is True


def test_revision_conflict_same_destination_is_refused(
    client_workspace: Path, tmp_path: Path
) -> None:
    """A changed revision/identity aimed at the same destination is refused: the
    verified bytes of the winning revision are never silently overwritten."""
    payload = _payload(client_workspace)
    status, result = _run_job(tmp_path, payload, key="rev-1")
    assert status is JobStatus.COMPLETED and result is not None
    destination = Path(result["artifact_path"])
    winning_sha = sha256_file(destination)

    # revision N+1 of the same logical request: a different idempotency key
    # (hence a different command/project identity) at the same destination
    newer = _payload(client_workspace, args=["0", "2"], idempotency_key="creative:42:4242:111:v2")
    status2, result2 = _run_job(tmp_path, newer, key="rev-2", queue_dir="jobs2")
    _assert_failure(
        status2,
        result2,
        code="artifact_destination_conflict",
        failure_class=FailureClass.TERMINAL,
    )
    assert result2 is not None and "cannot claim" in str(result2["error_detail"])
    assert sha256_file(destination) == winning_sha, "the verified artifact survives"
    assert (
        json.loads(
            destination.with_name(destination.name + SIDECAR_SUFFIX).read_text(encoding="utf-8")
        )["identity"]["idempotency_key"]
        == payload["idempotency_key"]
    )


def test_retry_preserves_verified_artifact(client_workspace: Path, tmp_path: Path) -> None:
    """A retry of the same job reuses the verified artifact instead of rewriting
    it; a *tampered* destination is caught, quarantined and healed."""
    payload = _payload(client_workspace)
    status, result = _run_job(tmp_path, payload, key="keep-1")
    assert status is JobStatus.COMPLETED and result is not None
    destination = Path(result["artifact_path"])
    digest = sha256_file(destination)
    inode = destination.stat().st_ino

    retry = asyncio.run(creative_render_job(payload))
    assert retry["success"] is True
    assert retry["sha256"] == digest
    assert retry["traceability"]["verification"]["reused_existing_artifact"] is True
    assert destination.stat().st_ino == inode, "a verified artifact is reused, not rewritten"

    destination.write_bytes(b"tampered after publication")
    healed = asyncio.run(creative_render_job(payload))
    assert healed["success"] is True
    assert sha256_file(destination) == digest
    assert healed["traceability"]["verification"]["reused_existing_artifact"] is False
    assert list(client_workspace.glob("output.mp4.invalid-*")), "invalid bytes are kept"


def test_retry_after_partial_write_uses_atomic_publication(
    client_workspace: Path, tmp_path: Path
) -> None:
    """A leftover partial file from a crashed attempt is never mistaken for the
    artifact: publication replaces it with verified bytes and removes staging."""
    destination = client_workspace / "output.mp4"
    destination.write_bytes(b"partial garbage from a crashed render")

    status, result = _run_job(tmp_path, _payload(client_workspace), key="partial-1")
    assert status is JobStatus.COMPLETED and result is not None
    published = Path(result["artifact_path"])
    assert published.read_bytes() != b"partial garbage from a crashed render"
    assert published.stat().st_size > 0
    info = probe_video(published, binary=resolve_ffmpeg_bin())
    assert info.duration_us == 1_000_000
    assert not list(client_workspace.glob("*.staging-*"))
    assert not list(client_workspace.glob("*.part*")), "no lane temp file survives"


# ── 5. runtime failure semantics ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "code", "failure_class"),
    [
        (
            {"operation": "definitely_not_an_operation"},
            "unsupported_operation",
            FailureClass.TERMINAL,
        ),
        ({"input_path": "MISSING"}, "media_missing", FailureClass.TERMINAL),
        ({"input_path": "/tmp/escaped.mp4"}, "invalid_request", FailureClass.TERMINAL),
    ],
)
def test_runtime_failure_never_becomes_success(
    client_workspace: Path,
    tmp_path: Path,
    overrides: dict[str, Any],
    code: str,
    failure_class: FailureClass,
) -> None:
    resolved = {
        key: (str(client_workspace / "missing.mp4") if value == "MISSING" else value)
        for key, value in overrides.items()
    }
    payload = _payload(client_workspace, **resolved)
    status, result = _run_job(tmp_path, payload, key=f"fail-{code}")
    _assert_failure(status, result, code=code, failure_class=failure_class)
    assert result is not None and result["error_detail"]
    assert not (client_workspace / "output.mp4").exists()


def test_retryable_vs_terminal_status_follows_classification(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both failure statuses exist because both failure kinds really occur."""
    terminal_status, terminal_result = _run_job(
        tmp_path,
        _payload(client_workspace, operation="nonexistent"),
        key="cls-terminal",
    )
    assert terminal_status is JobStatus.TERMINAL_FAILED
    assert terminal_result is not None
    assert terminal_result["failure_class"] == FailureClass.TERMINAL.value
    assert terminal_result["error_code"] in ERROR_CODES

    import nexus_ai_agent.creative.slideshow.ffmpeg as ffmpeg_mod

    def _missing() -> str:
        raise ffmpeg_mod.FfmpegUnavailableError("ffmpeg is not installed (test)")

    monkeypatch.setattr(ffmpeg_mod, "resolve_ffmpeg_bin", _missing)
    retry_status, retry_result = _run_job(
        tmp_path,
        _payload(client_workspace),
        key="cls-retryable",
        queue_dir="jobs2",
    )
    assert retry_status is JobStatus.FAILED_RETRYABLE
    assert retry_result is not None
    assert retry_result["failure_class"] == FailureClass.RETRYABLE.value
    assert retry_result["error_code"] == "ffmpeg_unavailable"
    assert retry_result["error_code"] in ERROR_CODES
    assert not (client_workspace / "output.mp4").exists()


def test_unexpected_worker_error_is_durable_failed_without_artifact_claim(
    client_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal crash is a durable FAILED row (unclassified), never a success
    and never a typed failure that pretends to know what happened."""
    import nexus_ai_agent.creative.render_jobs as render_jobs_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated internal crash")

    monkeypatch.setattr(render_jobs_mod, "_guarded_input", _boom)
    status, result = _run_job(tmp_path, _payload(client_workspace), key="crash-1")
    assert status is JobStatus.FAILED
    if result is not None:
        assert result.get("success") is not True
        assert result.get("artifact_path") is None
    assert not (client_workspace / "output.mp4").exists()


def test_seam_contract_is_stable() -> None:
    """The public seam other layers depend on (worker registry, bot notifiers)."""
    assert callable(creative_render_job)
    assert {
        "invalid_request",
        "unsupported_operation",
        "media_missing",
        "ffmpeg_unavailable",
        "render_failed",
        "caption_profile_unavailable",
        "artifact_verification_failed",
        "artifact_destination_conflict",
    } <= set(ERROR_CODES)
