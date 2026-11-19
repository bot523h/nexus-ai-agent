"""NAGAR One V1: the real ``storyboard_render`` job through the real queue.

REAL-COMPUTE: a real ffmpeg render. REAL-DISK: the MP4, passport and durable
approval SQLite file are written to and read from a temporary studio workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    default_artifact_verifiers,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative import storyboard_jobs
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_jobs import (
    ENV_SIGNING_KEY,
    ENV_WORKSPACE,
    storyboard_render_job,
    storyboard_render_verifier,
)
from nexus_ai_agent.creative.storyboard_render import (
    StoryboardRenderError,
    approval_identity_for,
    issue_render_approval,
)
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalAlreadyConsumedError,
    ApprovalError,
    ApprovalState,
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.worker import default_job_handlers

SIGNING_KEY = "k" * 48
SCRIPT = (
    "INT. KITCHEN - DAY\n"
    "Sara: Hello, are you there?\n"
    "Ali: Yes, I am here.\n"
    "EXT. STREET - NIGHT\n"
    "سارا: سلام، حالت چطوره؟\n"
)
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}
JOB_ID = "job-queue-1"
PROJECT_ID = "proj-queue-1"
PRINCIPAL_ID = "owner-queue-1"


@pytest.fixture()
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure the studio via environment, approve one render, return the workspace."""
    workspace = tmp_path / "studio"
    workspace.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
    monkeypatch.setenv(ENV_SIGNING_KEY, SIGNING_KEY)
    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=profile_hash,
        )
    finally:
        ledger.close()
    assert approval_id
    return workspace


def _payload(approval_id: str) -> dict[str, object]:
    compiled = compile_storyboard(SCRIPT)
    return build_storyboard_render_payload(
        {
            "project_id": PROJECT_ID,
            "job_id": JOB_ID,
            "screenplay_text": SCRIPT,
            "source_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
            "manifest_sha256": compiled.manifest_sha256,
            "approval_id": approval_id,
            "principal_id": PRINCIPAL_ID,
            **PROFILE,
        }
    )


def _approval_id(workspace: Path) -> str:
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        rows = ledger._all()
        assert len(rows) == 1
        return rows[0].approval_id
    finally:
        ledger.close()


async def _drive_through_queue(
    db_path: Path, payload: dict[str, object]
) -> tuple[JobStatus, dict[str, object] | None]:
    queue = InProcessJobQueue(
        db_path,
        handlers=default_job_handlers(),
        artifact_verifiers=default_artifact_verifiers(),
    )
    await queue.acquire_ownership()
    job_id = await queue.enqueue(
        job_type="storyboard_render",
        idempotency_key="storyboard-queue-1",
        payload=payload,
    )
    deadline = time.monotonic() + 180
    while True:
        status = await queue.get_status(job_id)
        if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL):
            return status, await queue.get_result(job_id)
        if time.monotonic() > deadline:
            raise AssertionError(f"job still {status} after 180s")
        await asyncio.sleep(0.2)


def test_storyboard_job_completes_through_real_queue_with_verified_artifact(
    studio: Path, tmp_path: Path
) -> None:
    payload = _payload(_approval_id(studio))
    status, result = asyncio.run(_drive_through_queue(tmp_path / "queue.sqlite3", payload))

    assert status is JobStatus.COMPLETED
    assert result is not None
    assert result["status"] == "rendered"
    assert result["verifier_status"] == "PASS"
    mp4 = studio / str(result["output_rel_path"])
    passport = studio / str(result["passport_rel_path"])
    assert mp4.is_file() and mp4.stat().st_size > 0
    assert passport.is_file()


