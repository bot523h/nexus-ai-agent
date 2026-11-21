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
    _PromotionRefused,
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
from nexus_ai_agent.jobs.lifecycle import ExecutionClaim
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
    if MODE == "die-between-renames":
        # Crash injection: die EXACTLY between the two promotion renames —
        # the new MP4 has landed at the final path, its passport has not.
        queue._between_promote_replaces_hook = lambda: os._exit(99)
    if MODE == "crash-after-render":
        await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="sb-subprocess-crash",
            payload=PAYLOAD_JSON,
        )
        # The worker thread dies with os._exit(99) inside the render; this
        # coroutine simply keeps the process alive until that happens.
        await asyncio.sleep(3600)
    elif MODE == "die-between-renames":
        await queue.enqueue(
            job_type="storyboard_render",
            idempotency_key="sb-subprocess-between-renames",
            payload=PAYLOAD_JSON,
        )
        # The worker thread dies with os._exit(99) between the two renames;
        # this coroutine simply keeps the process alive until that happens.
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


def test_subprocess_crash_between_two_promotion_renames_recovers_cleanly(
    studio: Path, tmp_path: Path
) -> None:
    """A real os._exit(99) BETWEEN the two promotion renames: the new MP4 is
    at the final path without its passport, the row is never COMPLETED and
    the download gate refuses it; a brand-new process repairs the destination
    and completes attempt 2 with a valid, consistent, downloadable pair."""
    from nexus_ai_agent.api.studio import _require_completed_and_verified
    from nexus_ai_agent.creative.artifact_download import (
        DownloadRefused,
        artifact_rel_path_for_job,
        authorize_artifact_download,
    )

    db = tmp_path / "between-renames-queue.sqlite3"
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(_payload(_approval_id(studio))), encoding="utf-8")

    crash = _run_child("die-between-renames", db, payload_file, studio)
    assert crash.returncode == 99, crash.stderr[-2000:]

    renders = studio / "projects" / PROJECT_ID / "renders" / JOB_ID
    row = _queue_row(db)
    queue_job_id = str(row["id"])

    # The crashed promotion left EXACTLY the inconsistent destination: the
    # new MP4 landed at the final path, its passport never did...
    assert (renders / "storyboard.mp4").is_file()
    assert not (renders / "storyboard.passport.json").exists()
    # ...no previous generation was ever parked (this was the first render)...
    assert not list((studio / "projects").rglob("*.promote-backup"))
    # ...and the crashed attempt's quarantine still holds its staged passport.
    assert list((_studio_quarantine(studio) / queue_job_id / "attempt-1").glob("*.passport.json"))

    # The row was never COMPLETED...
    assert row["status"] != "completed"
    # ...so the download gate refuses it at BOTH layers: the artifact gate
    # (a row with no durable result publishes no downloadable path)...
    gate_row = {
        "id": queue_job_id,
        "status": row["status"],
        "input_data": {"project_id": PROJECT_ID},
        "result": json.loads(str(row["result_json"] or "null")),
    }
    with pytest.raises(DownloadRefused) as info:
        artifact_rel_path_for_job(gate_row, PROJECT_ID)
        _require_completed_and_verified(gate_row)
    assert (info.value.status_code, info.value.reason) == (409, "artifact_not_ready")
    # ...and the passport gate (an orphaned MP4 without its sealed passport).
    orphan_rel = f"projects/{PROJECT_ID}/renders/{JOB_ID}/storyboard.mp4"
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(studio, orphan_rel, SIGNING_KEY)
    assert (info.value.status_code, info.value.reason) == (409, "passport_missing")

    successor = _run_child("successor", db, payload_file, studio)
    assert successor.returncode == 0, successor.stderr[-2000:]

    row = _queue_row(db)
    assert row["status"] == "completed"
    assert row["attempt"] == 2
    result = json.loads(str(row["result_json"]))
    assert result["status"] == "rendered"
    assert result["attempt"] == 2
    assert result["approval_id"]

    # The successor's own promotion repaired the destination: a consistent
    # final pair, no leftover backups anywhere in the studio...
    assert (renders / "storyboard.mp4").is_file()
    assert (renders / "storyboard.passport.json").is_file()
    assert not list((studio / "projects").rglob("*.promote-backup"))
    # ...the successor's quarantine was emptied by the atomic promotion...
    assert not list((_studio_quarantine(studio) / queue_job_id / "attempt-2").glob("*.mp4"))
    # ...and the published pair is actually downloadable end-to-end: the
    # completed row resolves to a rel path that authorizes cleanly.
    completed_row = {
        "id": queue_job_id,
        "status": row["status"],
        "input_data": {"project_id": PROJECT_ID},
        "result": result,
    }
    rel = artifact_rel_path_for_job(completed_row, PROJECT_ID)
    _require_completed_and_verified(completed_row)
    authorized = authorize_artifact_download(studio, rel, SIGNING_KEY)
    try:
        assert authorized.size_bytes == (renders / "storyboard.mp4").stat().st_size
    finally:
        authorized.close()

    # The approval reservation was closed by THIS attempt's finalization.
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


