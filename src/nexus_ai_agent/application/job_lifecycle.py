"""The one job lifecycle contract: terminality, failure classes, attempt phases.

A command does not become a successful job by returning ``{"success": True}``.
The canonical rule this module encodes:

    execution success  +  artifact verification success  =  SUCCEEDED eligibility

so ``COMPLETED`` (:data:`SUCCESS_STATUSES`) is the *only* durable status a job
may carry when the artifact it produced was proven valid, stable and
traceable. Everything else is a failure — and every failure is either
*classified* (the handler said which class it is) or *unclassified* (a crash:
terminal, never auto-retried, because a handler that never answered cannot
claim retryability either).

Durable status vs. attempt phase
--------------------------------

``JobStatus`` (the port) is what the queue persists. :class:`JobPhase` is what
one *attempt* passes through inside the worker; it is finer-grained and is
recorded in the job result (``phase_trail``) so the boundaries are evidence,
not prose:

===============  ==================  =========================================
phase            durable status      boundary this phase marks
===============  ==================  =========================================
``PENDING``      ``PENDING``         row admitted, no process owns it
``RUNNING``      ``PROCESSING``      handler entered; side effects allowed
``VERIFYING``    ``PROCESSING``      bytes exist; the canonical verifier runs
``SUCCEEDED``    ``COMPLETED``       verified bytes are the published artifact
``FAILED``       failure statuses    terminal, with the class in the result
===============  ==================  =========================================

There is deliberately **no durable ``VERIFYING`` status**: verification is a
phase of one attempt inside the handler, and the durable row only changes when
the attempt has an outcome. Modelling it as a persisted state would require the
queue to own runtime phases — a rewrite this gate is forbidden to perform and
does not need (recorded as an honest boundary, not as a feature).

Failure classes (evidence-backed, not decoration)
-------------------------------------------------

The retryable/terminal split exists because the repository *has* both kinds of
failure and the durable status could not express them: an installation without
FFmpeg (``ffmpeg_unavailable``) is retried successfully after the operator
installs it, while ``invalid_request`` can never become true by retrying.
:data:`FAILURE_CLASS_BY_CODE` is the creative lane's table; the *default* for
anything unknown or absent is :attr:`FailureClass.TERMINAL` — fail-closed: an
unclassified failure is never advertised as retryable.

Retryability is a *classification*, not a scheduler
---------------------------------------------------

The in-process queue has no automatic retry, and this gate adds none: the
resume entry points only ever touch ``pending``/``processing`` rows. The
classification tells an operator (or a future scheduler) whether a retry is
meaningful, and it is pinned by tests. What *is* enforced here is the retry
safety rule: a retry can never destroy a valid artifact
(:mod:`nexus_ai_agent.application.artifact_publication`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobStatus

#: The only durable status a job may carry when its artifact was verified.
SUCCESS_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.COMPLETED})

#: Every non-success terminal status. ``FAILED`` is the *unclassified* class
#: (a handler crash): terminal, never auto-retried, operator triage.
FAILURE_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.FAILED, JobStatus.FAILED_RETRYABLE, JobStatus.TERMINAL_FAILED}
)

#: Work a live process owns: resumed/re-scheduled, never terminal.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.PENDING, JobStatus.PROCESSING})

#: Terminal = the row will never change again without an explicit operator act.
TERMINAL_STATUSES: frozenset[JobStatus] = SUCCESS_STATUSES | FAILURE_STATUSES


def is_terminal(status: JobStatus) -> bool:
    """True when the row is finished (success or failure) forever."""
    return status in TERMINAL_STATUSES


def is_success(status: JobStatus) -> bool:
    """True only for the verified-success status."""
    return status in SUCCESS_STATUSES


def is_failure(status: JobStatus) -> bool:
    """True for every terminal failure status (classified or not)."""
    return status in FAILURE_STATUSES


class FailureClass(str, Enum):
    """Why a failure can or cannot be retried. See the module docstring."""

    RETRYABLE = "retryable"
    TERMINAL = "terminal"


#: Declared failure class → the durable status it must be persisted as.
FAILURE_STATUS_BY_CLASS: dict[FailureClass, JobStatus] = {
    FailureClass.RETRYABLE: JobStatus.FAILED_RETRYABLE,
    FailureClass.TERMINAL: JobStatus.TERMINAL_FAILED,
}


def failure_status(failure_class: FailureClass | str | None) -> JobStatus:
    """Map a declared failure class to a durable failure status (fail-closed).

    Anything unknown, missing or malformed becomes ``TERMINAL_FAILED``: an
    unclassifiable failure must never be advertised as safe to retry.
    """
    if failure_class is None:
        return JobStatus.TERMINAL_FAILED
    try:
        parsed = FailureClass(str(failure_class))
    except ValueError:
        return JobStatus.TERMINAL_FAILED
    return FAILURE_STATUS_BY_CLASS[parsed]


# ---------------------------------------------------------------------------
# result contract (the keys the queue recognises)
# ---------------------------------------------------------------------------

RESULT_SUCCESS = "success"
RESULT_FAILURE_CLASS = "failure_class"
RESULT_ERROR_CODE = "error_code"
RESULT_ERROR_DETAIL = "error_detail"
RESULT_JOB_ID = "job_id"
RESULT_TRACEABILITY = "traceability"
RESULT_PHASE_TRAIL = "phase_trail"


def declares_outcome(result: Mapping[str, object]) -> bool:
    """True when a handler result carries the explicit outcome contract.

    Only results that declare ``success`` as a real boolean are classified by
    the queue. Results without it are the repository's *legacy* handler shape
    (``pdf_extract``/``story`` return plain payload dictionaries); they keep
    completing as before — that path is an honest, documented boundary, not a
    verification claim by this gate.
    """
    return isinstance(result.get(RESULT_SUCCESS), bool)


def result_job_status(result: Mapping[str, object]) -> JobStatus | None:
    """The durable status a contract-bearing result demands (``None`` = legacy).

    ``success is True`` → ``COMPLETED`` (the handler's contract asserts the
    artifact was verified — see the worker gate). ``success is False`` → the
    declared failure class, defaulting fail-closed to ``TERMINAL_FAILED``.
    """
    if not declares_outcome(result):
        return None
    if result[RESULT_SUCCESS] is True:
        return JobStatus.COMPLETED
    return failure_status(_failure_class_of(result))


def _failure_class_of(result: Mapping[str, object]) -> str | None:
    raw = result.get(RESULT_FAILURE_CLASS)
    return str(raw) if isinstance(raw, str) else None


def stamp_job_id(result: Mapping[str, object], job_id: str) -> dict[str, object]:
    """Stamp the durable row id into a contract-bearing result.

    This closes ``job_id → payload → command_id`` inside the persisted result
    itself so :meth:`ArtifactTrace.reconstruct` needs no side channel. Legacy
    results (no outcome contract) are stored byte-identical to today.
    """
    if not declares_outcome(result):
        return dict(result)
    stamped: dict[str, object] = {**result, RESULT_JOB_ID: job_id}
    trace = stamped.get(RESULT_TRACEABILITY)
    if isinstance(trace, Mapping) and not trace.get("job_id"):
        # the artifact's own lineage record must carry the row identity too:
        # a traceability block that cannot name its job is not traceable
        stamped[RESULT_TRACEABILITY] = {**trace, "job_id": job_id}
    return stamped


# ---------------------------------------------------------------------------
# attempt phases: the explicit state machine
# ---------------------------------------------------------------------------


class JobPhase(str, Enum):
    """Phases of ONE attempt. The durable status is what the queue persists."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IllegalPhaseTransition(ValueError):
    """An attempt tried to cross a boundary the lifecycle forbids."""


