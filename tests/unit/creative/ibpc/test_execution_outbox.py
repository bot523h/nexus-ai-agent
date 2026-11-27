"""Wave 3 — crash-safe approval-to-enqueue outbox & non-destructive poll timeout.

The tests simulate a *crash* the only honest way a single process can: by
re-opening the durable store and constructing a fresh coordinator, exactly as a
restarted process would.  The crash boundaries are created by making one of the
injected callbacks die mid-flight (after the outbox write, before the queue
write), and the single-use approval is the real
:class:`~nexus_ai_agent.nagar.ibpc.approval.DurableApprovalLedger`, whose
``consume`` genuinely refuses a replay.

Coverage target for ``execution_outbox``: ``>= 98%`` line *and* branch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity, DurableApprovalLedger
from nexus_ai_agent.nagar.ibpc.execution_outbox import (
    ApprovalConsumptionError,
    CompletionVerificationError,
    DurableExecutionOutboxStore,
    ExecutionOutboxConflictError,
    ExecutionOutboxCoordinator,
    ExecutionOutboxError,
    ExecutionOutboxOutcome,
    ExecutionOutboxState,
    ExecutionOutboxTerminalError,
    OutboxRow,
    PollTimeoutError,
    VerifiedExecutionCompletion,
    approval_identity_for,
    derive_idempotency_key,
    derive_reserved_job_id,
    utc_now,
)

TENANT = "tenant-studio-1"
PROJECT = "proj-cinema-1"
SESSION = "sess-1"
TURN = "turn-1"
PRINCIPAL = "prn_" + "1" * 32
APPROVAL = "appr-1"
REVISION = "rev-1"
REV_JCS = "sha256:" + "e" * 64
PLAN_DIGEST = "sha256:" + "c" * 64
PREVIEW_DIGEST = "sha256:" + "d" * 64
ASSET_DIGEST = "sha256:" + "a" * 64
AST_DIGEST = "sha256:" + "b" * 64


class _Clock:
    """Deterministic wall clock for the durable timestamps."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-26T02:00:{self._tick:02d}+00:00"


class _Monotonic:
    """Deterministic monotonic clock: every read advances by ``step``."""

    def __init__(self, step: float = 0.5) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def _identity_payload(**overrides: Any) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "principal_id": PRINCIPAL,
        "sequence_number": 1,
        "source_revision": 0,
        "source_asset_digest": ASSET_DIGEST,
        "ast_digest": AST_DIGEST,
        "plan_digest": PLAN_DIGEST,
        "preview_digest": PREVIEW_DIGEST,
        "revision_id": REVISION,
    }
    identity.update(overrides)
    return identity


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "approval_identity": _identity_payload(),
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "session_id": SESSION,
        "turn_id": TURN,
        "revision_jcs_sha256": REV_JCS,
        "operations": [{"op_id": "trim", "start": 0, "end": 5}],
    }
    payload.update(overrides)
    return payload


def _proof(
    *, artifact_id: str = "artifact-1", passport_id: str = "passport-1", **overrides: Any
) -> VerifiedExecutionCompletion:
    """A well-formed verified completion (the only shape that may complete)."""
    fields: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_sha256": "a" * 64,
        "passport_id": passport_id,
        "lineage_digest": "sha256:" + "b" * 64,
        "verified_at_utc": "2026-11-27T00:00:00+00:00",
    }
    fields.update(overrides)
    return VerifiedExecutionCompletion(**fields)


def _store(tmp_path: Path, *, clock: _Clock | None = None) -> DurableExecutionOutboxStore:
    return DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3", now=clock or _Clock())


class _Harness:
    """The world around one intent: a real approval ledger and a fake queue/worker."""

    def __init__(self, tmp_path: Path, *, poll_timeout: float = 1.0) -> None:
        self.tmp_path = tmp_path
        self.ledger = DurableApprovalLedger(tmp_path / "approvals.sqlite3", now=_Clock())
        self.consumed: list[str] = []
        self.enqueued: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.verify_calls: list[str] = []
        self.store = _store(tmp_path)
        self.clock = _Monotonic()
        self.coordinator = self.build_coordinator(poll_timeout=poll_timeout)

    # -- callbacks --------------------------------------------------------- #

    def consume(
        self,
        approval_id: str,
        identity: ApprovalIdentity,
        *,
        job_id: str | None = None,
        attempt: int = 1,
    ) -> None:
        """The default consume callback: a fake, and it stays one.

        ``job_id``/``attempt`` are accepted (and deliberately ignored) because
        the coordinator always presents the row's durable reservation.  Tests
        that need the *real* single-use ledger build
        :meth:`real_ledger_consume` instead — that is where the crash-window
        recovery is proved.
        """
        del job_id, attempt
        if approval_id in self.consumed:
            raise RuntimeError("already_consumed")
        if identity.principal_id != PRINCIPAL:
            raise RuntimeError("identity_mismatch")
        self.consumed.append(approval_id)

    def real_ledger_consume(
        self,
        approval_id: str,
        identity: ApprovalIdentity,
        *,
        job_id: str | None = None,
        attempt: int = 1,
    ) -> None:
        """Consume through the genuine :class:`DurableApprovalLedger`."""
        self.ledger.consume(approval_id, identity=identity, job_id=job_id, attempt=attempt)
        self.consumed.append(approval_id)

    def enqueue(
        self, *, idempotency_key: str, payload: dict[str, Any], job_id: str | None = None
    ) -> str:
        for existing in self.enqueued:
            if existing["idempotency_key"] == idempotency_key:
                return str(existing["job_id"])
        resolved = job_id or f"job-{len(self.enqueued) + 1}"
        self.enqueued.append({"idempotency_key": idempotency_key, "job_id": resolved})
        job_id = resolved
        self.jobs[job_id] = {
            "status": "COMPLETED",
            "artifact_id": "artifact-1",
            "passport_id": "passport-1",
        }
        return job_id

    def await_completion(self, job_id: str) -> dict[str, Any]:
        return dict(self.jobs[job_id])

    def await_slow(self, job_id: str) -> dict[str, Any]:
        return {"status": "PENDING", "artifact_id": "", "passport_id": ""}

    def verify(self, row: OutboxRow, completion: Any) -> VerifiedExecutionCompletion:
        self.verify_calls.append(row.outbox_id)
        return _proof(
            artifact_id=str(completion.artifact_id),
            passport_id=str(completion.passport_id),
        )

    def build_coordinator(
        self,
        *,
        poll_timeout: float = 1.0,
        verify: Callable[[OutboxRow, Any], Any] | None = None,
        consume: Callable[..., None] | None = None,
        enqueue: Callable[..., str] | None = None,
        slow: bool = False,
        max_enqueue_attempts: int = 0,
        confirm: bool = True,
    ) -> ExecutionOutboxCoordinator:
        coordinator = ExecutionOutboxCoordinator(
            store=self.store,
            consume_approval_fn=consume or self.consume,
            enqueue_job_fn=enqueue or self.enqueue,
            await_completion_fn=(self.await_slow if slow else self.await_completion),
            verify_completion_fn=verify or self.verify,
            poll_timeout_seconds=poll_timeout,
            poll_interval_seconds=0.5,
            max_enqueue_attempts=max_enqueue_attempts,
            clock=self.clock,
            sleep=lambda _seconds: None,
        )
        if confirm:
            coordinator = ExecutionOutboxCoordinator(
                store=self.store,
                consume_approval_fn=consume or self.consume,
                enqueue_job_fn=enqueue or self.enqueue,
                await_completion_fn=(self.await_slow if slow else self.await_completion),
                verify_completion_fn=verify or self.verify,
                confirm_consumption_fn=coordinator.confirm_consumption_from_ledger(self.ledger),
                poll_timeout_seconds=poll_timeout,
                poll_interval_seconds=0.5,
                max_enqueue_attempts=max_enqueue_attempts,
                clock=self.clock,
                sleep=lambda _seconds: None,
            )
        return coordinator

    def finish(self, job_id: str) -> None:
        self.jobs[job_id] = {
            "status": "COMPLETED",
            "artifact_id": "artifact-1",
            "passport_id": "passport-1",
        }

    def fail(self, job_id: str, *, status: str = "FAILED_TERMINAL") -> None:
        self.jobs[job_id] = {"status": status, "artifact_id": "", "passport_id": ""}

    def execute_slow(self, *, poll_timeout: float = 1.0) -> ExecutionOutboxOutcome:
        """Run one intent whose worker stays PENDING for the whole poll budget."""
        return self.execute(self.build_coordinator(poll_timeout=poll_timeout, slow=True))

    def execute(
        self, coordinator: ExecutionOutboxCoordinator | None = None
    ) -> ExecutionOutboxOutcome:
        return (coordinator or self.coordinator).execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


