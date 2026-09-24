"""The canonical Job lifecycle contract (task-178, failure taxonomy task-181).

Chain (every step explicit — none implicit):

    Command
      → Job                        (durable row, idempotency-keyed)
      → Runtime Execution          (handler; first side effect only after
                                    the side-effect boundary below)
      → Artifact Verification      (independent, queue-owned re-measurement)
      → Publication                (atomic rename of the staged artifact, then
                                    a re-probe of the published bytes — for
                                    lanes with a destination outside the job
                                    workspace; workspace-scoped artifacts are
                                    published at delivery, after verification)
      → Result                     (verified facts or typed failure reason)

States
------
The persisted enum (``application.ports.job_queue.JobStatus``) is
``pending / processing / verifying / completed / failed_retryable /
failed_terminal``. The canonical names of this contract are aliases — the
historical persisted spellings of the non-failure states are kept for
compatibility (no reasonless rename):

=================  ======================  ==================================
Canonical          Persisted enum          Meaning
=================  ======================  ==================================
``PENDING``        ``PENDING``             durable, not yet reserved
``RUNNING``        ``PROCESSING``          reserved by this process; execution
                                           may touch the world only after the
                                           side-effect boundary
``VERIFYING``      ``VERIFYING``           execution returned; artifact is
                                           being independently re-measured
``SUCCEEDED``      ``COMPLETED``           execution + publication +
                                           verification all ok
``FAILED_RETRYABLE``  ``FAILED_RETRYABLE`` classified retry-eligible (the
                                           world can change); terminal as
                                           implemented — no scheduler
``FAILED_TERMINAL``   ``FAILED_TERMINAL``  identical request must fail again;
                                           operator/user action required
=================  ======================  ==================================

Rows written by pre-task-181 code as ``"failed"`` read back as
``FAILED_TERMINAL`` via :func:`parse_job_status` (the old contract had one
undifferentiated terminal failure).

Success rule (Q3): execution success ≠ job success.

    execution success + artifact verification success = SUCCESS eligibility

Failure rule (task-181, GAP-A): a typed user failure
(``{"success": False, "error_code": ...}``) is a FAILURE of the job.  It
never reaches ``COMPLETED`` — it is classified by
``jobs.failure_semantics`` and persisted as ``FAILED_RETRYABLE`` or
``FAILED_TERMINAL`` with the error spelling ``typed_failure:<code>``.

Every transition row in :data:`TRANSITIONS` names its owner and the
invariant that must hold when the edge is taken; the queue adapter enforces
the edges with guarded, status-conditioned UPDATEs (a compare-and-set, never
a blind write).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from nexus_ai_agent.application.ports.job_queue import JobStatus

#: Canonical aliases. ``RUNNING`` and ``SUCCEEDED`` are how this contract
#: spells the two historical persisted values; the enum values themselves do
#: not move (compatibility — see module docstring).
RUNNING: Final[JobStatus] = JobStatus.PROCESSING
SUCCEEDED: Final[JobStatus] = JobStatus.COMPLETED

#: Legacy persisted spellings (pre-task-181 sidecar rows) → canonical states.
_LEGACY_STATUS_ALIASES: Final[dict[str, JobStatus]] = {
    "failed": JobStatus.FAILED_TERMINAL,
    "running": JobStatus.PROCESSING,
    "succeeded": JobStatus.COMPLETED,
}

#: The pre-flight list. NO EXECUTION SIDE-EFFECT is allowed before every one
#: of these has passed; exactly one boundary exists, and the first lawful
#: side effect is a write to a *staging* path (never the final destination —
#: the runtime publishes atomically from ``.part`` staging).
PREFLIGHT_REQUIREMENTS: Final[tuple[str, ...]] = (
    "authorization",  # surface/HMAC gate + payload schema validation
    "capability",  # operation ∈ registry allow-list (packs CapabilityRegistry)
    "references",  # input media contained in its workspace, readable
    "idempotency",  # durable UNIQUE key collapse at enqueue (job identity)
    "revision/precondition",  # guarded status CAS: row must be in the
    #                            expected source state to move forward
    "job reservation",  # PENDING→RUNNING compare-and-set owns the execution
)

SIDE_EFFECT_BOUNDARY: Final[str] = (
    "SIDE-EFFECT ALLOWED only after: "
    + ", ".join(PREFLIGHT_REQUIREMENTS)
    + ". First lawful side effect = staging write; final destination is "
    "published by atomic rename only."
)


@dataclass(frozen=True)
class TransitionRule:
    """One legal edge: who may take it and what must be true when taken."""

    owner: str
    invariant: str


#: The canonical transition matrix. Anything not listed is illegal and the
#: adapter's guarded UPDATEs make it impossible to persist.
#:
#: RESERVED, NOT IMPLEMENTED (fail-closed): ``(FAILED_RETRYABLE, PENDING)`` —
#: the retry edge a future scheduler would take.  It is deliberately absent
#: from this matrix: with no scheduler in the repository, nothing may move a
#: failure state, and ``assert_transition`` refuses the edge.
TRANSITIONS: Final[dict[tuple[JobStatus, JobStatus], TransitionRule]] = {
    (JobStatus.PENDING, JobStatus.PROCESSING): TransitionRule(
        owner="queue (reservation CAS)",
        invariant="row exists, not terminal; started_at recorded; at most one "
        "live execution per row per process (task table dedupes)",
    ),
    (JobStatus.PENDING, JobStatus.FAILED_RETRYABLE): TransitionRule(
        owner="queue (claim-time structural failure)",
        invariant="failure discovered before any reservation is useful "
        "(e.g. no handler for the job type) and classified RETRYABLE (a "
        "later deploy can make the identical job succeed); typed error "
        "persisted; no side effect has occurred",
    ),
    (JobStatus.PENDING, JobStatus.FAILED_TERMINAL): TransitionRule(
        owner="queue (claim-time structural failure)",
        invariant="failure discovered before any reservation is useful and "
        "classified TERMINAL (e.g. no handler — a deploy must change); typed "
        "error persisted; no side effect has occurred",
    ),
    (JobStatus.PROCESSING, JobStatus.PROCESSING): TransitionRule(
        owner="queue (resume reclaim)",
        invariant="a previous process died mid-execution; row is re-claimed "
        "by resume_pending and attempt increments",
    ),
    (JobStatus.PROCESSING, JobStatus.VERIFYING): TransitionRule(
        owner="queue",
        invariant="handler returned a dict (execution finished); nothing is "
        "trusted yet — the result only becomes the job result after "
        "verification (and, where a publisher is registered, after atomic "
        "publication + re-probe)",
    ),
    (JobStatus.VERIFYING, JobStatus.COMPLETED): TransitionRule(
        owner="queue",
        invariant="artifact verification succeeded (and, for published "
        "lanes, the staged artifact was atomically published and re-probed); "
        "the verified facts are persisted inside the result under the "
        "queue-owned 'artifact_verification' key",
    ),
    (JobStatus.VERIFYING, JobStatus.FAILED_RETRYABLE): TransitionRule(
        owner="queue",
        invariant="verification refused or the verifier itself raised "
        "(fail-closed), classified RETRYABLE (artifact damage is what a "
        "clean re-execution is for); typed 'verification_failed:<code>' "
        "error persisted; staged temps retracted; no result payload of a "
        "success claim is stored",
    ),
    (JobStatus.VERIFYING, JobStatus.FAILED_TERMINAL): TransitionRule(
        owner="queue",
        invariant="verification refused with a deterministic handler defect "
        "(no claim / malformed sha / mis-routed publication / typed user "
        "failure reached verification), classified TERMINAL; error "
        "persisted; staged temps retracted",
    ),
    (JobStatus.PROCESSING, JobStatus.FAILED_RETRYABLE): TransitionRule(
        owner="queue (fail-closed conversion)",
        invariant="typed user failure (success=False dialect) or an "
        "exception escaped the handler and classification returned "
        "RETRYABLE; runtime failure is NEVER converted into success; typed "
        "error persisted (typed_failure:<code> or the exception text)",
    ),
    (JobStatus.PROCESSING, JobStatus.FAILED_TERMINAL): TransitionRule(
        owner="queue (fail-closed conversion)",
        invariant="typed user failure or handler exception classified "
        "TERMINAL (invalid input / unsupported operation / permission / "
        "render_failed / contract violation); error persisted",
    ),
    (JobStatus.PROCESSING, JobStatus.PENDING): TransitionRule(
        owner="queue (cancellation / shutdown)",
        invariant="process-lifecycle event, not a business failure; row "
        "stays recoverable; started_at cleared",
    ),
    (JobStatus.VERIFYING, JobStatus.PENDING): TransitionRule(
        owner="queue (cancellation / shutdown)",
        invariant="verification is read-only, so abandoning it is safe; the "
        "next resume re-runs verification from scratch",
    ),
}

_TERMINAL: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
)

FAILURE_STATES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
)


def parse_job_status(raw: str) -> JobStatus:
    """Parse a persisted status, mapping legacy spellings deterministically.

    Pre-task-181 rows may carry ``"failed"`` (one undifferentiated terminal
    failure); they read back as ``FAILED_TERMINAL`` — the old contract had no
    retry classification, so terminal is the conservative, honest mapping.
    Unknown spellings raise (fail-closed; never silently coerce).
    """
    try:
        return JobStatus(raw)
    except ValueError:
        aliased = _LEGACY_STATUS_ALIASES.get(raw)
        if aliased is not None:
            return aliased
        raise


def is_terminal(status: JobStatus) -> bool:
    """Terminal states have no outgoing edges — enforced by guarded UPDATEs.

    Both failure states are terminal *as implemented*: no scheduler exists to
    take the reserved ``failed_retryable → pending`` edge.  ``FAILED_RETRYABLE``
    records retry-eligibility for that future edge — it never implies a retry
    happened.
    """
    return status in _TERMINAL


def is_failure(status: JobStatus) -> bool:
    """True for the two durable failure states (never ``COMPLETED``)."""
    return status in FAILURE_STATES


def is_legal_transition(source: JobStatus, target: JobStatus) -> bool:
    """True iff (source, target) is an edge of :data:`TRANSITIONS`."""
    return (source, target) in TRANSITIONS


def assert_transition(source: JobStatus, target: JobStatus) -> None:
    """Raise ``ValueError`` for an edge outside the canonical matrix."""
    if not is_legal_transition(source, target):
        raise ValueError(f"illegal job transition: {source.value} → {target.value}")


@dataclass(frozen=True)
class JobResult:
    """The Result end of the canonical chain — minimum sufficient fields.

    Built from the durable row plus the (verified) handler result. Every
    ``SUCCEEDED`` job's result must be traceable back through this object to
    the command that started it.  Failure jobs carry ``failure_reason``
    (``typed_failure:<code>`` / ``verification_failed:<code>`` / exception
    text) and the durable ``execution_status`` is always one of the two
    failure states — a failure result never reads as success here either.
    """

    command_id: str
    job_id: str
    project_id: str
    operation_id: str
    attempt: int
    execution_status: str
    verification_status: str
    logical_identity: dict[str, object]
    spec_identity: dict[str, object]
    physical_identity: dict[str, object]
    sha256: str | None
    size_bytes: int | None
    probe: dict[str, object] | None
    failure_reason: str | None

    @classmethod
    def from_parts(
        cls,
        *,
        job_id: str,
        payload: dict[str, object],
        result: dict[str, object] | None,
        attempt: int,
        execution_status: str,
        verification: dict[str, object] | None,
        error: str | None,
    ) -> JobResult:
        """Assemble the chain from durable facts only (never assumed).

        ``verification`` is the queue-persisted ``artifact_verification``
        block; the three identities inside it are the ones the verifier
        actually measured, with the payload's idempotency key as the logical
        root (``project_id = shot-<idempotency key>``, the render chain's
        own convention).
        """
        verification = verification or {}
        logical_raw = verification.get("logical_identity")
        spec_raw = verification.get("spec_identity")
        physical_raw = verification.get("physical_identity")
        logical = dict(logical_raw) if isinstance(logical_raw, dict) else {}
        spec = dict(spec_raw) if isinstance(spec_raw, dict) else {}
        physical = dict(physical_raw) if isinstance(physical_raw, dict) else {}
        key = str(payload.get("idempotency_key") or "")
        sha_raw = verification.get("sha256")
        size_raw = verification.get("size_bytes")
        probe_raw = verification.get("probe")
        return cls(
            command_id=f"cmd-{key}-{payload.get('operation', '')}",
            job_id=job_id,
            project_id=str(logical.get("project_id") or f"shot-{key}"),
            operation_id=str(spec.get("operation") or payload.get("operation") or ""),
            attempt=attempt,
            execution_status=execution_status,
            verification_status=str(verification.get("status") or "not_applicable"),
            logical_identity=logical,
            spec_identity=spec,
            physical_identity=physical,
            sha256=str(sha_raw) if isinstance(sha_raw, str) and sha_raw else None,
            size_bytes=int(size_raw) if isinstance(size_raw, int) else None,
            probe=dict(probe_raw) if isinstance(probe_raw, dict) else None,
            failure_reason=error,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "execution_status": self.execution_status,
            "verification_status": self.verification_status,
            "logical_identity": self.logical_identity,
            "spec_identity": self.spec_identity,
            "physical_identity": self.physical_identity,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "probe": self.probe,
            "failure_reason": self.failure_reason,
        }


__all__ = [
    "FAILURE_STATES",
    "PREFLIGHT_REQUIREMENTS",
    "RUNNING",
    "SIDE_EFFECT_BOUNDARY",
    "SUCCEEDED",
    "JobResult",
    "TRANSITIONS",
    "TransitionRule",
    "assert_transition",
    "is_failure",
    "is_legal_transition",
    "is_terminal",
    "parse_job_status",
]
