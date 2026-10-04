"""Map committed Job-lifecycle facts onto causal records.

This is the only place where the queue's vocabulary meets the ledger's.  It
is deliberately *stateless*: every record it writes is derived from the event
it just received plus the journal it already holds, so a process that dies
and restarts resumes with the same identities (nothing is remembered in RAM
between the two processes — that is the whole point of a durable witness).

Honesty rules encoded here, each one deliberate:

* **A handler's word is never a causal fact.**  The ``execution`` record
  carries the result digest, the typed failure code and whether an error was
  reported — never the handler-claimed artifact digest, size or path.  The
  ``artifact`` node is created only from the *independently measured*
  verification block, so content identity in the graph is always the
  verifier's measurement.
* **Actor is the recorder, not a guess.**  The queue observes no end-user
  identity, so every record it may assert names the queue adapter itself as
  the asserting actor (``kind="service"``).  It does not name an end user it
  never saw, and it records no authority decision: on this tree no durable
  authorization decision exists yet, so the ``authority`` field stays empty
  and the passport reports the authority stage as *not recorded* rather than
  inventing a grant.
* **Order follows causality.**  A record's parents are always appended
  before (or by) it: request → job → attempt → execution → artifact →
  verification → receipt, with the artifact written before the verification
  record that names it.
"""

from __future__ import annotations

from nexus_ai_agent.application.ports.job_lifecycle_observer import (
    ExecutionFinished,
    JobEnqueued,
    JobReservationRejected,
    JobReserved,
    JobSettled,
    VerificationFinished,
)
from nexus_ai_agent.causal.journal import CausalJournal
from nexus_ai_agent.causal.models import (
    ActorRef,
    NodeRef,
    Stage,
    artifact_node_id,
    attempt_node_id,
    digest_of,
    digest_of_text,
    execution_node_id,
    job_node_id,
    receipt_node_id,
    request_node_id,
    verification_node_id,
)

#: The default asserting actor for records the queue witnesses.
DEFAULT_RECORDER_ID = "in_process_job_queue"