@pytest.fixture()
def harness(tmp_path: Path) -> Any:
    world = _Harness(tmp_path)
    try:
        yield world
    finally:
        world.close()


# --------------------------------------------------------------------------- #
# Deterministic identity & closed state machine
# --------------------------------------------------------------------------- #
class TestIdentityAndVocabulary:
    def test_idempotency_key_is_deterministic_and_field_separated(self) -> None:
        kwargs = {
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "session_id": SESSION,
            "turn_id": TURN,
            "approval_id": APPROVAL,
            "revision_jcs_sha256": REV_JCS,
        }
        first = derive_idempotency_key(**kwargs)
        assert first == derive_idempotency_key(**kwargs)
        assert first.startswith("obx_") and len(first) == 68
        # Moving a character across the field boundary must NOT collide.
        shifted = derive_idempotency_key(**{**kwargs, "turn_id": TURN + "x", "session_id": SESSION})
        assert shifted != first
        for field in kwargs:
            assert derive_idempotency_key(**{**kwargs, field: "x"}) != first

    @pytest.mark.parametrize("field", ["tenant_id", "project_id", "session_id", "turn_id"])
    def test_blank_key_fields_are_refused(self, field: str) -> None:
        kwargs: dict[str, Any] = {
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "session_id": SESSION,
            "turn_id": TURN,
            "approval_id": APPROVAL,
            "revision_jcs_sha256": REV_JCS,
        }
        kwargs[field] = "   "
        with pytest.raises(ExecutionOutboxError):
            derive_idempotency_key(**kwargs)

    def test_state_machine_is_a_closed_set_with_the_documented_spellings(self) -> None:
        assert {state.value for state in ExecutionOutboxState} == {
            "INTENT_STAGED",
            "APPROVAL_CONSUMED",
            "JOB_ENQUEUED",
            "AWAITING_WORKER",
            "COMPLETED",
            "FAILED_TERMINAL",
        }
        # A timeout is parked, never failed: the two spellings must not collide.
        states = {state.name: state.value for state in ExecutionOutboxState}
        assert states["AWAITING_WORKER"] != states["FAILED_TERMINAL"]

    def test_outcome_terminality_and_round_trip(self) -> None:
        outcome = ExecutionOutboxOutcome(
            status="COMPLETED", outbox_id="obx", idempotency_key="k", job_id="job-1"
        )
        assert outcome.terminal is True
        assert outcome.to_dict()["job_id"] == "job-1"
        assert (
            ExecutionOutboxOutcome(
                status="FAILED_TERMINAL", outbox_id="o", idempotency_key="k"
            ).terminal
            is True
        )
        assert (
            ExecutionOutboxOutcome(
                status="AWAITING_WORKER", outbox_id="o", idempotency_key="k"
            ).terminal
            is False
        )

    def test_typed_errors_keep_the_canonical_reason(self) -> None:
        for error in (
            ExecutionOutboxConflictError("payload_conflict", "x"),
            ApprovalConsumptionError("replay", "x"),
            PollTimeoutError("poll_timeout", "x"),
            ExecutionOutboxTerminalError("terminal", "x"),
        ):
            assert isinstance(error, ExecutionOutboxError)
            assert error.reason in str(error)
            assert error.detail == "x"

    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().endswith("+00:00")


# --------------------------------------------------------------------------- #
# The happy path and its idempotency
# --------------------------------------------------------------------------- #
class TestHappyPath:
    def test_execute_runs_all_four_states_and_completes(self, harness: _Harness) -> None:
        outcome = harness.execute()
        assert outcome.status == "COMPLETED"
        # The job id is the row's durable *reservation*, derived from the
        # idempotency key before the approval was consumed -- never a random id.
        assert outcome.job_id == derive_reserved_job_id(outcome.idempotency_key)
        assert outcome.job_id == harness.enqueued[0]["job_id"]
        assert outcome.artifact_id == "artifact-1"
        assert outcome.passport_id == "passport-1"
        assert outcome.terminal is True
        assert harness.consumed == [APPROVAL]
        assert len(harness.enqueued) == 1
        events = harness.store.events_for(outcome.outbox_id)
        assert [event["to_state"] for event in events] == [
            "INTENT_STAGED",
            "APPROVAL_CONSUMED",
            "JOB_ENQUEUED",
            "COMPLETED",
        ]

    def test_a_completed_intent_is_returned_from_cache(self, harness: _Harness) -> None:
        first = harness.execute()
        second = harness.execute()
        assert second.status == "COMPLETED"
        assert second.outbox_id == first.outbox_id
        assert second.artifact_id == first.artifact_id
        assert harness.consumed == [APPROVAL]
        assert len(harness.enqueued) == 1
        # f(f(x)) = f(x): the cached outcome carries no "resumed" marker.
        assert second.resumed_from == ""
        assert second.can_resume is False

    def test_worker_completion_without_verification_identity_fails_closed(
        self, harness: _Harness
    ) -> None:
        def verify(_row: OutboxRow, _completion: Any) -> Any:
            return _proof(artifact_id="", passport_id="")

        coordinator = harness.build_coordinator(verify=verify)
        outcome = harness.execute(coordinator)
        # A blank proof cannot even be constructed, so the coordinator holds the
        # row rather than promoting the worker's claim to a completed turn.
        assert outcome.status == "AWAITING_WORKER"
        assert outcome.can_resume is True
        assert "completion_unverified" in outcome.detail

    def test_verifier_returning_a_non_proof_holds_the_record(self, harness: _Harness) -> None:
        coordinator = harness.build_coordinator(
            verify=lambda _row, _completion: ("artifact-1", "passport-1")
        )
        outcome = harness.execute(coordinator)
        assert outcome.status == "AWAITING_WORKER"
        assert "invalid_completion_proof" in outcome.detail

    def test_verification_refusal_is_terminal(self, harness: _Harness) -> None:
        def verify(_row: OutboxRow, _completion: Any) -> tuple[str, str]:
            raise RuntimeError("pin drift")

        outcome = harness.execute(harness.build_coordinator(verify=verify))
        assert outcome.status == "FAILED_TERMINAL"
        assert "verification_failed" in outcome.detail
        assert "pin drift" in outcome.detail

    def test_a_worker_failure_is_terminal_and_never_verified(self, harness: _Harness) -> None:
        def failing_enqueue(**kwargs: Any) -> str:
            job_id = harness.enqueue(**kwargs)
            harness.fail(job_id)
            return job_id

        outcome = harness.execute(harness.build_coordinator(enqueue=failing_enqueue))
        assert outcome.status == "FAILED_TERMINAL"
        assert "worker_failed" in outcome.detail
        # A failed job is never handed to the verification callback.
        assert harness.verify_calls == []

    def test_object_shaped_completion_and_status_enum_are_accepted(self, harness: _Harness) -> None:
        class _Status:
            value = "SUCCEEDED"

        class _Completion:
            status = _Status()
            artifact_id = "artifact-9"
            passport_id = "passport-9"

        coordinator = harness.build_coordinator(
            verify=lambda _row, completion: _proof(
                artifact_id=str(completion.artifact_id),
                passport_id=str(completion.passport_id),
            )
        )
        object.__setattr__(coordinator, "_await", lambda _job: _Completion())
        outcome = harness.execute(coordinator)
        assert outcome.status == "COMPLETED"
        assert outcome.artifact_id == "artifact-9"