# --------------------------------------------------------------------------- #
# CP4.1: generation-aware promotion recovery, fail-closed call site and
# ownership-loss rollback.
# --------------------------------------------------------------------------- #

PROFILE_V2 = {"width": 320, "height": 180, "fps": 24, "sample_rate": 16000}
SECOND_JOB_KEY = "sb-subprocess-between-renames"


def _issue_for(workspace: Path, profile: dict[str, object]) -> str:
    """Issue one more approval for JOB_ID under ``profile`` (a new generation)."""
    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        return issue_render_approval(
            ledger,
            project_id=PROJECT_ID,
            job_id=JOB_ID,
            principal_id=PRINCIPAL_ID,
            source_sha256=src,
            script_sha256=compiled.script_sha256,
            manifest_sha256=compiled.manifest_sha256,
            render_profile_hash=mz.RenderProfile.from_mapping(profile).profile_hash(),
        )
    finally:
        ledger.close()


def _payload_for(approval_id: str, profile: dict[str, object]) -> dict[str, object]:
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
            **profile,
        }
    )


async def _complete_and_release(db: Path, payload: dict[str, object]) -> JobStatus:
    """Run one storyboard job to a terminal status, then release the queue lock."""
    queue = InProcessJobQueue(
        db, handlers=default_job_handlers(), artifact_verifiers=default_artifact_verifiers()
    )
    await queue.acquire_ownership()
    try:
        job_id = await queue.enqueue(
            job_type="storyboard_render", idempotency_key="storyboard-queue-1", payload=payload
        )
        deadline = time.monotonic() + 240
        while True:
            status = await queue.get_status(job_id)
            if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL):
                return status
            if time.monotonic() > deadline:
                raise AssertionError(f"job still {status}")
            await asyncio.sleep(0.2)
    finally:
        await queue.shutdown()


def _renders(studio: Path) -> Path:
    return studio / "projects" / PROJECT_ID / "renders" / JOB_ID


def _row_by_key(db: Path, key: str) -> dict[str, object]:
    import sqlite3

    connection = sqlite3.connect(db)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, status, attempt, result_json, error FROM nexus_job_queue "
            "WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)


def _direct_queue(db: Path) -> InProcessJobQueue:
    return InProcessJobQueue(db, handlers={}, artifact_verifiers={})


def _staged_render(
    studio: Path, payload: dict[str, object], queue_job_id: str, attempt: int
) -> dict[str, object]:
    """Render one quarantined attempt (no queue, the same handler contract)."""
    from nexus_ai_agent.creative.storyboard_render import RenderContext, render_storyboard_sync

    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        ctx = RenderContext(workspace_root=studio, approval_ledger=ledger, signing_key=SIGNING_KEY)
        return dict(
            render_storyboard_sync(payload, ctx, queue_job_id=queue_job_id, attempt=attempt)
        )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "forged",
    [
        None,
        {
            "final_output_path": "/nonexistent/x.mp4",
            "final_passport_path": "/nonexistent/x.passport.json",
        },
    ],
)
def test_process_job_fails_terminal_when_promotion_publishes_no_pair(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forged: object
) -> None:
    """The call site never completes a storyboard job whose promotion did not publish."""

    def _no_promotion(self: InProcessJobQueue, claim: object, result: object, job_type: str):
        return forged

    monkeypatch.setattr(InProcessJobQueue, "_promote_staged_artifacts", _no_promotion)
    db = tmp_path / "callsite-queue.sqlite3"
    approval_id = _approval_id(studio)
    status, _ = asyncio.run(_drive_through_queue(db, _payload(approval_id)))

    assert status is JobStatus.FAILED_TERMINAL
    row = _queue_row(db)
    assert row["status"] != "completed"
    assert "promotion_failed:promotion_not_performed" in str(row["error"])
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(studio), now=lambda: "2026-11-19T06:00:00Z"
    )
    try:
        reservation = ledger._read_reservation(approval_id)
        assert reservation is None or reservation[2] is None, "approval must stay un-finalized"
    finally:
        ledger.close()