def test_retry_publishes_from_staging_and_consumes_the_approval(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attempt=1 crashes after the side effects -> attempt=2 completes.

    Attempt 1 consumes the approval and stages its MP4/passport in the
    ``.staging`` quarantine, then the process "dies" (the handler hangs and
    the queue is shut down, reopening the row). Attempt 2 re-reads the
    consumed approval through the ``(queue_job_id, attempt)`` reservation
    fence, stages its own quarantine, and only the queue's independent PASS
    verdict promotes it: the final path appears, the quarantine of the
    winning attempt is emptied, and the approval ends CONSUMED with a
    finalized reservation.
    """
    hang = threading.Event()

    real_render = storyboard_jobs.render_storyboard_sync

    def crashing_render(payload, ctx, **kwargs):  # noqa: ANN001, ANN202
        real_render(payload, ctx, **kwargs)
        # The side effects (approval consumption + staged files) are on disk;
        # the execution now "crashes" before it could ever complete.
        hang.wait(timeout=60)
        raise RuntimeError("simulated crash after side effects")

    # A dedicated patch scope: restoring it must not touch the fixture's env
    # patches (a shared monkeypatch.undo() would wipe NEXUS_STUDIO_WORKSPACE).
    monkeypatch.setattr(storyboard_jobs, "render_storyboard_sync", crashing_render)

    db_path = tmp_path / "retry-queue.sqlite3"
    payload = _payload(_approval_id(studio))

    async def scenario() -> tuple[str, dict[str, object]]:
        queue = InProcessJobQueue(
            db_path,
            handlers=default_job_handlers(),
            artifact_verifiers=default_artifact_verifiers(),
        )
        await queue.acquire_ownership()
        job_id = await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="storyboard-retry-1",
            payload=payload,
        )
        # Wait until attempt 1 has staged its quarantined artifacts.
        staged_passports = studio / "artifacts" / ".staging" / job_id / "attempt-1"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if list(staged_passports.glob("*.passport.json")):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("attempt 1 never staged its quarantine artifacts")
        # The process dies: shutdown reopens the in-flight attempt.
        await queue.shutdown()
        hang.set()
        # Restore the real chain for the successor process (attempt 2).
        monkeypatch.setattr(storyboard_jobs, "render_storyboard_sync", real_render)

        successor = InProcessJobQueue(
            db_path,
            handlers=default_job_handlers(),
            artifact_verifiers=default_artifact_verifiers(),
        )
        await successor.acquire_ownership()
        await successor.resume_pending()
        deadline = time.monotonic() + 180
        while True:
            status = await successor.get_status(job_id)
            if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL):
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"job still {status} after 180s")
            await asyncio.sleep(0.2)
        result = await successor.get_result(job_id)
        await successor.shutdown()
        return job_id, dict(result or {})

    queue_job_id, result = asyncio.run(scenario())

    assert result.get("status") == "rendered", result
    # The winning attempt's quarantine was promoted to the final path...
    final_mp4 = Path(str(result["final_output_path"]))
    final_passport = Path(str(result["final_passport_path"]))
    assert final_mp4.is_file() and final_mp4.stat().st_size > 0
    assert final_passport.is_file()
    assert result["output_path"] == str(final_mp4)
    assert result["approval_id"]
    assert result["attempt"] == 2
    # ...and the promoted result re-verifies at its final path.
    assert storyboard_render_verifier(payload, result).ok is True

    quarantine = studio / "artifacts" / ".staging" / queue_job_id
    attempt1 = quarantine / "attempt-1"
    attempt2 = quarantine / "attempt-2"
    # The crashed attempt's quarantine was NEVER promoted: its files are the
    # only ones allowed to stay behind.
    assert list(attempt1.glob("*.mp4")) and list(attempt1.glob("*.passport.json"))
    # The winning attempt's quarantine is empty after the atomic promotion.
    assert not list(attempt2.glob("*.mp4")) and not list(attempt2.glob("*.passport.json"))

    approval_id = str(result["approval_id"])
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        binding = ledger.get(approval_id)
        assert binding.state is ApprovalState.CONSUMED
        reservation = ledger._read_reservation(approval_id)
        assert reservation is not None
        assert reservation[0] == queue_job_id
        assert reservation[3] == 2
        assert reservation[2] is not None, "reservation must be finalized after COMPLETED"
        # A superseded attempt can never finalize the newer attempt's reservation.
        with pytest.raises(ApprovalAlreadyConsumedError):
            ledger.finalize_consumption(approval_id, job_id=queue_job_id, attempt=1)
        # The finalized reservation no longer admits recovery consumption.
        identity = approval_identity_for(
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
            script_sha256=compile_storyboard(SCRIPT).script_sha256,
            manifest_sha256=compile_storyboard(SCRIPT).manifest_sha256,
            render_profile_hash=mz.RenderProfile.from_mapping(PROFILE).profile_hash(),
        )
        with pytest.raises(ApprovalError, match="replay"):
            ledger.consume(approval_id, identity=identity, job_id=queue_job_id, attempt=2)
    finally:
        ledger.close()


def _render_once(studio: Path) -> tuple[dict[str, object], dict[str, object]]:
    payload = _payload(_approval_id(studio))
    result = asyncio.run(storyboard_render_job(payload))
    return payload, result


def test_forged_passport_is_refused_with_passport_prefixed_code(studio: Path) -> None:
    payload, result = _render_once(studio)
    passport_path = studio / str(result["passport_rel_path"])
    forged = json.loads(passport_path.read_text(encoding="utf-8"))
    forged["probed_duration_s"] = 9999.0  # field changed, seal left untouched
    passport_path.write_text(json.dumps(forged), encoding="utf-8")

    outcome = storyboard_render_verifier(payload, result)

    assert outcome.ok is False
    assert outcome.reason_code == "passport_seal_mismatch"
    assert outcome.reason_code is not None and outcome.reason_code.startswith("passport_")


def test_output_outside_workspace_is_refused_explicitly(studio: Path) -> None:
    payload, result = _render_once(studio)
    escaped = dict(result)
    escaped["output_rel_path"] = "../escaped.mp4"

    outcome = storyboard_render_verifier(payload, escaped)

    assert outcome.ok is False
    assert outcome.reason_code == "path_outside_workspace"


def test_verifier_refuses_when_studio_is_not_configured(
    studio: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _render_once(studio)
    monkeypatch.delenv(ENV_WORKSPACE)

    outcome = storyboard_render_verifier(payload, result)

    assert outcome.ok is False
    assert outcome.reason_code == "studio_not_configured"


def test_local_reference_image_content_is_bound_into_the_approval(
    studio: Path, tmp_path: Path
) -> None:
    """A shot's local plate hashes into the render profile the approval binds."""
    from nexus_ai_agent.creative.storyboard_render import (
        local_image_content_digests,
        render_profile_hash,
    )

    plate = studio / "plates" / "plate.png"
    plate.parent.mkdir(parents=True, exist_ok=True)
    mz.write_slate_png(plate, width=320, height=180, seed=b"\x07" * 32, label="plate")
    local_images = {"s000-sh000": "plates/plate.png"}
    image_digests = local_image_content_digests(studio, local_images)
    assert image_digests == {"s000-sh000": mz.stream_sha256(plate)[0]}

    profile = mz.RenderProfile.from_mapping(PROFILE)
    extended_hash = render_profile_hash(profile, image_digests)
    assert extended_hash != profile.profile_hash()
    assert render_profile_hash(profile) == profile.profile_hash()

    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        approval_id = issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=extended_hash,
        )
    finally:
        ledger.close()

    payload = {**_payload(approval_id), "local_images": local_images}
    result = asyncio.run(storyboard_render_job(payload))
    assert result["status"] == "rendered"
    assert result["render_profile_hash"] == extended_hash

    # The plate's BYTES are the identity: swap them and even a freshly issued
    # approval bound to the OLD content is refused — the path itself proves
    # nothing.
    mz.write_slate_png(plate, width=320, height=180, seed=b"\x08" * 32, label="plate")
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        stale_approval_id = issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=extended_hash,
        )
    finally:
        ledger.close()
    stale_payload = {**_payload(stale_approval_id), "local_images": local_images}
    with pytest.raises(StoryboardRenderError, match="approval_identity_mismatch_preview_digest"):
        asyncio.run(storyboard_render_job(stale_payload))


