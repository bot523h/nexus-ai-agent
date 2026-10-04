"""Phase 6 (§13): Crash / Restart / Recovery Matrix (CRASH_1 .. CRASH_8).

Simulates and verifies deterministic recovery across all 8 lifecycle crash points:
- CRASH_1: crash after intent/plan validation, before transaction commit
- CRASH_2: crash mid-plan step N of M
- CRASH_3: crash after transaction commit, before job enqueue
- CRASH_4: crash after job claim/lease acquisition, before artifact write
- CRASH_5: crash after partial/temp artifact write, before final atomic rename/registration
- CRASH_6: crash after artifact registration, before receipt finalization
- CRASH_7: stale worker wakes after lease expiry while new worker holds monotonic fencing token
- CRASH_8: process restart followed by idempotent command replay and targeted undo
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.creative.studio import (
    ArtifactPassport,
    CommandBus,
    DurableStudioStore,
    ExecutionProof,
    IndependentMediaVerificationError,
    PlanTransaction,
    ProvenanceCausalChain,
    ReferenceResolutionError,
    StaleFencingTokenError,
    verify_media_artifact_independently,
)
from nexus_ai_agent.creative.studio.testing import default_authorizer, make_command, make_project


def test_crash_1_after_plan_validation_before_transaction_commit(tmp_path: Path) -> None:
    """CRASH_1: crash after intent/plan validation, before transaction commit."""
    db_path = tmp_path / "crash1.db"
    store1 = DurableStudioStore(db_path)
    project = make_project("proj_crash_1")
    genesis_hash = project.state_hash
    bus1 = CommandBus(state=project, store=store1, authorizer=default_authorizer())
    assert bus1.state_revision == 0

    # Validate plan envelope without committing
    plan = PlanTransaction(
        plan_id="plan_c1",
        commands=(make_command("timeline.mark", "c1", input={"at": "اینجا", "label": "M1"}),),
    )
    assert plan.plan_id == "plan_c1"
    # Simulate process crash before dispatch_plan commits
    store1.close()

    # Recovery
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_1")
    assert recovery["state_revision"] == 0
    assert recovery["state_hash"] == genesis_hash
    assert store2.load_history("proj_crash_1") == []
    store2.close()


def test_crash_2_mid_plan_step_n_of_m_leaves_zero_partial_commits(tmp_path: Path) -> None:
    """CRASH_2: crash/abort mid-plan step N of M leaves canonical state unchanged."""
    db_path = tmp_path / "crash2.db"
    store1 = DurableStudioStore(db_path)
    project = make_project("proj_crash_2")
    bus1 = CommandBus(state=project, store=store1, authorizer=default_authorizer())

    # Baseline commit at revision 1
    bus1.dispatch(make_command("timeline.mark", "c_base", input={"at": "اینجا", "label": "Base"}))
    rev1_hash = bus1.state_hash

    # Multi-step plan where step 1 succeeds speculatively and step 2 fails mid-plan
    step1 = make_command("timeline.mark", "c_s1", input={"at": "اینجا", "label": "Step1"})
    step2 = make_command("timeline.mark", "c_s2", input={"at": "missing_marker_ref", "label": "S2"})
    plan = PlanTransaction(plan_id="plan_c2", commands=(step1, step2))

    with pytest.raises(ReferenceResolutionError):
        bus1.dispatch_plan(plan)
    store1.close()

    # Recovery after restart
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_2")
    assert recovery["state_revision"] == 1
    assert recovery["state_hash"] == rev1_hash
    loaded_proj, loaded_hist = store2.load_project("proj_crash_2")  # type: ignore[misc]
    assert len(loaded_proj.timeline.markers) == 1
    assert len(loaded_hist) == 1
    aborts = store2.list_aborted_transactions("proj_crash_2")
    assert len(aborts) == 1 and aborts[0]["plan_id"] == "plan_c2"
    store2.close()


def test_crash_3_after_transaction_commit_before_job_enqueue(tmp_path: Path) -> None:
    """CRASH_3: crash after transaction commit, before background job enqueue."""
    db_path = tmp_path / "crash3.db"
    store1 = DurableStudioStore(db_path)
    bus1 = CommandBus(
        state=make_project("proj_crash_3"), store=store1, authorizer=default_authorizer()
    )
    res = bus1.dispatch(
        make_command(
            "timeline.mark",
            "c_commit_before_job",
            input={"at": "اینجا", "label": "Committed"},
            idempotency_key="idem_c3",
        )
    )
    committed_hash = bus1.state_hash
    # Crash before job enqueue
    store1.close()

    # Recovery: replay returns exact committed transaction without re-mutating state
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_3")
    assert recovery["state_hash"] == committed_hash
    proj2, hist2 = store2.load_project("proj_crash_3")  # type: ignore[misc]
    bus2 = CommandBus(state=proj2, history=hist2, store=store2, authorizer=default_authorizer())
    replay = bus2.dispatch(
        make_command(
            "timeline.mark",
            "c_retry_after_crash",
            input={"at": "اینجا", "label": "Committed"},
            idempotency_key="idem_c3",
        )
    )
    assert replay.transaction_id == res.transaction_id
    assert bus2.state_revision == 1
    store2.close()


def test_crash_4_after_lease_acquisition_before_artifact_write(tmp_path: Path) -> None:
    """CRASH_4: crash after job claim/lease acquisition, before artifact write."""
    db_path = tmp_path / "crash4.db"
    store1 = DurableStudioStore(db_path)
    store1.save_project_state(make_project("proj_crash_4"))
    lease1 = store1.acquire_lease(
        "job_c4", "attempt_1", "worker_crashed", ttl_seconds=10.0, now=100.0
    )
    assert lease1.fencing_token == 1
    store1.close()

    # Restart after lease expiry (now=200.0)
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_4", now=200.0)
    assert recovery["expired_leases"] == 1

    # New worker acquires monotonic token 2 and completes cleanly
    lease2 = store2.acquire_lease(
        "job_c4", "attempt_2", "worker_healthy", ttl_seconds=30.0, now=200.0
    )
    assert lease2.fencing_token == 2
    store2.close()


def test_crash_5_after_partial_temp_artifact_write_before_atomic_rename(tmp_path: Path) -> None:
    """CRASH_5: crash after partial/temp artifact write, before final atomic rename/registration."""
    db_path = tmp_path / "crash5.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    partial_file = workspace / "output.mp4.part"
    partial_file.write_bytes(b"\x00\x00\x00\x18ftypmp42partial_bytes")
    final_file = workspace / "output.mp4"

    store1 = DurableStudioStore(db_path)
    store1.save_project_state(make_project("proj_crash_5"))
    store1.close()

    # Recovery cleans up .part staging file; final artifact is never visible as committed
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_5", allowed_root=workspace)
    assert str(partial_file) in recovery["cleaned_staging_files"]
    assert not partial_file.exists()
    assert not final_file.exists()
    with pytest.raises(IndependentMediaVerificationError, match="does not exist"):
        verify_media_artifact_independently(
            final_file,
            allowed_root=workspace,
            declared_sha256="sha256:" + "00" * 32,
            declared_media_kind="video",
        )
    store2.close()


def test_crash_6_after_artifact_registration_before_receipt_finalization(tmp_path: Path) -> None:
    """CRASH_6: crash after artifact registration, before receipt finalization."""
    db_path = tmp_path / "crash6.db"
    store1 = DurableStudioStore(db_path)
    project = make_project("proj_crash_6")
    store1.save_project_state(project)

    mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    mp4_path = tmp_path / "rendered.mp4"
    mp4_path.write_bytes(mp4_bytes)
    mp4_sha = f"sha256:{hashlib.sha256(mp4_bytes).hexdigest()}"

    verified = verify_media_artifact_independently(
        mp4_path,
        allowed_root=tmp_path,
        declared_sha256=mp4_sha,
        declared_media_kind="video",
        declared_byte_size=len(mp4_bytes),
    )
    assert verified["verified"] is True

    passport = ArtifactPassport(
        artifact_id="art_c6",
        artifact_type="video",
        content_hash=mp4_sha,
        byte_size=len(mp4_bytes),
        verification_receipt_id="rcpt_c6_final",
        causal_chain=ProvenanceCausalChain(
            request_id="req_c6",
            project_id="proj_crash_6",
            actor_id="alice",
            transaction_id="tx_c6",
            command_id="cmd_c6",
            operation="timeline.trim",
        ),
        execution_proof=ExecutionProof(
            executor_id="lane-v1",
            status="success",
            output_hash=mp4_sha,
            duration_ms=12.0,
            timestamp_utc="2026-10-03T00:00:00Z",
        ),
    ).with_computed_hash()
    store1.save_artifact_passport(passport)
    # Simulate crash right after passport registration before receipt is written
    assert store1.list_execution_receipts("proj_crash_6") == []
    store1.close()

    # Recovery deterministically backfills the missing execution receipt from verified passport
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_6")
    assert recovery["backfilled_receipts"] == ["rcpt_c6_final"]
    receipts = store2.list_execution_receipts("proj_crash_6")
    assert len(receipts) == 1
    assert receipts[0]["receipt_id"] == "rcpt_c6_final"
    assert receipts[0]["passport_hash"] == passport.passport_hash
    store2.close()


def test_crash_7_stale_worker_wakes_after_lease_expiry_and_is_fenced(tmp_path: Path) -> None:
    """CRASH_7: stale worker wakes after lease expiry while new worker holds monotonic token."""
    db_path = tmp_path / "crash7.db"
    store = DurableStudioStore(db_path)
    store.save_project_state(make_project("proj_crash_7"))

    lease_old = store.acquire_lease("job_c7", "attempt_1", "worker_slow", ttl_seconds=10.0, now=0.0)
    assert lease_old.fencing_token == 1

    # Lease expires at t=10.0; new worker claims job at t=15.0 and gets fencing_token=2
    lease_new = store.acquire_lease(
        "job_c7", "attempt_2", "worker_fast", ttl_seconds=30.0, now=15.0
    )
    assert lease_new.fencing_token == 2

    # New worker commits at t=20.0
    store.commit_fenced_side_effect(
        "job_c7",
        "attempt_2",
        lease_new.fencing_token,
        project_id="proj_crash_7",
        transaction_id="tx_new",
        command_id="cmd_c7",
        artifact_id="art_new",
        result_payload={"winner": "worker_fast"},
        now=20.0,
    )

    # Stale worker wakes at t=25.0 and attempts to commit with fencing_token=1 -> fail-closed
    with pytest.raises(StaleFencingTokenError):
        store.commit_fenced_side_effect(
            "job_c7",
            "attempt_1",
            lease_old.fencing_token,
            project_id="proj_crash_7",
            transaction_id="tx_old",
            command_id="cmd_c7",
            artifact_id="art_stale",
            result_payload={"winner": "worker_slow"},
            now=25.0,
        )

    receipts = store.list_execution_receipts("proj_crash_7")
    assert len(receipts) == 1
    assert receipts[0]["artifact_id"] == "art_new"
    store.close()


def test_crash_8_process_restart_idempotent_replay_and_targeted_undo(tmp_path: Path) -> None:
    """CRASH_8: process restart followed by idempotent command replay and targeted undo."""
    db_path = tmp_path / "crash8.db"
    store1 = DurableStudioStore(db_path)
    bus1 = CommandBus(
        state=make_project("proj_crash_8"), store=store1, authorizer=default_authorizer()
    )

    cmd1 = make_command(
        "timeline.mark",
        "c_m1",
        input={"at": "اینجا", "label": "Mark 1"},
        idempotency_key="idem_m1",
    )
    res1 = bus1.dispatch(cmd1)
    rev1_hash = bus1.state_hash
    store1.close()

    # Restart process
    store2 = DurableStudioStore(db_path)
    recovery = store2.reconcile_on_startup("proj_crash_8")
    assert recovery["state_hash"] == rev1_hash
    proj2, hist2 = store2.load_project("proj_crash_8")  # type: ignore[misc]
    bus2 = CommandBus(state=proj2, history=hist2, store=store2, authorizer=default_authorizer())

    # Idempotent replay after restart returns identical transaction without duplicate side effect
    replay = bus2.dispatch(
        make_command(
            "timeline.mark",
            "c_m1_retry",
            input={"at": "اینجا", "label": "Mark 1"},
            idempotency_key="idem_m1",
        )
    )
    assert replay.transaction_id == res1.transaction_id
    assert len(bus2.project.timeline.markers) == 1

    # Targeted undo by transaction_id rewinds the mark and evicts idempotency key durably
    undo_res = bus2.dispatch(
        make_command("system.undo", "c_undo", input={"transaction_id": res1.transaction_id})
    )
    assert undo_res.output["undone_transaction_id"] == res1.transaction_id
    assert bus2.project.timeline.markers == []
    store2.close()