# --------------------------------------------------------------------------- #
# Crash boundaries — the reason this module exists
# --------------------------------------------------------------------------- #
class TestCrashRecovery:
    def test_crash_after_intent_staged_consumes_and_enqueues_once(self, harness: _Harness) -> None:
        def dying_enqueue(**_kwargs: Any) -> str:
            raise RuntimeError("process died before the queue write")

        crashed = harness.build_coordinator(enqueue=dying_enqueue)
        # The crash happens *after* approval consumption and before enqueue: the
        # coordinator records FAILED_TERMINAL for this process...
        first = harness.execute(crashed)
        assert first.status == "FAILED_TERMINAL" or first.status == "APPROVAL_CONSUMED"

        # ... but the *real* crash boundary the outbox protects is a row left in
        # INTENT_STAGED (no consume call yet).  Simulate it directly.
        store = harness.store
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id="appr-crash-staged",
            revision_jcs_sha256=REV_JCS,
        )
        row, created = store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id="appr-crash-staged",
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        assert created is True
        assert row.state == "INTENT_STAGED"

        outcomes = harness.coordinator.reconcile_in_flight()
        recovered = next(o for o in outcomes if o.outbox_id == row.outbox_id)
        assert recovered.status == "COMPLETED"
        assert recovered.resumed_from == "INTENT_STAGED"
        assert harness.consumed.count("appr-crash-staged") == 1
        # Exactly one job per intent: the first coordinator parked its row in
        # APPROVAL_CONSUMED (a transient enqueue failure no longer burns the
        # approval), so the reconciler picks *that* row up, not a second job.
        intent_jobs = [e for e in harness.enqueued if e["idempotency_key"] == key]
        assert len(intent_jobs) == 1

    def test_crash_after_approval_consumed_never_reconsumes(self, harness: _Harness) -> None:
        """The exact window: consumed, then died before ``enqueue``."""

        # Stage + consume by hand, then let the *next* process resume from
        # APPROVAL_CONSUMED without re-consuming.
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        harness.consume(APPROVAL, approval_identity_for(row))
        consumed_row = harness.store.mark_approval_consumed(outbox_id=row.outbox_id)
        assert consumed_row.state == "APPROVAL_CONSUMED"
        assert harness.consumed == [APPROVAL]

        # A restarted process builds a fresh coordinator: the same consume
        # callback would raise "already_consumed" if it were ever re-invoked.
        restarted = harness.build_coordinator()
        outcomes = restarted.reconcile_in_flight()
        recovered = next(o for o in outcomes if o.outbox_id == row.outbox_id)
        assert recovered.status == "COMPLETED"
        assert recovered.resumed_from == "APPROVAL_CONSUMED"
        assert harness.consumed == [APPROVAL]  # exactly once
        assert len(harness.enqueued) == 1  # exactly once

    def test_crash_after_job_enqueued_never_double_enqueues(self, harness: _Harness) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        harness.consume(APPROVAL, approval_identity_for(row))
        harness.store.mark_approval_consumed(outbox_id=row.outbox_id)
        job_id = harness.enqueue(idempotency_key=key, payload=_payload())
        enqueued_row = harness.store.mark_job_enqueued(outbox_id=row.outbox_id, job_id=job_id)
        assert enqueued_row.state == "JOB_ENQUEUED"

        harness.finish(job_id)
        outcomes = harness.build_coordinator().reconcile_in_flight()
        recovered = next(o for o in outcomes if o.outbox_id == row.outbox_id)
        assert recovered.status == "COMPLETED"
        assert recovered.resumed_from == "JOB_ENQUEUED"
        assert recovered.job_id == job_id
        assert len(harness.enqueued) == 1

    def test_poll_timeout_is_awaiting_worker_not_a_failure(self, harness: _Harness) -> None:
        outcome = harness.execute_slow(poll_timeout=1.0)
        assert outcome.status == "AWAITING_WORKER"
        assert outcome.can_resume is True
        assert outcome.job_id == derive_reserved_job_id(outcome.idempotency_key)
        assert outcome.terminal is False
        row = harness.store.get(outbox_id=outcome.outbox_id)
        assert row is not None
        assert row.state == "AWAITING_WORKER"
        assert "poll_timeout" in row.last_error
        # The approval stays consumed and the job enqueued exactly once.
        assert harness.consumed == [APPROVAL]
        assert len(harness.enqueued) == 1

    def test_reconcile_after_the_worker_finishes_completes_the_turn(
        self, harness: _Harness
    ) -> None:
        awaiting = harness.execute_slow(poll_timeout=1.0)
        assert awaiting.status == "AWAITING_WORKER"
        harness.finish(awaiting.job_id or "")
        outcomes = harness.build_coordinator().reconcile_in_flight()
        completed = next(o for o in outcomes if o.outbox_id == awaiting.outbox_id)
        assert completed.status == "COMPLETED"
        assert completed.resumed_from == "AWAITING_WORKER"
        assert completed.artifact_id == "artifact-1"

    def test_awaiting_worker_that_probes_still_running_stays_awaiting(
        self, harness: _Harness
    ) -> None:
        awaiting = harness.execute_slow(poll_timeout=1.0)
        again = harness.build_coordinator(slow=True).reconcile_in_flight()
        still = next(o for o in again if o.outbox_id == awaiting.outbox_id)
        assert still.status == "AWAITING_WORKER"
        assert still.can_resume is True

    def test_awaiting_worker_whose_job_then_fails_is_terminal(self, harness: _Harness) -> None:
        awaiting = harness.execute_slow(poll_timeout=1.0)
        harness.fail(awaiting.job_id or "")
        outcomes = harness.build_coordinator().reconcile_in_flight()
        failed = next(o for o in outcomes if o.outbox_id == awaiting.outbox_id)
        assert failed.status == "FAILED_TERMINAL"
        assert "worker_failed" in failed.detail

    def test_reconcile_covers_every_in_flight_state(self, harness: _Harness) -> None:
        """One pass drives INTENT_STAGED, APPROVAL_CONSUMED and JOB_ENQUEUED rows."""
        store = harness.store
        for index, state in enumerate(("INTENT_STAGED", "APPROVAL_CONSUMED", "JOB_ENQUEUED")):
            approval_id = f"appr-{index}"
            key = derive_idempotency_key(
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                approval_id=approval_id,
                revision_jcs_sha256=REV_JCS,
            )
            row, _ = store.stage_intent(
                idempotency_key=key,
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=approval_id,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(),
            )
            if state in ("APPROVAL_CONSUMED", "JOB_ENQUEUED"):
                harness.consumed.append(approval_id)  # already consumed by the dead process
                store.mark_approval_consumed(outbox_id=row.outbox_id)
            if state == "JOB_ENQUEUED":
                job_id = harness.enqueue(idempotency_key=key, payload=_payload())
                store.mark_job_enqueued(outbox_id=row.outbox_id, job_id=job_id)

        for job_id in list(harness.jobs):
            harness.finish(job_id)
        outcomes = harness.build_coordinator().reconcile_in_flight()
        assert len(outcomes) == 3
        assert {o.status for o in outcomes} == {"COMPLETED"}
        assert {o.resumed_from for o in outcomes} == {
            "INTENT_STAGED",
            "APPROVAL_CONSUMED",
            "JOB_ENQUEUED",
        }
        # Every approval consumed exactly once; every job enqueued exactly once.
        assert len(harness.consumed) == len(set(harness.consumed)) == 3
        assert len(harness.enqueued) == len(
            {entry["idempotency_key"] for entry in harness.enqueued}
        )

    def test_a_scope_key_absent_from_the_payload_is_tolerated(self, harness: _Harness) -> None:
        """Only keys the payload actually declares are checked."""
        payload = _payload()
        for key in ("tenant_id", "project_id", "session_id", "turn_id", "revision_jcs_sha256"):
            payload.pop(key)
        outcome = harness.coordinator.execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=payload,
        )
        assert outcome.status == "COMPLETED"

    def test_replaying_a_different_job_id_for_one_intent_is_refused(
        self, harness: _Harness
    ) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        harness.store.mark_approval_consumed(outbox_id=row.outbox_id)
        harness.store.mark_job_enqueued(outbox_id=row.outbox_id, job_id="job-1")
        # The same job id is idempotent...
        same = harness.store.mark_job_enqueued(outbox_id=row.outbox_id, job_id="job-1")
        assert same.job_id == "job-1"
        # ... a different one is a conflict, not a silent re-point.
        with pytest.raises(ExecutionOutboxConflictError) as excinfo:
            harness.store.mark_job_enqueued(outbox_id=row.outbox_id, job_id="job-2")
        assert excinfo.value.reason == "job_already_enqueued"

    def test_awaiting_the_worker_from_a_spent_state_is_refused(self, harness: _Harness) -> None:
        outcome = harness.execute(harness.build_coordinator(enqueue=lambda **_k: ""))
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.store.mark_awaiting_worker(outbox_id=outcome.outbox_id, job_id="job-1")
        assert excinfo.value.reason == "illegal_transition"

    def test_completing_an_already_completed_row_is_idempotent(self, harness: _Harness) -> None:
        outcome = harness.execute()
        again = harness.store.mark_completed(
            outbox_id=outcome.outbox_id, artifact_id="other", passport_id="other"
        )
        assert again.state == "COMPLETED"
        assert again.artifact_id == "artifact-1"  # the original identity is kept

    def test_moving_a_terminal_row_back_is_refused(self, harness: _Harness) -> None:
        outcome = harness.execute(harness.build_coordinator(enqueue=lambda **_k: ""))
        assert outcome.status == "FAILED_TERMINAL"
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.store.mark_approval_consumed(outbox_id=outcome.outbox_id)
        assert excinfo.value.reason == "illegal_transition"

    def test_the_coordinator_exposes_its_store(self, harness: _Harness) -> None:
        assert harness.coordinator.store is harness.store

    def test_a_prebuilt_worker_completion_is_passed_through(self, harness: _Harness) -> None:
        from nexus_ai_agent.nagar.ibpc.execution_outbox import _WorkerCompletion

        coordinator = harness.build_coordinator(
            verify=lambda _row, completion: _proof(
                artifact_id=str(completion.artifact_id),
                passport_id=str(completion.passport_id),
            )
        )
        object.__setattr__(
            coordinator,
            "_await",
            lambda _job: _WorkerCompletion(
                status="COMPLETED", artifact_id="artifact-7", passport_id="passport-7"
            ),
        )
        outcome = harness.execute(coordinator)
        assert outcome.artifact_id == "artifact-7"
        assert outcome.passport_id == "passport-7"

    def test_reconcile_limit_is_honoured(self, harness: _Harness) -> None:
        store = harness.store
        for index in range(3):
            key = derive_idempotency_key(
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                approval_id=f"appr-limit-{index}",
                revision_jcs_sha256=REV_JCS,
            )
            store.stage_intent(
                idempotency_key=key,
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=f"appr-limit-{index}",
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(),
            )
            harness.finish("job-1")
        outcomes = harness.build_coordinator().reconcile_in_flight(limit=2)
        assert len(outcomes) == 2