def test_staging_helpers_refuse_unsafe_components() -> None:
    from nexus_ai_agent.creative.storyboard_render import staged_artifact_paths

    mp4, passport = staged_artifact_paths(
        Path("/studio/artifacts"), queue_job_id="job-123", attempt=2, job_id="job-cli"
    )
    assert mp4 == Path("/studio/artifacts/.staging/job-123/attempt-2/job-cli.mp4")
    assert passport.name == "job-cli.passport.json"
    for bad in ("../escape", "a/b", "", " ", ".hidden-ok-x" * 30):
        with pytest.raises(StoryboardRenderError, match="invalid_queue_job_id"):
            staged_artifact_paths(
                Path("/studio/artifacts"), queue_job_id=bad, attempt=1, job_id="job-cli"
            )
    with pytest.raises(StoryboardRenderError, match="invalid_attempt"):
        staged_artifact_paths(
            Path("/studio/artifacts"), queue_job_id="job-123", attempt=0, job_id="job-cli"
        )


# --------------------------------------------------------------------------- #
# Real cross-process crash tests: a worker process is killed with os._exit(99)
# at the critical boundaries, and a brand-new process must recover durably.
# The child runs under `python -I` (isolated: no PYTHONPATH, no user site) with
# the repository's absolute `src` path injected at the front of `sys.path`.
# --------------------------------------------------------------------------- #

