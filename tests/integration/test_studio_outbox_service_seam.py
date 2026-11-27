"""The execution outbox, driven by the SERVICE through ``execute()``.

Earlier revisions of this suite called the private seam helpers
(``_stage_outbox_intent``, ``_record_outbox_enqueue_failure``) with hand-built
contexts.  That proved the helpers could be called; it did not prove
``execute()`` reached them, and it encoded the very ordering bug the seam was
supposed to close -- staging *after* the approval had already been consumed.

These tests therefore drive the real production path end to end: a real clip,
the real ``InProcessJobQueue`` with the real job handlers, the real approval
ledger, and the real ``execute()``.  What they pin down:

* the durable intent is staged BEFORE the approval is consumed, so a seam
  failure refuses a turn without spending anything (Invariant 17, fail-closed);
* a crash after the ledger commit and before the queue commit leaves a
  resumable row, not a terminal one;
* a retry -- or a fresh service over the same files -- resumes that row under
  the SAME reserved job id, consumes nothing twice, and enqueues exactly one
  job (Invariant 19, single authority).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.sql_application_directory import (
    SQLApplicationDirectory,
    channel_binding_id,
)
from nexus_ai_agent.application.principal import ChannelBinding
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.connect.key_provider import StaticKeyRing
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin
from nexus_ai_agent.nagar.ibpc.approval import ApprovalState
from nexus_ai_agent.nagar.ibpc.authority import DirectoryAuthority
from nexus_ai_agent.nagar.ibpc.execution_outbox import (
    DurableExecutionOutboxStore,
    ExecutionOutboxState,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps, PipelineResult
from nexus_ai_agent.storage.migrations import ensure_startup_schema
from nexus_ai_agent.worker import default_job_handlers

PROJECT_ID = "ibpc-outbox"
REVISION = 3
SIZE = "1280x720"
RATE = 30
SECONDS = 2.0


def _clip(path: Path) -> None:
    """A real, decodable clip -- the execution path is never mocked."""
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # pragma: no cover - environment only
        pytest.skip(f"FFmpeg unavailable: {exc}")
    proc = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={SECONDS}:size={SIZE}:rate={RATE}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={SECONDS}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:  # pragma: no cover - environment only
        pytest.skip(f"could not synthesize clip: {proc.stderr.decode('utf-8', 'replace')[-200:]}")


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A runtime wired the way the composition root wires it, outbox included."""
    creative_root = tmp_path / "creative_tmp"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    ensure_startup_schema()

    directory = SQLApplicationDirectory()
    binding = ChannelBinding("cli", "outbox-owner")
    principal = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT_ID, principal)

    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)

    def _build(root: Path) -> IBPCService:
        service = IBPCService(
            IBPCServiceDeps(
                queue=queue,
                registry=build_runtime_registry(),
                workspace_root=root,
                revision_provider=lambda: REVISION,
                authority=DirectoryAuthority(directory),
                channel_binding_ref=channel_binding_id(binding),
                # The MAC key ring is structurally a KeyProvider; the port is
                # declared against a narrower protocol, hence the cast.
                approval_key_provider=cast(
                    "Any", StaticKeyRing({"test-v1": b"t" * 32}, active_key_id="test-v1")
                ),
            )
        )
        service.enable_execution_outbox(DurableExecutionOutboxStore(root / "outbox.sqlite3"))
        return service

    service = _build(creative_root)
    yield service, queue, creative_root, str(principal), _build
    service.close()
    settings_module.get_settings.cache_clear()