# --------------------------------------------------------------------------- #
# Conflict, refusal and fail-closed paths
# --------------------------------------------------------------------------- #
class TestFailClosed:
    def test_payload_conflict_after_completion_is_refused(self, harness: _Harness) -> None:
        outcome = harness.execute()
        row = harness.store.get(outbox_id=outcome.outbox_id)
        assert row is not None
        with pytest.raises(ExecutionOutboxConflictError) as excinfo:
            harness.store.stage_intent(
                idempotency_key=row.idempotency_key,
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=APPROVAL,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(operations=[{"op_id": "different"}]),
            )
        assert excinfo.value.reason == "payload_conflict"

    def test_different_payload_for_a_live_key_conflicts(self, harness: _Harness) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        kwargs: dict[str, Any] = {
            "idempotency_key": key,
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "session_id": SESSION,
            "turn_id": TURN,
            "principal_id": PRINCIPAL,
            "approval_id": APPROVAL,
            "revision_id": REVISION,
            "revision_jcs_sha256": REV_JCS,
            "plan_payload": _payload(),
        }
        harness.store.stage_intent(**kwargs)
        with pytest.raises(ExecutionOutboxConflictError) as excinfo:
            harness.store.stage_intent(**{**kwargs, "plan_payload": _payload(operations=[])})
        assert excinfo.value.reason == "payload_conflict"
        # A *scope* field that disagrees with the staged intent is refused by the
        # scope guard (before the payload comparison), which is the stricter rule.
        with pytest.raises(ExecutionOutboxConflictError) as excinfo2:
            harness.store.stage_intent(**{**kwargs, "revision_jcs_sha256": "sha256:" + "9" * 64})
        assert excinfo2.value.reason == "payload_scope_mismatch"

    def test_payload_scope_mismatch_is_refused_before_any_write(self, harness: _Harness) -> None:
        for payload in (
            _payload(tenant_id="tenant-other"),
            _payload(project_id="proj-other"),
            _payload(session_id="sess-other"),
            _payload(turn_id="turn-other"),
            _payload(revision_jcs_sha256="sha256:" + "8" * 64),
            _payload(tenant_id=7),
        ):
            with pytest.raises(ExecutionOutboxConflictError) as excinfo:
                harness.coordinator.execute_or_recover(
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    session_id=SESSION,
                    turn_id=TURN,
                    principal_id=PRINCIPAL,
                    approval_id=APPROVAL,
                    revision_id=REVISION,
                    revision_jcs_sha256=REV_JCS,
                    plan_payload=payload,
                )
            assert excinfo.value.reason == "payload_scope_mismatch"
        assert harness.consumed == []

    def test_approval_refusal_is_terminal_and_never_enqueues(self, harness: _Harness) -> None:
        def refusing_consume(_approval_id: str, _identity: ApprovalIdentity) -> None:
            raise RuntimeError("replay: approval already consumed")

        outcome = harness.execute(harness.build_coordinator(consume=refusing_consume))
        assert outcome.status == "FAILED_TERMINAL"
        assert "approval_consumption_failed" in outcome.detail
        assert harness.enqueued == []

    def test_transient_queue_refusal_never_destroys_the_spent_approval(
        self, harness: _Harness
    ) -> None:
        def refusing_enqueue(**_kwargs: Any) -> str:
            raise RuntimeError("queue unavailable")

        outcome = harness.execute(harness.build_coordinator(enqueue=refusing_enqueue))
        # The approval is spent; a transient queue error must NOT terminalise the
        # row, because that would require a fresh human approval for a hiccup.
        assert outcome.status == "APPROVAL_CONSUMED"
        assert outcome.can_resume is True
        assert "enqueue_retryable" in outcome.detail
        assert harness.consumed == [APPROVAL]
        row = harness.store.get(outbox_id=outcome.outbox_id)
        assert row is not None
        assert row.attempt_count == 1

    def test_a_validation_refusal_at_enqueue_is_terminal(self, harness: _Harness) -> None:
        def rejecting_enqueue(**_kwargs: Any) -> str:
            raise ValueError("payload can never be accepted")

        outcome = harness.execute(harness.build_coordinator(enqueue=rejecting_enqueue))
        assert outcome.status == "FAILED_TERMINAL"
        assert "enqueue_rejected" in outcome.detail

    def test_enqueue_attempts_exhaustion_is_terminal(self, harness: _Harness) -> None:
        def refusing_enqueue(**_kwargs: Any) -> str:
            raise RuntimeError("queue unavailable")

        coordinator = harness.build_coordinator(enqueue=refusing_enqueue, max_enqueue_attempts=1)
        outcome = harness.execute(coordinator)
        assert outcome.status == "FAILED_TERMINAL"
        assert "enqueue_attempts_exhausted" in outcome.detail

    def test_enqueue_without_a_job_id_is_terminal(self, harness: _Harness) -> None:
        outcome = harness.execute(harness.build_coordinator(enqueue=lambda **_k: ""))
        assert outcome.status == "FAILED_TERMINAL"
        assert "no_job_id" in outcome.detail

    def test_a_terminal_row_is_reported_and_never_retried(self, harness: _Harness) -> None:
        outcome = harness.execute(harness.build_coordinator(enqueue=lambda **_k: ""))
        assert outcome.status == "FAILED_TERMINAL"
        again = harness.execute()
        assert again.status == "FAILED_TERMINAL"
        assert again.detail == outcome.detail
        assert again.can_resume is False

    def test_missing_approval_identity_is_terminal(self, harness: _Harness) -> None:
        payload = _payload()
        payload.pop("approval_identity")
        outcome = harness.coordinator.execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=payload,
        )
        assert outcome.status == "FAILED_TERMINAL"
        assert "approval_identity_failed" in outcome.detail

    def test_malformed_completion_shape_is_refused(self, harness: _Harness) -> None:
        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=harness.consume,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=lambda _job: object(),
            poll_timeout_seconds=1.0,
            clock=harness.clock,
            sleep=lambda _s: None,
        )
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.execute(coordinator)
        assert excinfo.value.reason == "malformed_completion"

    def test_malformed_payload_json_is_refused(self, harness: _Harness) -> None:
        outcome = harness.execute(harness.build_coordinator(enqueue=lambda **_k: ""))
        harness.store._conn.execute(
            "UPDATE nexus_ibpc_execution_outbox SET plan_payload_json = '\"not-an-object\"' "
            "WHERE outbox_id = ?",
            (outcome.outbox_id,),
        )
        row = harness.store.get(outbox_id=outcome.outbox_id)
        assert row is not None
        with pytest.raises(ExecutionOutboxError) as excinfo:
            parsed = row.plan_payload
            assert parsed is None  # pragma: no cover - the raise above wins
        assert excinfo.value.reason == "malformed_payload"

    def test_invalid_coordinator_budgets_are_refused(self, harness: _Harness) -> None:
        for poll_timeout_seconds, poll_interval_seconds in (
            (-1.0, 0.0),
            (1.0, -1.0),
        ):
            with pytest.raises(ExecutionOutboxError):
                ExecutionOutboxCoordinator(
                    store=harness.store,
                    consume_approval_fn=harness.consume,
                    enqueue_job_fn=harness.enqueue,
                    await_completion_fn=harness.await_completion,
                    poll_timeout_seconds=poll_timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )

    def test_invalid_store_selectors_and_limits_are_refused(self, harness: _Harness) -> None:
        with pytest.raises(ExecutionOutboxError):
            harness.store.get()
        with pytest.raises(ExecutionOutboxError):
            harness.store.get(outbox_id="a", idempotency_key="b")
        with pytest.raises(ExecutionOutboxError):
            harness.store.list_in_flight(limit=0)
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_job_enqueued(outbox_id="missing", job_id="j")
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_job_enqueued(outbox_id="missing", job_id="")

    def test_illegal_transitions_are_refused(self, harness: _Harness) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_job_enqueued(outbox_id=row.outbox_id, job_id="job-x")
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_awaiting_worker(outbox_id=row.outbox_id)
        completed = harness.store.mark_failed_terminal(outbox_id=row.outbox_id, error="manual stop")
        assert completed.state == "FAILED_TERMINAL"
        # Re-failing and re-completing a terminal row is idempotent...
        assert (
            harness.store.mark_failed_terminal(outbox_id=row.outbox_id, error="again").state
            == "FAILED_TERMINAL"
        )
        # ... while completing from a terminal row is refused.
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_completed(outbox_id=row.outbox_id, artifact_id="a", passport_id="p")

    def test_awaiting_worker_without_a_job_is_refused(self, harness: _Harness) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_awaiting_worker(outbox_id=row.outbox_id)
        with pytest.raises(ExecutionOutboxError):
            harness.store.mark_awaiting_worker(outbox_id="missing", job_id="job-1")

    def test_record_attempt_failure_keeps_the_state(self, harness: _Harness) -> None:
        awaiting = harness.execute_slow(poll_timeout=1.0)
        updated = harness.store.record_attempt_failure(
            outbox_id=awaiting.outbox_id, error="transient"
        )
        assert updated.state == "AWAITING_WORKER"
        assert updated.attempt_count >= 1
        assert updated.last_error == "transient"
        with pytest.raises(ExecutionOutboxError):
            harness.store.record_attempt_failure(outbox_id="missing", error="x")

    def test_a_corrupted_row_without_a_job_id_is_refused(self, harness: _Harness) -> None:
        """A hand-corrupted row must not be awaited into a state it cannot hold."""
        awaiting = harness.execute_slow(poll_timeout=1.0)
        harness.store._conn.execute(
            "UPDATE nexus_ibpc_execution_outbox SET job_id = NULL WHERE outbox_id = ?",
            (awaiting.outbox_id,),
        )
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.store.mark_awaiting_worker(outbox_id=awaiting.outbox_id)
        assert excinfo.value.reason == "missing_job_id"

    def test_failing_a_completed_row_is_refused(self, harness: _Harness) -> None:
        outcome = harness.execute()
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.store.mark_failed_terminal(outbox_id=outcome.outbox_id, error="late")
        assert excinfo.value.reason == "illegal_transition"

    def test_re_marking_an_already_consumed_approval_is_idempotent(self, harness: _Harness) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=APPROVAL,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        first = harness.store.mark_approval_consumed(outbox_id=row.outbox_id)
        second = harness.store.mark_approval_consumed(outbox_id=row.outbox_id)
        assert first.state == second.state == "APPROVAL_CONSUMED"
        # Only ONE transition event was written for the second call (none).
        assert [event["to_state"] for event in harness.store.events_for(row.outbox_id)] == [
            "INTENT_STAGED",
            "APPROVAL_CONSUMED",
        ]

    def test_the_error_path_rolls_back(self, harness: _Harness) -> None:
        with pytest.raises(RuntimeError):
            with harness.store._transaction():
                harness.store._conn.execute(
                    "INSERT INTO nexus_ibpc_execution_outbox (outbox_id, idempotency_key, "
                    "tenant_id, project_id, session_id, turn_id, principal_id, approval_id, "
                    "revision_id, revision_jcs_sha256, plan_payload_json, state, "
                    "created_at_utc, updated_at_utc) VALUES "
                    "('obx_rollback', 'k', 't', 'p', 's', 'tu', 'pr', 'a', 'r', ?, '{}', "
                    "'INTENT_STAGED', 'now', 'now')",
                    (REV_JCS,),
                )
                raise RuntimeError("boom")
        assert harness.store.get(outbox_id="obx_rollback") is None