_REPO_SRC = str(Path(__file__).resolve().parents[2] / "src")

_CHILD_SCRIPT = (
    "import sys\nsys.path.insert(0, "
    + repr(_REPO_SRC)
    + ")\n"
    + """
import asyncio
import json
import os
import sqlite3
import time

MODE, DB, PAYLOAD, WORKSPACE = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

from nexus_ai_agent.adapters.in_process_job_queue import (
    InProcessJobQueue,
    default_artifact_verifiers,
)
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative import storyboard_jobs
from nexus_ai_agent.worker import default_job_handlers

with open(PAYLOAD, encoding="utf-8") as handle:
    PAYLOAD_JSON = json.load(handle)


def _queue_job_id():
    connection = sqlite3.connect(DB)
    try:
        return str(connection.execute("SELECT id FROM nexus_job_queue").fetchone()[0])
    finally:
        connection.close()


async def _drive(queue, job_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        status = await queue.get_status(job_id)
        if status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED_TERMINAL,
            JobStatus.FAILED_RETRYABLE,
        ):
            return status
        if time.monotonic() > deadline:
            raise SystemExit("job still " + str(status) + " after " + str(timeout) + "s")
        await asyncio.sleep(0.2)


async def main():
    # The crash-point patches MUST be in place before the handler/verifier
    # maps are built: both factories read the module attributes at call time.
    if MODE == "crash-after-render":
        real_render = storyboard_jobs.render_storyboard_sync

        def crashing_render(payload, ctx, **kwargs):
            real_render(payload, ctx, **kwargs)
            # Approval consumed + .staging written; the process now dies for
            # real, before the queue could ever verify or promote.
            os._exit(99)

        storyboard_jobs.render_storyboard_sync = crashing_render
    elif MODE == "die-at-finalize":
        def dying_finalize(approval_id, *, job_id, attempt):
            # The row is already durably COMPLETED; the process dies BEFORE
            # the approval reservation is closed.
            os._exit(99)

        storyboard_jobs.finalize_storyboard_approval = dying_finalize
    elif MODE == "hang-in-verifier":
        real_verifier = storyboard_jobs.storyboard_render_verifier

        def hanging_verifier(payload, result):
            outcome = real_verifier(payload, result)
            # attempt 1 is VERIFYING with staged bytes: announce it, then hang
            # after the PASS verdict, right before the queue would promote.
            with open(os.path.join(WORKSPACE, ".phase-verifying"), "w") as handle:
                handle.write("1")
            release = os.path.join(WORKSPACE, ".release")
            while not os.path.exists(release):
                time.sleep(0.05)
            return outcome

        storyboard_jobs.storyboard_render_verifier = hanging_verifier

    queue = InProcessJobQueue(
        DB,
        handlers=default_job_handlers(),
        artifact_verifiers=default_artifact_verifiers(),
    )
    # The takeover process deliberately does NOT acquire the exclusive
    # ownership lock: this is the CAS-fencing coexistence path (the queue's
    # own contract says fencing tests must not take the sidecar lock).
    if MODE != "takeover":
        await queue.acquire_ownership()
    if MODE == "crash-after-render":
        await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="sb-subprocess-crash",
            payload=PAYLOAD_JSON,
        )
        # The worker thread dies with os._exit(99) inside the render; this
        # coroutine simply keeps the process alive until that happens.
        await asyncio.sleep(3600)
    elif MODE == "die-at-finalize":
        job_id = await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="sb-subprocess-finalize",
            payload=PAYLOAD_JSON,
        )
        await _drive(queue, job_id, 240)
        await asyncio.sleep(2)  # let the dying finalize land before shutdown
        await queue.shutdown()
    elif MODE == "successor":
        # A brand-new process: startup recovery requeues the dead owner's row
        # (attempt 2), renders, verifies, promotes and finalizes.
        await queue.resume_pending()
        job_id = _queue_job_id()
        await _drive(queue, job_id, 240)
        await queue.shutdown()
    elif MODE == "reconciler":
        # Startup recovery only: resume_pending runs the completed-jobs
        # reconciliation that closes the orphaned approval reservation.
        await queue.resume_pending()
        await queue.shutdown()
    elif MODE == "hang-in-verifier":
        job_id = await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="sb-subprocess-fence",
            payload=PAYLOAD_JSON,
        )
        await _drive(queue, job_id, 600)
        await queue.shutdown()
    elif MODE == "takeover":
        from datetime import timedelta

        job_id = _queue_job_id()
        for _ in range(90):
            recovered = await queue.recover_job(job_id, stale_after=timedelta(seconds=1))
            if recovered:
                break
            await asyncio.sleep(1.0)
        else:
            raise SystemExit("takeover never happened")
        await _drive(queue, job_id, 240)
        await queue.shutdown()
    else:
        raise SystemExit("unknown mode " + MODE)


asyncio.run(main())
"""
)


