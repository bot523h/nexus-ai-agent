"""Record a cognition decision as durable provenance — one observation.

The post-#153 contract requires the cognition decision to be *explainable* in
the causal ledger without ever becoming authority.  This module is the smallest
thing that satisfies it:

* it appends to the **existing** :class:`~nexus_ai_agent.provenance.journal.CausalJournal`
  (no parallel ledger), using only :class:`~nexus_ai_agent.provenance.models.EventKind`
  values — a *refusal* is an ``event_conflict`` observation (append-only, never
  a lifecycle transition), an *accepted proposal* is a ``job_reserved``
  transition keyed exactly-once on the real attempt;
* it records **only** durable facts — producer name, chosen operation, refusal
  reason, the execution identities actually observed — never the raw prompt or
  the raw model output (the existing provenance contract permits no content);
* **failure degrades evidence, never authority**: a journal append error is
  swallowed here, so it can never change whether a command executed.  The
  command bus remains the authority; this is its witness, not its judge.

The observation is addressed to a real authoritative identity: the job the
execution will be recorded under (its idempotency-derived job id) — never an
invented one.  When no execution identity exists (a refusal), the event is an
observation and needs none.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from nexus_ai_agent.provenance.models import CausalEvent, EventKind


class CognitionDecision(str, Enum):
    """The stable, durable outcomes a cognition decision can record."""

    PROPOSAL_ACCEPTED = "proposal_accepted"
    REFUSED = "refused"


#: The job type every cognition-observed execution carries (non-transition
#: facts about a free-text intent, distinct from the render job types).
COGNITION_JOB_TYPE = "cognition_intent"


def _producer_slug(producer_class: str) -> str:
    return producer_class.rsplit(".", 1)[-1][:64]


def cognition_decision_event(
    *,
    decision: CognitionDecision,
    producer_class: str,
    job_id: str,
    idempotency_key: str,
    operation: str | None = None,
    refusal_reason: str | None = None,
    execution_identity: dict[str, Any] | None = None,
) -> CausalEvent:
    """Build the observation event for one cognition decision.

    * an accepted proposal is a ``job_reserved`` transition (attempt 0) — the
      exactly-once envelope the journal already dedupes on ``(kind, job,
      attempt)``, so a redelivered identical decision appends nothing and a
      *different* decision for the same key quarantines honestly;
    * a refusal is an ``event_conflict`` observation — append-only, never a
      lifecycle transition, so it can never advance any job's state machine.

    Only durable, non-content facts are recorded (producer class name, chosen
    operation, refusal reason code, and — for an accepted proposal — the real
    execution identity the bus returned).  No prompt, no model text, no secret.
    """
    detail: dict[str, Any] = {
        "decision": decision.value,
        "producer": _producer_slug(producer_class),
    }
    if operation is not None:
        detail["operation"] = operation
    if refusal_reason is not None:
        detail["refusal_reason"] = refusal_reason
    if execution_identity is not None:
        detail["execution_identity"] = execution_identity
    is_accept = decision is CognitionDecision.PROPOSAL_ACCEPTED
    return CausalEvent(
        kind=EventKind.JOB_RESERVED if is_accept else EventKind.EVENT_CONFLICT,
        job_id=job_id,
        job_type=COGNITION_JOB_TYPE,
        idempotency_key=idempotency_key,
        attempt=0 if is_accept else None,
        detail=detail,
    )


def record_cognition_decision(
    journal: Any | None,
    *,
    decision: CognitionDecision,
    producer_class: str,
    job_id: str,
    idempotency_key: str,
    operation: str | None = None,
    refusal_reason: str | None = None,
    execution_identity: dict[str, Any] | None = None,
) -> bool:
    """Append the cognition observation; return ``True`` iff recorded.

    A missing journal (evidence plane degraded) or any append error returns
    ``False`` and is swallowed: recording is *evidence*, never a gate on
    execution.  Callers must not branch their authority on this return value.
    """
    if journal is None:
        return False
    try:
        event = cognition_decision_event(
            decision=decision,
            producer_class=producer_class,
            job_id=job_id,
            idempotency_key=idempotency_key,
            operation=operation,
            refusal_reason=refusal_reason,
            execution_identity=execution_identity,
        )
        journal.append(event)
        return True
    except Exception:  # noqa: BLE001 — evidence degradation must not break execution
        return False


__all__ = [
    "COGNITION_JOB_TYPE",
    "CognitionDecision",
    "cognition_decision_event",
    "record_cognition_decision",
]