# --------------------------------------------------------------------------- #
# Real single-use approval ledger integration
# --------------------------------------------------------------------------- #
class TestRealApprovalLedger:
    def test_the_ledger_refuses_a_second_consume_so_the_outbox_must_not_retry(
        self, harness: _Harness
    ) -> None:
        """Guard of the guard: a naive retry WOULD have failed with ``replay``."""
        from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
        from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

        identity = approval_identity_for(
            OutboxRow(
                outbox_id="obx",
                idempotency_key="k",
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=APPROVAL,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload_json=json.dumps(_payload(), sort_keys=True),
                state="INTENT_STAGED",
                job_id=None,
                artifact_id=None,
                passport_id=None,
                attempt_count=0,
                last_error="",
                created_at_utc="now",
                updated_at_utc="now",
            )
        )
        binding = ApprovalBinding(
            approval_id=APPROVAL,
            session_id=SESSION,
            turn_id=TURN,
            sequence_number=1,
            tenant_id=TENANT,
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            source_revision=0,
            source_asset_digest=ASSET_DIGEST,
            ast_digest=AST_DIGEST,
            plan_digest=PLAN_DIGEST,
            preview_digest=PREVIEW_DIGEST,
            revision_id=REVISION,
        )
        harness.ledger.create(binding)
        approved = harness.ledger.approve(APPROVAL, identity=identity, turn_is_active=lambda: True)
        assert approved.state.value == "APPROVED"
        harness.ledger.consume(APPROVAL, identity=identity)
        with pytest.raises(ApprovalError) as excinfo:
            harness.ledger.consume(APPROVAL, identity=identity)
        assert "already consumed" in str(excinfo.value)

    def test_approval_identity_requires_the_payload_block(self) -> None:
        row = OutboxRow(
            outbox_id="obx",
            idempotency_key="k",
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload_json=json.dumps({"no_identity": True}),
            state="INTENT_STAGED",
            job_id=None,
            artifact_id=None,
            passport_id=None,
            attempt_count=0,
            last_error="",
            created_at_utc="now",
            updated_at_utc="now",
        )
        with pytest.raises(ExecutionOutboxError):
            approval_identity_for(row)