#: Allowed transitions *between attempt phases*. Terminal phases have none.
ALLOWED_PHASE_TRANSITIONS: dict[JobPhase, frozenset[JobPhase]] = {
    JobPhase.PENDING: frozenset({JobPhase.RUNNING}),
    JobPhase.RUNNING: frozenset({JobPhase.VERIFYING, JobPhase.FAILED}),
    JobPhase.VERIFYING: frozenset({JobPhase.SUCCEEDED, JobPhase.FAILED}),
    JobPhase.SUCCEEDED: frozenset(),
    JobPhase.FAILED: frozenset(),
}


@dataclass(frozen=True)
class PhaseSpec:
    """The documented contract of one phase: entry, exit, effects, terminality."""

    phase: JobPhase
    durable_status: JobStatus
    entry_condition: str
    exit_condition: str
    side_effects: str
    retry_semantics: str
    terminal: bool

    @property
    def allowed(self) -> frozenset[JobPhase]:
        return ALLOWED_PHASE_TRANSITIONS[self.phase]


#: Single source of truth for the state machine's documented semantics. The
#: tests assert that :data:`ALLOWED_PHASE_TRANSITIONS` and this table agree.
PHASE_CONTRACT: dict[JobPhase, PhaseSpec] = {
    JobPhase.PENDING: PhaseSpec(
        phase=JobPhase.PENDING,
        durable_status=JobStatus.PENDING,
        entry_condition="the durable row was admitted (enqueue inserted it)",
        exit_condition="a process marks the row PROCESSING before invoking the handler",
        side_effects="none — no workspace write, no render, no artifact",
        retry_semantics="resume paths may schedule this row; nothing was produced yet",
        terminal=False,
    ),
    JobPhase.RUNNING: PhaseSpec(
        phase=JobPhase.RUNNING,
        durable_status=JobStatus.PROCESSING,
        entry_condition="the durable PROCESSING mark is on the row (RUNNING boundary)",
        exit_condition="bytes exist and are handed to the verification gate, or the attempt fails",
        side_effects="staging writes only — never the destination artifact",
        retry_semantics="a crash here is unclassified → FAILED; no success is ever claimed",
        terminal=False,
    ),
    JobPhase.VERIFYING: PhaseSpec(
        phase=JobPhase.VERIFYING,
        durable_status=JobStatus.PROCESSING,
        entry_condition="the attempt produced bytes it intends to publish",
        exit_condition="the canonical verifier accepts them and publication succeeds",
        side_effects="read-only verification + atomic publication of verified bytes",
        retry_semantics="verification failure is a typed failure; a retry may re-render, "
        "but it can never overwrite a valid destination artifact",
        terminal=False,
    ),
    JobPhase.SUCCEEDED: PhaseSpec(
        phase=JobPhase.SUCCEEDED,
        durable_status=JobStatus.COMPLETED,
        entry_condition="the published artifact verified (exists, size>0, sha256, probe, identity)",
        exit_condition="terminal — the row is COMPLETED with the verified evidence",
        side_effects="the destination artifact + its identity sidecar are the job's product",
        retry_semantics="replays are rejected by the queue key; the artifact is preserved",
        terminal=True,
    ),
    JobPhase.FAILED: PhaseSpec(
        phase=JobPhase.FAILED,
        durable_status=JobStatus.TERMINAL_FAILED,
        entry_condition="any gate refused, or verification/publication failed",
        exit_condition="terminal — the row carries FAILED_RETRYABLE / TERMINAL_FAILED / FAILED",
        side_effects="no destination artifact is published; partial bytes stay in staging",
        retry_semantics="retryability comes from the classified error code; "
        "a retry is a new attempt",
        terminal=True,
    ),
}