def test_subprocess_crash_during_a_new_generation_restores_the_prior_generation(
    studio: Path, tmp_path: Path
) -> None:
    """A real os._exit(99) while generation 2 replaces generation 1 between the renames.

    Startup recovery must put the verified generation 1 back BEFORE any successor
    task runs, drop the parked backups, and let attempt 2 publish generation 2.
    """
    db = tmp_path / "prior-generation-queue.sqlite3"
    assert asyncio.run(_complete_and_release(db, _payload(_approval_id(studio)))) is (
        JobStatus.COMPLETED
    )
    renders = _renders(studio)
    gen1_mp4 = (renders / "storyboard.mp4").read_bytes()
    gen1_pp = (renders / "storyboard.passport.json").read_bytes()

    payload_file = tmp_path / "payload-v2.json"
    payload_file.write_text(
        json.dumps(_payload_for(_issue_for(studio, PROFILE_V2), PROFILE_V2)), encoding="utf-8"
    )
    crash = _run_child("die-between-renames", db, payload_file, studio)
    assert crash.returncode == 99, crash.stderr[-2000:]

    # The crash state: the new MP4 landed, the new passport did not, and the
    # previous generation is parked beside them.
    assert (renders / "storyboard.mp4").read_bytes() != gen1_mp4
    assert not (renders / "storyboard.passport.json").exists()
    assert (renders / "storyboard.mp4.promote-backup").read_bytes() == gen1_mp4
    assert (renders / "storyboard.passport.json.promote-backup").read_bytes() == gen1_pp

    async def recover() -> JobStatus:
        queue = InProcessJobQueue(
            db,
            handlers=default_job_handlers(),
            artifact_verifiers=default_artifact_verifiers(),
        )
        await queue.acquire_ownership()
        await queue.resume_pending()
        # No await since resume_pending: the destination is settled BEFORE any
        # recovered execution can start, so generation 1 is what sits there now.
        assert (renders / "storyboard.mp4").read_bytes() == gen1_mp4
        assert (renders / "storyboard.passport.json").read_bytes() == gen1_pp
        assert not list(renders.glob("*.promote-backup"))
        job_id = str(_row_by_key(db, SECOND_JOB_KEY)["id"])
        deadline = time.monotonic() + 240
        try:
            while True:
                status = await queue.get_status(job_id)
                if status in (JobStatus.COMPLETED, JobStatus.FAILED_TERMINAL):
                    return status
                if time.monotonic() > deadline:
                    raise AssertionError(f"successor still {status}")
                await asyncio.sleep(0.2)
        finally:
            await queue.shutdown()

    assert asyncio.run(recover()) is JobStatus.COMPLETED
    row = _row_by_key(db, SECOND_JOB_KEY)
    assert row["attempt"] == 2
    result = json.loads(str(row["result_json"]))
    assert result["render_profile_hash"] == mz.RenderProfile.from_mapping(PROFILE_V2).profile_hash()
    assert (renders / "storyboard.mp4").read_bytes() != gen1_mp4
    assert not list(renders.glob("*.promote-backup"))


@pytest.mark.parametrize("prior_generation", [False, True])
def test_promote_rolls_back_when_ownership_is_lost_after_the_replace(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_generation: bool
) -> None:
    """A superseded attempt's verified bytes never stay published after the replace."""
    db = tmp_path / "ownership-queue.sqlite3"
    queue = _direct_queue(db)
    renders = _renders(studio)
    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", lambda *_a: True)
    gen1_mp4 = gen1_pp = None
    if prior_generation:
        first = _staged_render(studio, _payload(_approval_id(studio)), "qjob-own", 1)
        assert (
            queue._promote_staged_artifacts(
                ExecutionClaim(job_id="qjob-own", attempt=1), first, "storyboard_render"
            )
            is not None
        )
        gen1_mp4 = (renders / "storyboard.mp4").read_bytes()
        gen1_pp = (renders / "storyboard.passport.json").read_bytes()

    second_payload = _payload_for(_issue_for(studio, PROFILE_V2), PROFILE_V2)
    second = _staged_render(studio, second_payload, "qjob-own", 2)
    # Ownership holds for the pre-rename checks and is lost on the post-replace check.
    calls = {"n": 0}

    def _lost_after_replace(self: InProcessJobQueue, claim: object, expected: object) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", _lost_after_replace)
    promoted = queue._promote_staged_artifacts(
        ExecutionClaim(job_id="qjob-own", attempt=2), second, "storyboard_render"
    )

    assert promoted is None
    assert calls["n"] >= 2, "the post-replace ownership check must have run"
    assert Path(str(second["staged_output_path"])).is_file(), "the bytes return to quarantine"
    assert not list(renders.glob("*.promote-backup"))
    if prior_generation:
        assert (renders / "storyboard.mp4").read_bytes() == gen1_mp4
        assert (renders / "storyboard.passport.json").read_bytes() == gen1_pp
    else:
        assert not (renders / "storyboard.mp4").exists()
        assert not (renders / "storyboard.passport.json").exists()