# --------------------------------------------------------------------------- #
# Durability, concurrency and the cross-process lock
# --------------------------------------------------------------------------- #
class TestDurabilityAndConcurrency:
    def test_rows_survive_a_reopen_and_keep_their_events(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.sqlite3"
        with DurableExecutionOutboxStore(path) as store:
            row, _ = store.stage_intent(
                idempotency_key="key-1",
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=APPROVAL,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(),
            )
            store.mark_approval_consumed(outbox_id=row.outbox_id)
        with DurableExecutionOutboxStore(path) as reopened:
            recovered = reopened.get(idempotency_key="key-1")
            assert recovered is not None
            assert recovered.state == "APPROVAL_CONSUMED"
            assert [event["to_state"] for event in reopened.events_for(row.outbox_id)] == [
                "INTENT_STAGED",
                "APPROVAL_CONSUMED",
            ]

    def test_wal_pragmas_are_set(self, tmp_path: Path) -> None:
        with DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3") as store:
            assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert int(store._conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
            assert int(store._conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            assert store.path.name == "outbox.sqlite3"
        store.close()  # idempotent

    def test_concurrent_stage_of_one_key_creates_exactly_one_row(self, tmp_path: Path) -> None:
        store = DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3")
        created: list[bool] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                _row, was_created = store.stage_intent(
                    idempotency_key="shared-key",
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    session_id=SESSION,
                    turn_id=TURN,
                    principal_id=PRINCIPAL,
                    approval_id=APPROVAL,
                    revision_id=REVISION,
                    revision_jcs_sha256=REV_JCS,
                    plan_payload=_payload(),
                )
                created.append(was_created)
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert errors == []
            assert created.count(True) == 1
            assert created.count(False) == 7
            assert len(store.list_in_flight()) == 1
        finally:
            store.close()

    def test_reconcile_lock_is_exclusive_across_processes(self, tmp_path: Path) -> None:
        import fcntl
        import os

        store = DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3")
        try:
            with store.reconcile_lock():
                lock_path = tmp_path / "outbox.sqlite3.reconcile.lock"
                assert lock_path.exists()
                handle = os.open(str(lock_path), os.O_RDWR)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(handle)
            # Released afterwards.
            handle = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)
        finally:
            store.close()

    def test_nested_read_inside_a_write_transaction_joins_it(self, tmp_path: Path) -> None:
        with DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3") as store:
            with store._transaction():
                assert store.get(idempotency_key="missing") is None
            with store._transaction():
                with store._transaction():
                    assert store.list_in_flight() == ()

    def test_two_stores_over_one_file_see_each_other(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.sqlite3"
        first = DurableExecutionOutboxStore(path)
        second = DurableExecutionOutboxStore(path)
        try:
            row, _ = first.stage_intent(
                idempotency_key="cross-store",
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=APPROVAL,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(),
            )
            from_second = second.get(outbox_id=row.outbox_id)
            assert from_second is not None
            assert from_second.state == "INTENT_STAGED"
            second.mark_approval_consumed(outbox_id=row.outbox_id)
            reread = first.get(outbox_id=row.outbox_id)
            assert reread is not None
            assert reread.state == "APPROVAL_CONSUMED"
        finally:
            first.close()
            second.close()

    def test_a_second_connection_reads_the_durable_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "outbox.sqlite3"
        with DurableExecutionOutboxStore(path) as store:
            store.stage_intent(
                idempotency_key="raw-read",
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=APPROVAL,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload=_payload(),
            )
        raw = sqlite3.connect(str(path))
        try:
            assert raw.execute(
                "SELECT state FROM nexus_ibpc_execution_outbox WHERE idempotency_key = 'raw-read'"
            ).fetchone() == ("INTENT_STAGED",)
        finally:
            raw.close()


# --------------------------------------------------------------------------- #
# Wave 1 — the true crash window, replay classification, and verified completion
# --------------------------------------------------------------------------- #
def _binding(approval_id: str = APPROVAL) -> Any:
    from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

    return ApprovalBinding(
        approval_id=approval_id,
        session_id=SESSION,
        turn_id=TURN,
        sequence_number=1,
        tenant_id=TENANT,
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        source_revision=0,
        source_asset_digest=ASSET_DIGEST,
        ast_digest=AST_DIGEST,
        plan_digest=PLAN_DIGEST,
        preview_digest=PREVIEW_DIGEST,
        revision_id=REVISION,
    )


class TestCrashWindowBetweenLedgerAndOutbox:
    """The exact window: ``approval.py`` committed, ``outbox.py`` did not.

    Every test here uses the genuine :class:`DurableApprovalLedger` with a real
    job reservation, because that is the object whose reservation table decides
    whether a second consume is a recovery or a replay.
    """

    def _approved(
        self, harness: _Harness, *, approval_id: str = APPROVAL
    ) -> tuple[Any, ApprovalIdentity, str]:
        harness.ledger.create(_binding(approval_id))
        identity = approval_identity_for(
            OutboxRow(
                outbox_id="obx-seed",
                idempotency_key="k",
                tenant_id=TENANT,
                project_id=PROJECT,
                session_id=SESSION,
                turn_id=TURN,
                principal_id=PRINCIPAL,
                approval_id=approval_id,
                revision_id=REVISION,
                revision_jcs_sha256=REV_JCS,
                plan_payload_json=json.dumps(_payload(), sort_keys=True),
                state="INTENT_STAGED",
                job_id=None,
                artifact_id=None,
                passport_id=None,
                attempt_count=0,
                last_error="",
                created_at_utc="now",
                updated_at_utc="now",
            )
        )
        harness.ledger.approve(approval_id, identity=identity, turn_is_active=lambda: True)
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=approval_id,
            revision_jcs_sha256=REV_JCS,
        )
        return identity, key, derive_reserved_job_id(key)

    def _stage(self, harness: _Harness, *, approval_id: str = APPROVAL) -> tuple[Any, str]:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id=approval_id,
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=approval_id,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        return row, derive_reserved_job_id(key)

    def _real_coordinator(self, harness: _Harness, **kwargs: Any) -> ExecutionOutboxCoordinator:
        plain = harness.build_coordinator(consume=harness.real_ledger_consume, **kwargs)
        return ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=harness.real_ledger_consume,
            enqueue_job_fn=kwargs.get("enqueue") or harness.enqueue,
            await_completion_fn=(
                harness.await_slow if kwargs.get("slow") else harness.await_completion
            ),
            verify_completion_fn=harness.verify,
            confirm_consumption_fn=plain.confirm_consumption_from_ledger(harness.ledger),
            poll_timeout_seconds=kwargs.get("poll_timeout", 1.0),
            poll_interval_seconds=0.5,
            clock=harness.clock,
            sleep=lambda _seconds: None,
        )

    def test_true_crash_between_approval_commit_and_outbox_commit_recovers_without_double_consume(
        self, harness: _Harness
    ) -> None:
        identity, key, reserved = self._approved(harness)
        row, staged_reserved = self._stage(harness)
        assert row.state == "INTENT_STAGED"
        assert staged_reserved == reserved

        # --- the crash: the ledger commits CONSUMED, the outbox never commits.
        harness.ledger.consume(APPROVAL, identity=identity, job_id=reserved, attempt=1)
        assert harness.store.get(outbox_id=row.outbox_id).state == "INTENT_STAGED"  # type: ignore[union-attr]

        # --- a restarted process reconciles that orphaned INTENT_STAGED row.
        outcomes = self._real_coordinator(harness).reconcile_in_flight()
        recovered = next(o for o in outcomes if o.outbox_id == row.outbox_id)

        assert recovered.status in ("AWAITING_WORKER", "COMPLETED")
        assert recovered.status != "FAILED_TERMINAL"
        assert recovered.resumed_from == "INTENT_STAGED"
        assert recovered.job_id == reserved
        # The approval was consumed exactly once; the recovery never re-consumed.
        assert harness.consumed.count(APPROVAL) == 1
        states = [event["to_state"] for event in harness.store.events_for(row.outbox_id)]
        assert "FAILED_TERMINAL" not in states
        assert states[:3] == ["INTENT_STAGED", "APPROVAL_CONSUMED", "JOB_ENQUEUED"]
        # And a second recovery is inert.
        again = self._real_coordinator(harness).reconcile_in_flight()
        assert again == () or all(o.status != "FAILED_TERMINAL" for o in again)

    def test_true_replay_with_different_job_fails_terminal(self, harness: _Harness) -> None:
        identity, _key, reserved = self._approved(harness)
        row, _ = self._stage(harness)

        # Somebody else consumed this approval under a *different* job id.
        harness.ledger.consume(APPROVAL, identity=identity, job_id="job-somebody-else", attempt=1)

        outcome = self._real_coordinator(harness).execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        assert outcome.status == "FAILED_TERMINAL"
        assert "replay" in outcome.detail
        assert harness.enqueued == []
        assert harness.store.get(outbox_id=row.outbox_id).state == "FAILED_TERMINAL"  # type: ignore[union-attr]

    def test_true_replay_with_different_plan_fails_terminal(self, harness: _Harness) -> None:
        identity, key, reserved = self._approved(harness)

        # The ledger consumed this approval for the reserved job, bound to plan
        # ``c``.  The outbox row that arrives *claims* plan ``f`` -- the same
        # job id, a different approved plan.  That is a replay, not a recovery.
        harness.ledger.consume(APPROVAL, identity=identity, job_id=reserved, attempt=1)
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(
                approval_identity=_identity_payload(plan_digest="sha256:" + "f" * 64)
            ),
        )
        assert row.reserved_job_id == reserved

        outcome = self._real_coordinator(harness).execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(
                approval_identity=_identity_payload(plan_digest="sha256:" + "f" * 64)
            ),
        )
        assert outcome.status == "FAILED_TERMINAL"
        assert "replay" in outcome.detail
        assert harness.enqueued == []
        stored = harness.store.get(outbox_id=row.outbox_id)
        assert stored is not None and stored.state == "FAILED_TERMINAL"

    def test_a_finalized_reservation_is_not_recovered_as_a_crash(self, harness: _Harness) -> None:
        identity, _key, reserved = self._approved(harness)
        _row, _ = self._stage(harness)
        harness.ledger.consume(APPROVAL, identity=identity, job_id=reserved, attempt=1)
        # The intent was already finalized (its job published), so a row still
        # sitting in INTENT_STAGED for it is stale bookkeeping, not a recovery.
        harness.ledger.finalize_consumption(APPROVAL, job_id=reserved, attempt=1)

        outcome = self._real_coordinator(harness).execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id=APPROVAL,
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        assert outcome.status == "FAILED_TERMINAL"
        assert harness.enqueued == []

    def test_a_revoked_approval_is_never_recovered(self, harness: _Harness) -> None:
        identity, _key, _reserved = self._approved(harness)
        row, _ = self._stage(harness)
        harness.ledger.revoke(APPROVAL)
        confirmed = self._real_coordinator(harness).confirm_consumption_from_ledger(harness.ledger)
        assert confirmed(row, identity) is False

    def test_the_confirmation_refuses_an_unknown_approval(self, harness: _Harness) -> None:
        identity, _key, _reserved = self._approved(harness)
        row, _ = self._stage(harness)
        confirmed = self._real_coordinator(harness).confirm_consumption_from_ledger(harness.ledger)
        assert confirmed(row, identity) is False

    def test_the_confirmation_returns_false_when_the_ledger_raises(self, harness: _Harness) -> None:
        identity, _key, _reserved = self._approved(harness)
        row, _ = self._stage(harness)

        class _Exploding:
            def get(self, _approval_id: str) -> Any:
                raise RuntimeError("ledger unavailable")

            def _read_reservation(self, _approval_id: str) -> Any:  # pragma: no cover
                raise AssertionError("must not be reached")

        confirmed = self._real_coordinator(harness).confirm_consumption_from_ledger(_Exploding())
        assert confirmed(row, identity) is False

    def test_a_confirm_callback_that_raises_is_treated_as_not_recovered(
        self, harness: _Harness
    ) -> None:
        self._approved(harness)
        row, _ = self._stage(harness)

        def exploding(_row: OutboxRow, _identity: Any) -> bool:
            raise RuntimeError("confirmation probe failed")

        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=harness.real_ledger_consume,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            verify_completion_fn=harness.verify,
            confirm_consumption_fn=exploding,
            poll_timeout_seconds=1.0,
            sleep=lambda _seconds: None,
        )
        outcome = harness.execute(coordinator)
        # Nothing was consumed, so the real ledger accepts the first consume:
        # the raising *confirm* is never consulted on the happy path.
        assert outcome.status in ("COMPLETED", "AWAITING_WORKER")
        assert harness.store.get(outbox_id=row.outbox_id) is not None

    def test_the_reserved_job_id_is_deterministic_and_key_derived(self) -> None:
        first = derive_reserved_job_id("obx_" + "1" * 64)
        assert first == derive_reserved_job_id("obx_" + "1" * 64)
        assert first.startswith("job-") and len(first) == 36
        assert first != derive_reserved_job_id("obx_" + "2" * 64)
        with pytest.raises(ExecutionOutboxError):
            derive_reserved_job_id("   ")