def _proposal(
    service: IBPCService, source: Path, principal: str, turn_id: str = "turn-1"
) -> tuple[str, PipelineResult]:
    session_id = service.open_session(
        tenant_id="ten-1",
        project_id=PROJECT_ID,
        principal_id=principal,
        user_id=7,
        chat_id=77,
        current_revision=REVISION,
    )
    result = service.submit_text_intent(
        session_id,
        turn_id,
        sequence_number=1,
        text="trim from 0.5 to 1.5 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult)
    service.approve(session_id, turn_id, result.approval_id, principal_id=principal)
    assert service.approvals.get(result.approval_id).state is ApprovalState.APPROVED
    return session_id, result


def _only_row(service: IBPCService) -> Any:
    """The single outbox row this test created (there is exactly one)."""
    store = service._outbox_store()
    assert store is not None
    rows = store.list_in_flight(limit=10)
    return rows[0] if rows else None


@pytest.mark.asyncio
async def test_a_seam_that_cannot_stage_refuses_without_spending_the_approval(
    rig: Any, tmp_path: Path
) -> None:
    """Invariant 17 (fail-closed): no durable record, no spend.

    This is the finding.  If staging fails, ``execute()`` must raise nothing
    into the caller but must REFUSE before ``approvals.consume`` runs -- the
    approval stays APPROVED and requeueable, and the queue stays empty.  The old
    code swallowed the failure, consumed the approval anyway and enqueued a job
    with no outbox row behind it.
    """
    service, queue, _root, principal, _build = rig
    source = tmp_path / "seam-source.mp4"
    _clip(source)
    session_id, result = _proposal(service, source, principal)

    store = service._outbox_store()
    assert store is not None

    def _stage_boom(**_kwargs: Any) -> Any:
        raise OSError("outbox volume is read-only")

    store.stage_intent = _stage_boom

    outcome = await service.execute(
        session_id, "turn-1", result.approval_id, principal_id=principal
    )

    # 1. the caller gets a typed refusal naming the outbox, not a crash
    assert outcome.status == "refused"
    assert "outbox_unavailable" in outcome.detail
    assert "read-only" in outcome.detail, "the underlying cause must survive the seam"

    # 2. the failure is readable, not just raised
    assert service.last_outbox_error is not None
    assert "stage_intent_failed" in service.last_outbox_error
    assert "read-only" in service.last_outbox_error

    # 3. nothing was spent: the approval is still usable
    assert service.approvals.get(result.approval_id).state is ApprovalState.APPROVED

    # 4. nothing was enqueued
    assert queue.job_ids() == []

    # 5. there is no dangling durable row to reconcile
    assert _only_row(service) is None


@pytest.mark.asyncio
async def test_an_enqueue_crash_parks_the_spent_approval_for_recovery(
    rig: Any, tmp_path: Path
) -> None:
    """A queue hiccup must not burn a spent approval, nor hide the crash."""
    service, queue, _root, principal, _build = rig
    source = tmp_path / "enqueue-source.mp4"
    _clip(source)
    session_id, result = _proposal(service, source, principal)

    calls = {"n": 0}
    real_enqueue = queue.enqueue

    async def _flaky_enqueue(
        *, job_type: str, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("queue write died mid-commit")
        return await real_enqueue(
            job_type=job_type, idempotency_key=idempotency_key, payload=payload
        )

    queue.enqueue = _flaky_enqueue

    first = await service.execute(session_id, "turn-1", result.approval_id, principal_id=principal)
    assert first.status == "refused"
    assert "enqueue_failed" in first.detail
    assert queue.job_ids() == []

    # The row is durably parked at APPROVAL_CONSUMED -- the approval is spent,
    # so it must NOT be FAILED_TERMINAL (that would demand a fresh human
    # approval for a one-second queue hiccup).
    parked = _only_row(service)

    assert parked is not None
    assert parked.state == ExecutionOutboxState.APPROVAL_CONSUMED.value
    assert parked.reserved_job_id, "the parked row must carry its reservation"
    assert "enqueue_retryable" in parked.last_error
    assert "mid-commit" in parked.last_error
    states = {event["to_state"] for event in service._outbox_store().events_for(parked.outbox_id)}
    assert ExecutionOutboxState.FAILED_TERMINAL.value not in states

    # A retry through the SAME entry point recovers: it resumes under the same
    # reservation, consumes nothing twice, and enqueues exactly one job.
    second = await service.execute(session_id, "turn-1", result.approval_id, principal_id=principal)
    assert second.status in ("executed", "executed_unverified"), second.detail
    assert second.job_id is not None
    assert queue.job_ids() == [second.job_id]

    final = service._outbox_store().get(outbox_id=parked.outbox_id)
    assert final is not None
    assert final.state == ExecutionOutboxState.COMPLETED.value


@pytest.mark.asyncio
async def test_a_fresh_service_resumes_the_parked_intent_exactly_once(
    rig: Any, tmp_path: Path
) -> None:
    """After a restart, reconciliation finishes the crashed intent once."""
    service, queue, creative_root, principal, build = rig
    source = tmp_path / "restart-source.mp4"
    _clip(source)
    session_id, result = _proposal(service, source, principal)

    real_enqueue = queue.enqueue

    async def _boom(*, job_type: str, idempotency_key: str, payload: dict[str, Any]) -> str:
        raise RuntimeError("process died before the queue write")

    queue.enqueue = _boom
    crashed = await service.execute(
        session_id, "turn-1", result.approval_id, principal_id=principal
    )
    assert crashed.status == "refused"
    parked = _only_row(service)
    assert parked is not None
    reserved = parked.reserved_job_id
    service.close()
    # The queue itself survived the restart: only the process died.
    queue.enqueue = real_enqueue

    # --- restart: a brand new service over the same SQLite files.
    second = build(creative_root)
    try:
        assert second.execution_outbox is not None
        outcomes = await second.reconcile_execution_outbox_async()
        assert outcomes, "the parked intent must be reconciled, not ignored"
        outcome = outcomes[0]

        assert outcome["idempotency_key"] == parked.idempotency_key
        job_ids = queue.job_ids()
        assert len(job_ids) == 1, "exactly one job, never two"
        assert outcome["job_id"] == job_ids[0], "the row must point at the enqueued job"
        # The ledger reservation and the queue's own job id are deliberately
        # different things: the reservation is the idempotency evidence the
        # ledger was asked to record at consume time, and exactly-once enqueue
        # comes from the queue's dedupe on that key, not from id equality.
        resumed = second._outbox_store().get(outbox_id=parked.outbox_id)
        assert resumed is not None
        assert resumed.reserved_job_id == reserved, "the reservation must survive the restart"
        assert resumed.state in (
            ExecutionOutboxState.JOB_ENQUEUED.value,
            ExecutionOutboxState.COMPLETED.value,
            ExecutionOutboxState.AWAITING_WORKER.value,
        ), resumed.state
    finally:
        second.close()


def test_an_unwired_service_keeps_the_legacy_path(tmp_path: Path) -> None:
    """ "No outbox configured" must mean "no seam", never "a broken seam"."""
    root = tmp_path / "plain"
    root.mkdir()
    directory = SQLApplicationDirectory()
    binding = ChannelBinding("cli", "plain-owner")
    principal = directory.resolve_or_create(binding)
    directory.ensure_project(PROJECT_ID, principal)
    service = IBPCService(
        IBPCServiceDeps(
            queue=InProcessJobQueue(root / "jobs.sqlite3"),
            registry=build_runtime_registry(),
            workspace_root=root,
            revision_provider=lambda: 0,
            authority=DirectoryAuthority(directory),
            channel_binding_ref=channel_binding_id(binding),
        )
    )
    try:
        assert service.execution_outbox is None
        assert service.last_outbox_error is None
        assert service.reconcile_execution_outbox() == ()
    finally:
        service.close()


def test_the_coroutine_bridge_refuses_to_block_a_running_event_loop() -> None:
    """The async entry points ``await`` the port; the bridge is for sync callers.

    Spawning a thread and joining it on the loop thread would block the loop for
    the whole duration of the bridged I/O -- the exact latency bug the bridge was
    added to avoid.  A running loop on the calling thread is therefore a
    programming error and must be loud.
    """
    import asyncio

    from nexus_ai_agent.nagar.ibpc.service import _run_coroutine_blocking

    async def value() -> str:
        await asyncio.sleep(0)
        return "ok"

    # 1. no loop on this thread: the documented case, driven to completion
    assert _run_coroutine_blocking(value()) == "ok"

    # 2. inside a loop: refused, with an actionable message
    async def driver() -> str:
        return _run_coroutine_blocking(value())

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(driver())
    assert "bridge_on_event_loop" in str(raised.value)
    assert "reconcile_execution_outbox_async" in str(raised.value)


def test_the_plan_projection_excludes_per_attempt_noise() -> None:
    """The stored plan must not carry paths or timestamps.

    The outbox compares this canonical JSON on every retry to detect a
    conflicting re-stage; a workspace path or a timestamp would make every
    retry look like a different intent and turn recovery into a conflict.
    """
    from nexus_ai_agent.nagar.ibpc.service import _outbox_plan_payload

    payload = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "input_sha256": "f" * 64,
        "workspace_dir": "/tmp/should-not-be-stored",
        "input_path": "/tmp/also-not",
        "timestamp": "2026-11-27T00:00:00Z",
    }
    projection = _outbox_plan_payload(payload)
    assert "workspace_dir" not in projection
    assert "input_path" not in projection
    assert "timestamp" not in projection
    assert projection["command"] == "edit"