def test_reconcile_keeps_a_verified_new_pair_and_drops_parked_backups(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", lambda *_a: True)
    queue = _direct_queue(tmp_path / "keep-queue.sqlite3")
    first = _staged_render(studio, _payload(_approval_id(studio)), "qjob-keep", 1)
    assert (
        queue._promote_staged_artifacts(
            ExecutionClaim(job_id="qjob-keep", attempt=1), first, "storyboard_render"
        )
        is not None
    )
    renders = _renders(studio)
    final_mp4, final_pp = renders / "storyboard.mp4", renders / "storyboard.passport.json"
    new_mp4 = final_mp4.read_bytes()
    for final in (final_mp4, final_pp):
        final.with_name(f"{final.name}.promote-backup").write_bytes(b"parked")

    queue._reconcile_interrupted_promotion_backups(final_mp4, final_pp)

    assert final_mp4.read_bytes() == new_mp4
    assert not list(renders.glob("*.promote-backup"))


def test_reconcile_restores_a_parked_generation_over_an_unverified_new_pair(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", lambda *_a: True)
    queue = _direct_queue(tmp_path / "restore-queue.sqlite3")
    first = _staged_render(studio, _payload(_approval_id(studio)), "qjob-rest", 1)
    assert (
        queue._promote_staged_artifacts(
            ExecutionClaim(job_id="qjob-rest", attempt=1), first, "storyboard_render"
        )
        is not None
    )
    renders = _renders(studio)
    final_mp4, final_pp = renders / "storyboard.mp4", renders / "storyboard.passport.json"
    gen1_mp4, gen1_pp = final_mp4.read_bytes(), final_pp.read_bytes()
    # Stage a crash state: generation 1 parked, a complete but unverified generation 2 in place.
    final_mp4.with_name("storyboard.mp4.promote-backup").write_bytes(gen1_mp4)
    final_pp.with_name("storyboard.passport.json.promote-backup").write_bytes(gen1_pp)
    final_pp.write_text("{}", encoding="utf-8")

    queue._reconcile_interrupted_promotion_backups(final_mp4, final_pp)

    assert final_mp4.read_bytes() == gen1_mp4
    assert final_pp.read_bytes() == gen1_pp
    assert not list(renders.glob("*.promote-backup"))


def test_reconcile_removes_a_pair_it_cannot_restore_whole(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked MP4 without its parked passport must never leave a mixed generation."""
    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", lambda *_a: True)
    queue = _direct_queue(tmp_path / "partial-queue.sqlite3")
    renders = _renders(studio)
    renders.mkdir(parents=True, exist_ok=True)
    final_mp4, final_pp = renders / "storyboard.mp4", renders / "storyboard.passport.json"
    final_mp4.write_bytes(b"new-mp4")
    final_mp4.with_name("storyboard.mp4.promote-backup").write_bytes(b"old-mp4")

    queue._reconcile_interrupted_promotion_backups(final_mp4, final_pp)

    assert not final_mp4.exists()
    assert not final_pp.exists()
    assert not list(renders.glob("*.promote-backup"))


def test_concurrent_stale_rollback_cannot_clobber_successor_promotion(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded attempt that loses ownership after its rename never clobbers a successor.

    Two queue instances share one database and one destination. The stale
    attempt pauses INSIDE its promotion (holding the destination lock, after
    its first rename). The successor must block on the cross-instance file
    lock; once the stale attempt rolls back, the successor promotes its own
    generation, which must be the final pair on disk and verify as PASS.
    """
    monkeypatch.setattr(InProcessJobQueue, "_owns_execution", lambda *_a: True)
    db = tmp_path / "race-queue.sqlite3"
    seed = _direct_queue(db)
    prior = _staged_render(studio, _payload(_approval_id(studio)), "qjob-race", 1)
    assert (
        seed._promote_staged_artifacts(
            ExecutionClaim(job_id="qjob-race", attempt=1), prior, "storyboard_render"
        )
        is not None
    )
    stale = _staged_render(
        studio, _payload_for(_issue_for(studio, PROFILE_V2), PROFILE_V2), "qjob-race", 2
    )
    successor = _staged_render(
        studio, _payload_for(_issue_for(studio, PROFILE_V2), PROFILE_V2), "qjob-race", 3
    )
    successor_mp4 = Path(str(successor["staged_output_path"])).read_bytes()
    successor_pp = Path(str(successor["staged_passport_path"])).read_bytes()

    stale_queue, successor_queue = _direct_queue(db), _direct_queue(db)
    inside, release, lost = threading.Event(), threading.Event(), threading.Event()

    def _pause_between_renames() -> None:
        inside.set()
        release.wait(timeout=30)

    stale_queue._between_promote_replaces_hook = _pause_between_renames
    stale_queue._owns_execution = lambda _claim, _expected: not lost.is_set()  # type: ignore[method-assign]
    successor_queue._owns_execution = lambda _claim, _expected: True  # type: ignore[method-assign]
    outcomes: dict[str, object] = {}

    def _run(name: str, queue: InProcessJobQueue, staged: dict[str, object], attempt: int) -> None:
        try:
            outcomes[name] = queue._promote_staged_artifacts(
                ExecutionClaim(job_id="qjob-race", attempt=attempt), staged, "storyboard_render"
            )
        except BaseException as exc:  # recorded and asserted below
            outcomes[f"{name}_exc"] = exc

    stale_thread = threading.Thread(target=_run, args=("stale", stale_queue, stale, 2))
    stale_thread.start()
    assert inside.wait(timeout=30), "the stale attempt must reach its promotion window"
    successor_thread = threading.Thread(
        target=_run, args=("successor", successor_queue, successor, 3)
    )
    successor_thread.start()
    time.sleep(0.5)
    assert successor_thread.is_alive(), "the successor must wait on the destination lock"
    lost.set()
    release.set()
    stale_thread.join(timeout=60)
    successor_thread.join(timeout=60)

    assert not stale_thread.is_alive() and not successor_thread.is_alive()
    assert "stale_exc" not in outcomes and "successor_exc" not in outcomes, outcomes
    assert outcomes["stale"] is None, "the superseded attempt must not publish"
    assert outcomes["successor"] is not None
    renders = _renders(studio)
    final_mp4, final_pp = renders / "storyboard.mp4", renders / "storyboard.passport.json"
    assert final_mp4.read_bytes() == successor_mp4
    assert final_pp.read_bytes() == successor_pp
    assert storyboard_jobs.verify_storyboard_final_pair(final_mp4, final_pp) is None
    assert not list(renders.glob("*.promote-backup"))
    assert Path(str(stale["staged_output_path"])).is_file(), "the stale bytes return to quarantine"


def test_reconcile_interrupted_promotion_backups_fails_closed_on_restore_oserror(
    studio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while restoring a parked generation refuses; it is never swallowed."""
    queue = _direct_queue(tmp_path / "oserror-queue.sqlite3")
    renders = _renders(studio)
    renders.mkdir(parents=True, exist_ok=True)
    final_mp4, final_pp = renders / "storyboard.mp4", renders / "storyboard.passport.json"
    final_mp4.write_bytes(b"unverified-new-mp4")
    final_pp.write_text("{}", encoding="utf-8")
    parked = final_mp4.with_name("storyboard.mp4.promote-backup")
    parked.write_bytes(b"parked-previous-mp4")
    real_replace = os.replace

    def _restore_fails(src: object, dst: object, *args: object, **kwargs: object) -> None:
        if str(src).endswith(".promote-backup"):
            raise OSError(5, "injected restore failure")
        return real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", _restore_fails)
    with pytest.raises(_PromotionRefused) as refused:
        queue._reconcile_interrupted_promotion_backups(final_mp4, final_pp)

    assert refused.value.reason == "reconcile_interrupted_backup_failed"
    assert parked.read_bytes() == b"parked-previous-mp4", "the parked generation is kept"