class CausalObserver:
    """Translate queue events into journal records (see module docstring)."""

    def __init__(self, journal: CausalJournal, *, recorder_id: str = DEFAULT_RECORDER_ID) -> None:
        self._journal = journal
        self._actor = ActorRef(kind="service", actor_id=recorder_id)

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------
    def on_enqueued(self, event: JobEnqueued) -> None:
        request = NodeRef(
            stage=Stage.REQUEST,
            node_id=request_node_id(event.job_type, event.idempotency_key),
            role="logical_request",
        )
        job = NodeRef(stage=Stage.JOB, node_id=job_node_id(event.job_id), role="job")
        idempotency_digest = digest_of_text(event.idempotency_key)
        self._journal.append(
            stage=Stage.REQUEST,
            subject=request,
            actor=self._actor,
            facts={
                "job_type": event.job_type,
                "idempotency_digest": idempotency_digest,
                "payload_digest": digest_of(event.payload),
            },
        )
        self._journal.append(
            stage=Stage.JOB,
            subject=job,
            actor=self._actor,
            parents=(request,),
            facts={
                "job_type": event.job_type,
                "idempotency_digest": idempotency_digest,
                "payload_digest": digest_of(event.payload),
                "created": event.created,
                "payload_conflict": event.payload_conflict,
            },
        )

    def on_reserved(self, event: JobReserved) -> None:
        self._journal.append(
            stage=Stage.ATTEMPT,
            subject=NodeRef(
                stage=Stage.ATTEMPT,
                node_id=attempt_node_id(event.job_id, event.attempt),
                role="reservation",
            ),
            actor=self._actor,
            parents=(NodeRef(stage=Stage.JOB, node_id=job_node_id(event.job_id), role="job"),),
            facts={
                "job_type": event.job_type,
                "attempt": event.attempt,
                "reservation": "granted",
            },
        )

    def on_reservation_rejected(self, event: JobReservationRejected) -> None:
        """Record that a reservation CAS was refused (a stale worker's face).

        No fenced token was minted for the rejected attempt, so the record
        names the job and says what happened — it does not invent an attempt
        number the queue never minted.  Repeated refusals collapse onto one
        record by the idempotency rule (the queue's own logs carry the count).
        """
        self._journal.append(
            stage=Stage.ATTEMPT,
            subject=NodeRef(
                stage=Stage.ATTEMPT,
                node_id=job_node_id(event.job_id),
                role="reservation_rejected",
            ),
            actor=self._actor,
            parents=(NodeRef(stage=Stage.JOB, node_id=job_node_id(event.job_id), role="job"),),
            facts={"job_type": event.job_type, "reservation": "rejected"},
        )

    def on_execution_finished(self, event: ExecutionFinished) -> None:
        success = event.typed_failure_code is None and event.error is None
        self._journal.append(
            stage=Stage.EXECUTION,
            subject=NodeRef(
                stage=Stage.EXECUTION,
                node_id=execution_node_id(event.job_id, event.attempt),
                role="execution",
            ),
            actor=self._actor,
            parents=(
                NodeRef(
                    stage=Stage.ATTEMPT,
                    node_id=attempt_node_id(event.job_id, event.attempt),
                    role="reservation",
                ),
            ),
            facts={
                "job_type": event.job_type,
                "attempt": event.attempt,
                "result_digest": digest_of(event.result) if event.result is not None else None,
                "typed_failure_code": event.typed_failure_code,
                "error_present": event.error is not None,
                "success": success,
            },
        )

    def on_verification_finished(self, event: VerificationFinished) -> None:
        execution = NodeRef(
            stage=Stage.EXECUTION,
            node_id=execution_node_id(event.job_id, event.attempt),
            role="execution",
        )
        measured = _measured_identity(event.block)
        parents: tuple[NodeRef, ...] = (execution,)
        if event.ok and measured is not None:
            digest, size, path = measured
            artifact = NodeRef(
                stage=Stage.ARTIFACT, node_id=artifact_node_id(digest), role="output"
            )
            self._journal.append(
                stage=Stage.ARTIFACT,
                subject=artifact,
                actor=self._actor,
                parents=(execution,),
                facts={
                    "artifact_digest": digest,
                    "artifact_size_bytes": size,
                    "artifact_path": path,
                    "operation": _operation_of(event.block),
                    "project_id": _project_of(event.block),
                },
            )
            parents = (artifact, execution)
        self._journal.append(
            stage=Stage.VERIFICATION,
            subject=NodeRef(
                stage=Stage.VERIFICATION,
                node_id=verification_node_id(event.job_id, event.attempt),
                role="independent_verification",
            ),
            actor=self._actor,
            parents=parents,
            facts={
                "attempt": event.attempt,
                "verdict": "accepted" if event.ok else "refused",
                "reason_code": event.reason_code,
                "evidence_digest": digest_of(event.block),
                "measured_digest": measured[0] if measured is not None else None,
                "measured_size_bytes": measured[1] if measured is not None else None,
                "probe": _probe_of(event.block),
                "published": event.published,
            },
        )

    def on_settled(self, event: JobSettled) -> None:
        parents = self._settlement_parents(event)
        verification_status = None
        if event.result is not None:
            block = event.result.get("artifact_verification")
            if isinstance(block, dict):
                status = block.get("status")
                verification_status = str(status) if isinstance(status, str) else None
        self._journal.append(
            stage=Stage.RECEIPT,
            subject=NodeRef(
                stage=Stage.RECEIPT,
                node_id=receipt_node_id(event.job_id, event.attempt),
                role="terminal",
            ),
            actor=self._actor,
            parents=parents,
            facts={
                "attempt": event.attempt,
                "status": event.status,
                "result_digest": digest_of(event.result) if event.result is not None else None,
                "verification_status": verification_status,
                "error_present": event.error is not None,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _settlement_parents(self, event: JobSettled) -> tuple[NodeRef, ...]:
        """Durable lookup of this attempt's links (never process memory)."""
        if event.attempt < 1:
            return (NodeRef(stage=Stage.JOB, node_id=job_node_id(event.job_id), role="job"),)
        parents: list[NodeRef] = []
        execution_id = execution_node_id(event.job_id, event.attempt)
        artifact_records = [
            record
            for record in self._journal.records()
            if record.stage is Stage.ARTIFACT
            and any(parent.node_id == execution_id for parent in record.parents)
        ]
        if artifact_records:
            parents.append(artifact_records[-1].subject)
        verification = self._journal.records_for(verification_node_id(event.job_id, event.attempt))
        if verification:
            parents.append(verification[-1].subject)
        parents.append(
            NodeRef(
                stage=Stage.EXECUTION,
                node_id=execution_id,
                role="execution",
            )
        )
        return tuple(parents)


#: Keys the verifier's block uses for the measurement (job-layer vocabulary).
def _measured_identity(block: dict[str, object]) -> tuple[str, int | None, str] | None:
    physical = block.get("physical_identity")
    if not isinstance(physical, dict):
        return None
    digest = physical.get("sha256")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    size = physical.get("size_bytes")
    path = physical.get("path")
    return (
        digest,
        int(size) if isinstance(size, int) else None,
        str(path) if isinstance(path, str) else "",
    )


def _operation_of(block: dict[str, object]) -> str | None:
    spec = block.get("spec_identity")
    if isinstance(spec, dict):
        operation = spec.get("operation")
        if isinstance(operation, str):
            return operation
    return None


def _project_of(block: dict[str, object]) -> str | None:
    logical = block.get("logical_identity")
    if isinstance(logical, dict):
        project = logical.get("project_id")
        if isinstance(project, str):
            return project
    return None


def _probe_of(block: dict[str, object]) -> dict[str, object] | None:
    probe = block.get("probe")
    if isinstance(probe, dict):
        return {str(key): value for key, value in probe.items()}
    return None


__all__ = ["DEFAULT_RECORDER_ID", "CausalObserver"]