@dataclass
class PhaseTrack:
    """The ordered phase trail of one attempt; illegal jumps raise.

    The worker advances this object at the boundaries it must not skip, so the
    persisted result carries machine-checkable proof of the order in which
    running, verification and success happened.
    """

    phases: list[JobPhase] = field(default_factory=lambda: [JobPhase.PENDING])

    @property
    def phase(self) -> JobPhase:
        return self.phases[-1]

    def advance(self, phase: JobPhase) -> None:
        """Move to ``phase`` or raise :class:`IllegalPhaseTransition`."""
        if phase not in ALLOWED_PHASE_TRANSITIONS[self.phase]:
            raise IllegalPhaseTransition(
                f"{self.phase.value} → {phase.value} is not an allowed attempt transition"
            )
        self.phases.append(phase)

    def trail(self) -> tuple[str, ...]:
        """The phase names in order (what gets persisted as ``phase_trail``)."""
        return tuple(phase.value for phase in self.phases)


# ---------------------------------------------------------------------------
# traceability: command → job → project → operation → revision → artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactTrace:
    """The artifact's provenance, reconstructible from the persisted result.

    Chain (the brief's required path)::

        command_id → job_id → project_id → operation_id → revision
            → logical identity + spec identity + physical identity
            → sha256 → verification evidence

    ``job_id`` is not known inside the handler (the queue owns row identity);
    the queue stamps it into the result envelope
    (:func:`stamp_job_id`) and :meth:`reconstruct` fails closed when any link
    is missing — a partially traceable artifact is not a traceable one.
    """

    command_id: str
    project_id: str
    operation_id: str
    revision: int | None
    state_hash: str | None
    logical_content_identity: str
    render_spec_hash: str | None
    physical_sha256: str
    size_bytes: int
    artifact_path: str
    verification: dict[str, Any]
    job_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "operation_id": self.operation_id,
            "revision": self.revision,
            "state_hash": self.state_hash,
            "logical_content_identity": self.logical_content_identity,
            "render_spec_hash": self.render_spec_hash,
            "physical_sha256": self.physical_sha256,
            "size_bytes": self.size_bytes,
            "artifact_path": self.artifact_path,
            "verification": dict(self.verification),
        }

    @classmethod
    def reconstruct(cls, job_id: str, result: Mapping[str, object]) -> ArtifactTrace:
        """Rebuild the chain from a persisted success result (fail-closed).

        Raises :class:`ValueError` when the result is not a success envelope,
        when the trace is missing, or when any required link is absent — an
        artifact whose lineage cannot be walked is not traceable.
        """
        if result.get(RESULT_SUCCESS) is not True:
            raise ValueError("not a success result: no verified artifact to trace")
        raw = result.get(RESULT_TRACEABILITY)
        if not isinstance(raw, Mapping):
            raise ValueError("success result carries no traceability record")
        stamped = result.get(RESULT_JOB_ID)
        if stamped is not None and str(stamped) != job_id:
            raise ValueError(f"trace job_id {stamped!r} does not match row {job_id!r}")

        declared = raw.get("job_id")
        if declared is not None and str(declared) != job_id:
            raise ValueError(f"trace record job_id {declared!r} does not match row {job_id!r}")

        def required(key: str) -> Any:
            value = raw.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"traceability link {key!r} is missing")
            return value

        physical = str(required("physical_sha256"))
        if not physical.startswith("sha256:") or len(physical) != len("sha256:") + 64:
            raise ValueError(f"physical identity is not a sha256 digest: {physical!r}")
        verification = raw.get("verification")
        if not isinstance(verification, Mapping) or not verification:
            raise ValueError("traceability carries no verification evidence")
        return cls(
            command_id=str(required("command_id")),
            project_id=str(required("project_id")),
            operation_id=str(required("operation_id")),
            revision=raw.get("revision") if isinstance(raw.get("revision"), int) else None,
            state_hash=(str(raw["state_hash"]) if raw.get("state_hash") else None),
            logical_content_identity=str(required("logical_content_identity")),
            render_spec_hash=(
                str(raw["render_spec_hash"]) if raw.get("render_spec_hash") else None
            ),
            physical_sha256=physical,
            size_bytes=int(raw.get("size_bytes") or 0),
            artifact_path=str(required("artifact_path")),
            verification=dict(verification),
            job_id=job_id,
        )


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_PHASE_TRANSITIONS",
    "ArtifactTrace",
    "FAILURE_STATUS_BY_CLASS",
    "FAILURE_STATUSES",
    "IllegalPhaseTransition",
    "PHASE_CONTRACT",
    "RESULT_ERROR_CODE",
    "RESULT_ERROR_DETAIL",
    "RESULT_FAILURE_CLASS",
    "RESULT_JOB_ID",
    "RESULT_PHASE_TRAIL",
    "RESULT_SUCCESS",
    "RESULT_TRACEABILITY",
    "SUCCESS_STATUSES",
    "TERMINAL_STATUSES",
    "FailureClass",
    "JobPhase",
    "PhaseSpec",
    "PhaseTrack",
    "declares_outcome",
    "failure_status",
    "is_failure",
    "is_success",
    "is_terminal",
    "result_job_status",
    "stamp_job_id",
]