def _run_child(
    mode: str, db: Path, payload_file: Path, workspace: Path, *, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, isolated interpreter
        [
            sys.executable,
            "-I",
            "-c",
            _CHILD_SCRIPT,
            mode,
            str(db),
            str(payload_file),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
    )


def _queue_row(db: Path) -> dict[str, object]:
    import sqlite3

    connection = sqlite3.connect(db)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, status, attempt, result_json, error FROM nexus_job_queue"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def _studio_quarantine(studio: Path) -> Path:
    return studio / "artifacts" / ".staging"


def test_subprocess_death_after_render_recovers_on_attempt_2(studio: Path, tmp_path: Path) -> None:
    """A real os._exit(99) after the side effects: nothing is published, and a
    brand-new process recovers the same approval and completes the job."""
    db = tmp_path / "crash-queue.sqlite3"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_payload(_approval_id(studio))), encoding="utf-8")

    crash = _run_child("crash-after-render", db, payload_file, studio)
    assert crash.returncode == 99, crash.stderr[-2000:]

    # Nothing was ever published to the final path...
    renders = studio / "projects" / PROJECT_ID / "renders" / JOB_ID
    assert not (renders / "storyboard.mp4").exists()
    assert not (renders / "storyboard.passport.json").exists()
    # ...and the crashed attempt's quarantine keeps its evidence.
    attempt1 = list(_studio_quarantine(studio).glob("*/attempt-1"))
    assert len(attempt1) == 1
    assert list(attempt1[0].glob("*.mp4")) and list(attempt1[0].glob("*.passport.json"))

    successor = _run_child("successor", db, payload_file, studio)
    assert successor.returncode == 0, successor.stderr[-2000:]

    row = _queue_row(db)
    assert row["status"] == "completed"
    assert row["attempt"] == 2
    result = json.loads(str(row["result_json"]))
    assert result["status"] == "rendered"
    assert result["attempt"] == 2
    assert result["approval_id"]
    # The successor published the final pair...
    assert (renders / "storyboard.mp4").is_file()
    assert (renders / "storyboard.passport.json").is_file()
    # ...its own quarantine was emptied by the atomic promotion...
    quarantine_root = _studio_quarantine(studio)
    assert len(list(quarantine_root.iterdir())) == 1
    queue_job_id = next(iter(quarantine_root.iterdir())).name
    assert not list((quarantine_root / queue_job_id / "attempt-2").glob("*.mp4"))
    # ...and the crashed attempt's quarantine was never touched.
    assert list((quarantine_root / queue_job_id / "attempt-1").glob("*.mp4"))
    # No promote-backup leftovers anywhere in the studio.
    assert not list((studio / "projects").rglob("*.promote-backup"))

    approval_id = str(result["approval_id"])
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        assert ledger.get(approval_id).state is ApprovalState.CONSUMED
        reservation = ledger._read_reservation(approval_id)
        assert reservation is not None
        assert reservation[0] == queue_job_id
        assert reservation[3] == 2
        assert reservation[2] is not None, "successor must finalize the reservation"
    finally:
        ledger.close()