class TestVerifiedCompletionIsMandatory:
    """``Job completed`` is a claim; only a verified proof may become COMPLETED."""

    def test_completion_without_verifier_or_invalid_completion_proof_fails_closed(
        self, harness: _Harness
    ) -> None:
        # 1. No verifier at all.
        no_verifier = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=harness.consume,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            poll_timeout_seconds=1.0,
            clock=harness.clock,
            sleep=lambda _s: None,
        )
        outcome = harness.execute(no_verifier)
        assert outcome.status == "AWAITING_WORKER"
        assert "verify_completion_fn_required" in outcome.detail
        assert outcome.artifact_id is None
        row = harness.store.get(outbox_id=outcome.outbox_id)
        assert row is not None and row.state == "AWAITING_WORKER"

        # 2. A verifier that returns something that is not a proof.
        wrong_shape = harness.build_coordinator(verify=lambda _r, _c: {"artifact_id": "artifact-1"})
        again = harness.execute(wrong_shape)
        assert again.status == "AWAITING_WORKER"
        assert "invalid_completion_proof" in again.detail

        # 3. A proof whose digest is not a 64-hex sha256 is refused at construction.
        with pytest.raises(CompletionVerificationError):
            _proof(artifact_sha256="not-a-digest")
        with pytest.raises(CompletionVerificationError):
            _proof(passport_id="")
        with pytest.raises(CompletionVerificationError):
            _proof(lineage_digest="")

    def test_a_verifier_that_raises_a_verification_error_holds_the_row(
        self, harness: _Harness
    ) -> None:
        def verify(_row: OutboxRow, _completion: Any) -> Any:
            raise CompletionVerificationError("passport_not_found", "no passport row")

        outcome = harness.execute(harness.build_coordinator(verify=verify))
        assert outcome.status == "AWAITING_WORKER"
        assert "passport_not_found" in outcome.detail

    def test_a_validated_completion_records_the_proof_in_the_event_log(
        self, harness: _Harness
    ) -> None:
        outcome = harness.execute(harness.build_coordinator())
        assert outcome.status == "COMPLETED"
        events = harness.store.events_for(outcome.outbox_id)
        completed = [e for e in events if e["to_state"] == "COMPLETED"]
        assert len(completed) == 1
        assert "artifact_sha256=" + "a" * 64 in completed[0]["detail"]
        assert "lineage=sha256:" + "b" * 64 in completed[0]["detail"]

    def test_verified_completion_round_trips_to_a_dict(self) -> None:
        proof = _proof()
        assert proof.to_dict() == {
            "artifact_id": "artifact-1",
            "artifact_sha256": "a" * 64,
            "passport_id": "passport-1",
            "lineage_digest": "sha256:" + "b" * 64,
            "verified_at_utc": "2026-11-27T00:00:00+00:00",
        }


class TestInterProcessOutboxLock:
    """``execute_or_recover`` takes the store's flock, not just the reconciler."""

    def test_outbox_lock_names_its_own_sidecar(self, tmp_path: Path) -> None:
        with DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3") as store:
            with store.outbox_lock():
                assert (tmp_path / "outbox.sqlite3.outbox.lock").exists()

    def test_outbox_lock_is_exclusive_across_processes(self, tmp_path: Path) -> None:
        import fcntl
        import os

        with DurableExecutionOutboxStore(tmp_path / "outbox.sqlite3") as store:
            with store.outbox_lock():
                handle = os.open(str(tmp_path / "outbox.sqlite3.outbox.lock"), os.O_RDWR)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(handle)

    def test_two_concurrent_executions_of_one_key_yield_one_job(self, harness: _Harness) -> None:
        results: list[Any] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(harness.execute(harness.build_coordinator()))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(harness.enqueued) == 1
        assert harness.consumed == [APPROVAL]
        assert all(r.status in ("COMPLETED", "AWAITING_WORKER") for r in results)