def test_subprocess_death_after_completed_is_reconciled_at_startup(
    studio: Path, tmp_path: Path
) -> None:
    """A real os._exit(99) BETWEEN the durable COMPLETED commit and the
    approval finalization: startup reconciliation in the next process closes
    the reservation and the replay stays refused."""
    db = tmp_path / "finalize-queue.sqlite3"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_payload(_approval_id(studio))), encoding="utf-8")

    crash = _run_child("die-at-finalize", db, payload_file, studio)
    assert crash.returncode == 99, crash.stderr[-2000:]

    # The row is already durably COMPLETED...
    row = _queue_row(db)
    assert row["status"] == "completed"
    assert row["attempt"] == 1
    result = json.loads(str(row["result_json"]))
    approval_id = str(result["approval_id"])
    queue_job_id = str(row["id"])

    # ...but the reservation was never closed: the process died first.
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        assert ledger.get(approval_id).state is ApprovalState.CONSUMED
        reservation = ledger._read_reservation(approval_id)
        assert reservation is not None
        assert reservation[2] is None, "the crash must leave the reservation open"
    finally:
        ledger.close()

    reconciler = _run_child("reconciler", db, payload_file, studio)
    assert reconciler.returncode == 0, reconciler.stderr[-2000:]

    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        reservation = ledger._read_reservation(approval_id)
        assert reservation is not None
        assert reservation[2] is not None, "startup reconciliation must close it"
        assert reservation[3] == 1
        # Closing it blocked the replay: the consumed approval cannot be
        # re-read by any later execution of the same job.
        identity = approval_identity_for(
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
            script_sha256=compile_storyboard(SCRIPT).script_sha256,
            manifest_sha256=compile_storyboard(SCRIPT).manifest_sha256,
            render_profile_hash=mz.RenderProfile.from_mapping(PROFILE).profile_hash(),
        )
        with pytest.raises(ApprovalError, match="replay"):
            ledger.consume(approval_id, identity=identity, job_id=queue_job_id, attempt=1)
        # Idempotent: a repeated finalization of the same token is a no-op.
        ledger.finalize_consumption(approval_id, job_id=queue_job_id, attempt=1)
    finally:
        ledger.close()


def test_stale_attempt_cannot_publish_or_finalize_after_takeover(
    studio: Path, tmp_path: Path
) -> None:
    """Attempt 1 hangs AFTER its PASS verdict; attempt 2 takes the row over,
    publishes and finalizes; the released attempt 1 can publish nothing."""
    db = tmp_path / "fence-queue.sqlite3"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_payload(_approval_id(studio))), encoding="utf-8")
    log = tmp_path / "hung-child.log"

    hung = subprocess.Popen(  # noqa: S603 - fixed argv, isolated interpreter
        [
            sys.executable,
            "-I",
            "-c",
            _CHILD_SCRIPT,
            "hang-in-verifier",
            str(db),
            str(payload_file),
            str(studio),
        ],
        stdout=open(log, "wb"),
        stderr=subprocess.STDOUT,
        env=dict(os.environ),
    )
    try:
        # Wait until attempt 1 is VERIFYING with its staged bytes on disk.
        phase = studio / ".phase-verifying"
        deadline = time.monotonic() + 240
        while not phase.exists():
            assert hung.poll() is None, log.read_text(errors="replace")[-2000:]
            assert time.monotonic() < deadline, "attempt 1 never reached VERIFYING"
            time.sleep(0.2)

        # A second process takes the row over (attempt 2) and completes it.
        takeover = _run_child("takeover", db, payload_file, studio)
        assert takeover.returncode == 0, takeover.stderr[-2000:]
        row = _queue_row(db)
        assert row["status"] == "completed"
        assert row["attempt"] == 2
        result = json.loads(str(row["result_json"]))
        assert result["attempt"] == 2
        queue_job_id = str(row["id"])
        approval_id = str(result["approval_id"])

        # Release the hung attempt-1 verifier: its promotion is fenced off.
        (studio / ".release").write_text("1", encoding="utf-8")
        assert hung.wait(timeout=240) == 0, log.read_text(errors="replace")[-2000:]
    finally:
        if hung.poll() is None:  # pragma: no cover - failure-path cleanup
            hung.kill()
            hung.wait(timeout=30)

    # The stale attempt published nothing: its quarantine keeps its files,
    # the winner's quarantine was emptied by the real promotion...
    quarantine_root = _studio_quarantine(studio)
    assert list((quarantine_root / queue_job_id / "attempt-1").glob("*.mp4"))
    assert not list((quarantine_root / queue_job_id / "attempt-2").glob("*.mp4"))
    # ...the final pair is the successor's publication...
    renders = studio / "projects" / PROJECT_ID / "renders" / JOB_ID
    assert (renders / "storyboard.mp4").is_file()
    assert (renders / "storyboard.passport.json").is_file()
    # ...no transactional-promote leftovers survived anywhere...
    assert not list((studio / "projects").rglob("*.promote-backup"))
    # ...and the stale attempt can never finalize the newer attempt's reservation.
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        reservation = ledger._read_reservation(approval_id)
        assert reservation is not None
        assert reservation[0] == queue_job_id
        assert reservation[3] == 2
        assert reservation[2] is not None
        with pytest.raises(ApprovalAlreadyConsumedError):
            ledger.finalize_consumption(approval_id, job_id=queue_job_id, attempt=1)
    finally:
        ledger.close()