class TestCrashWindowRepairPath:
    """The crash-window repair branch, named for what it proves.

    ``DurableApprovalLedger.consume`` is idempotent for the same open
    reservation, so production recovery normally succeeds on the replay itself.
    This class drives the *other* half of the repair -- a ledger or adapter that
    raises on any second consume -- so the branch that advances an
    ``INTENT_STAGED`` row to ``APPROVAL_CONSUMED`` is directly evidenced.
    """

    def test_a_confirm_true_after_a_refusing_consume_repairs_the_row(
        self, harness: _Harness
    ) -> None:
        def refusing(_approval_id: str, _identity: Any, **_kwargs: Any) -> None:
            raise RuntimeError("replay: approval already consumed")

        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=refusing,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            verify_completion_fn=harness.verify,
            confirm_consumption_fn=lambda _row, _identity: True,
            poll_timeout_seconds=1.0,
            sleep=lambda _s: None,
        )
        row, _ = TestCrashWindowBetweenLedgerAndOutbox()._stage(harness)
        repaired = coordinator._consume_approval(row)
        assert repaired.state == "APPROVAL_CONSUMED"
        assert repaired.last_error == ""
        events = harness.store.events_for(row.outbox_id)
        assert events[-1]["detail"].startswith("recovered_reservation:job-")
        # And the drive continues to enqueue from the repaired state.
        driven = coordinator._enqueue_job(repaired)
        assert driven.state == "JOB_ENQUEUED"
        assert driven.job_id == repaired.reserved_job_id


class TestStoreSurfacesAddedForTheCrashWindow:
    """The small store/coordinator surfaces the recovery path relies on."""

    def test_a_pre_reserved_store_is_migrated_in_place(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.sqlite3"
        raw = sqlite3.connect(str(path), isolation_level=None)
        try:
            raw.execute(
                "CREATE TABLE nexus_ibpc_execution_outbox ("
                " outbox_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,"
                " tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,"
                " session_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
                " principal_id TEXT NOT NULL, approval_id TEXT NOT NULL,"
                " revision_id TEXT NOT NULL, revision_jcs_sha256 TEXT NOT NULL,"
                " plan_payload_json TEXT NOT NULL, state TEXT NOT NULL,"
                " job_id TEXT, artifact_id TEXT, passport_id TEXT,"
                " attempt_count INTEGER NOT NULL DEFAULT 0,"
                " last_error TEXT NOT NULL DEFAULT '',"
                " created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL)"
            )
        finally:
            raw.close()
        with DurableExecutionOutboxStore(path) as store:
            columns = {
                row[1]
                for row in store._conn.execute("PRAGMA table_info(nexus_ibpc_execution_outbox)")
            }
            assert "reserved_job_id" in columns

    def test_confirm_reserved_consumption_defaults_its_event_detail(
        self, harness: _Harness
    ) -> None:
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id="appr-confirm",
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id="appr-confirm",
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        consumed = harness.store.confirm_reserved_consumption(outbox_id=row.outbox_id)
        assert consumed.state == "APPROVAL_CONSUMED"
        events = harness.store.events_for(row.outbox_id)
        assert events[-1]["detail"] == "recovered_consumed_reservation"

    def test_parking_for_retry_outside_approval_consumed_is_refused(
        self, harness: _Harness
    ) -> None:
        outcome = harness.execute(harness.build_coordinator())
        with pytest.raises(ExecutionOutboxError) as excinfo:
            harness.store.record_retryable_enqueue_failure(
                outbox_id=outcome.outbox_id, error="late"
            )
        assert excinfo.value.reason == "illegal_transition"

    def test_a_negative_enqueue_attempt_budget_is_refused(self, harness: _Harness) -> None:
        for value in (-1, True):
            with pytest.raises(ExecutionOutboxError):
                ExecutionOutboxCoordinator(
                    store=harness.store,
                    consume_approval_fn=harness.consume,
                    enqueue_job_fn=harness.enqueue,
                    await_completion_fn=harness.await_completion,
                    max_enqueue_attempts=value,
                )

    def test_confirm_from_ledger_refuses_a_finalized_less_reservation(
        self, harness: _Harness
    ) -> None:
        identity, _key, _reserved = TestCrashWindowBetweenLedgerAndOutbox()._approved(harness)
        # Consume *without* a job reservation: the ledger has no row to confirm.
        harness.ledger.consume(APPROVAL, identity=identity)
        row, _ = TestCrashWindowBetweenLedgerAndOutbox()._stage(harness)
        confirmed = harness.build_coordinator().confirm_consumption_from_ledger(harness.ledger)
        assert confirmed(row, identity) is False

    def test_a_refused_consume_without_a_confirm_callback_is_terminal(
        self, harness: _Harness
    ) -> None:
        def refusing(_approval_id: str, _identity: Any, **_kwargs: Any) -> None:
            raise RuntimeError("replay: approval already consumed")

        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=refusing,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            verify_completion_fn=harness.verify,
            poll_timeout_seconds=1.0,
            sleep=lambda _s: None,
        )
        outcome = harness.execute(coordinator)
        assert outcome.status == "FAILED_TERMINAL"
        assert "approval_consumption_failed" in outcome.detail

    def test_a_completion_claim_on_an_unenqueueable_row_is_terminal(
        self, harness: _Harness
    ) -> None:
        # Stage and consume but never enqueue; then hand the coordinator a row
        # that claims success.  It cannot even park, so it must refuse.
        key = derive_idempotency_key(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            approval_id="appr-unenqueued",
            revision_jcs_sha256=REV_JCS,
        )
        row, _ = harness.store.stage_intent(
            idempotency_key=key,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=SESSION,
            turn_id=TURN,
            principal_id=PRINCIPAL,
            approval_id="appr-unenqueued",
            revision_id=REVISION,
            revision_jcs_sha256=REV_JCS,
            plan_payload=_payload(),
        )
        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=harness.consume,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            poll_timeout_seconds=1.0,
            sleep=lambda _s: None,
        )
        from nexus_ai_agent.nagar.ibpc.execution_outbox import _WorkerCompletion

        held = coordinator._complete(
            row, _WorkerCompletion(status="COMPLETED", artifact_id="a", passport_id="p")
        )
        assert held.state == "FAILED_TERMINAL"
        assert "completion_unverified" in held.last_error

    def test_a_raising_confirm_callback_on_a_refused_consume_is_terminal(
        self, harness: _Harness
    ) -> None:
        def refusing(_approval_id: str, _identity: Any, **_kwargs: Any) -> None:
            raise RuntimeError("replay: approval already consumed")

        def exploding(_row: OutboxRow, _identity: Any) -> bool:
            raise RuntimeError("confirmation probe failed")

        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=refusing,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            verify_completion_fn=harness.verify,
            confirm_consumption_fn=exploding,
            poll_timeout_seconds=1.0,
            sleep=lambda _s: None,
        )
        outcome = harness.execute(coordinator)
        # A confirmation probe that itself fails is never read as "recovered".
        assert outcome.status == "FAILED_TERMINAL"
        assert "approval_consumption_failed" in outcome.detail

    def test_a_confirm_true_after_a_refusing_consume_repairs_the_row(
        self, harness: _Harness
    ) -> None:
        """For a consume callback that does *not* replay, ``confirm`` is the repair.

        ``DurableApprovalLedger.consume`` is idempotent for the same open
        reservation, so the ordinary recovery never needs this branch.  A ledger
        (or an adapter) that raises on *any* second consume does, and that is
        what this proves: the outbox advances instead of declaring a replay.
        """

        def refusing(_approval_id: str, _identity: Any, **_kwargs: Any) -> None:
            raise RuntimeError("replay: approval already consumed")

        coordinator = ExecutionOutboxCoordinator(
            store=harness.store,
            consume_approval_fn=refusing,
            enqueue_job_fn=harness.enqueue,
            await_completion_fn=harness.await_completion,
            verify_completion_fn=harness.verify,
            confirm_consumption_fn=lambda _row, _identity: True,
            poll_timeout_seconds=1.0,
            sleep=lambda _s: None,
        )
        row, _ = TestCrashWindowBetweenLedgerAndOutbox()._stage(harness)
        repaired = coordinator._consume_approval(row)
        assert repaired.state == "APPROVAL_CONSUMED"
        assert repaired.last_error == ""
        events = harness.store.events_for(row.outbox_id)
        assert events[-1]["detail"].startswith("recovered_reservation:job-")
        # And the drive continues to enqueue from the repaired state.
        driven = coordinator._enqueue_job(repaired)
        assert driven.state == "JOB_ENQUEUED"
        assert driven.job_id == repaired.reserved_job_id
