"""Crash-safe approval-to-enqueue outbox and non-destructive poll timeout.

Two failure modes this module removes, both of which produced *silent* wrong
answers before:

1. **The crash window between approval consumption and enqueue.** A single-use
   :class:`~nexus_ai_agent.nagar.ibpc.models.ApprovalBinding` can be consumed
   exactly once.  If the process consumes it and dies before
   ``job_queue.enqueue()`` commits, the approval is permanently ``CONSUMED``
   while no job exists — the turn can never execute, and no retry is legal
   because re-consuming is a ``replay`` refusal.
2. **The destructive poll timeout.** A short caller timeout while the worker is
   still legitimately running used to mark the turn ``FAILED``.  The work then
   completes anyway, so the durable record contradicts reality, and no
   reconciliation can repair it.

:class:`DurableExecutionOutboxStore` is a SQLite WAL intent log keyed by a
deterministic ``idempotency_key`` over
``(tenant, project, session, turn, approval, revision digest)``.  Every state
transition is a ``BEGIN IMMEDIATE`` write, so the intent and the queue-side
effect can never be observed apart:

``INTENT_STAGED`` → ``APPROVAL_CONSUMED`` → ``JOB_ENQUEUED`` →
``AWAITING_WORKER`` → ``COMPLETED`` / ``FAILED_TERMINAL``

:class:`ExecutionOutboxCoordinator.execute_or_recover` resumes from whatever
state a previous (possibly crashed) process left behind — it never re-consumes
an ``APPROVAL_CONSUMED`` approval, never double-enqueues a ``JOB_ENQUEUED`` job,
and turns a poll timeout into ``AWAITING_WORKER`` (``can_resume=True``) instead
of a failure.  ``reconcile_in_flight`` drives every in-flight row forward, and
takes a cross-process ``fcntl.flock`` so two reconcilers never work the same
row concurrently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from nexus_ai_agent.nagar.ibpc.canonical import canonical_json

__all__ = [
    "ApprovalConsumptionError",
    "CompletionVerificationError",
    "DurableExecutionOutboxStore",
    "ExecutionOutboxConflictError",
    "ExecutionOutboxCoordinator",
    "ExecutionOutboxError",
    "ExecutionOutboxOutcome",
    "ExecutionOutboxState",
    "ExecutionOutboxTerminalError",
    "OutboxRow",
    "PollTimeoutError",
    "VerifiedExecutionCompletion",
    "approval_identity_for",
    "derive_idempotency_key",
    "derive_reserved_job_id",
    "utc_now",
]

_T = TypeVar("_T")

IN_FLIGHT_STATES: frozenset[str] = frozenset(
    {
        "INTENT_STAGED",
        "APPROVAL_CONSUMED",
        "JOB_ENQUEUED",
        "AWAITING_WORKER",
    }
)


def utc_now() -> str:
    """One spelling of "now" for every durable timestamp in this module."""
    return datetime.now(timezone.utc).isoformat()


class ExecutionOutboxError(ValueError):
    """Typed, fail-closed refusal from the execution outbox."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class ExecutionOutboxConflictError(ExecutionOutboxError):
    """The same ``idempotency_key`` arrived with a different payload."""


class ApprovalConsumptionError(ExecutionOutboxError):
    """The approval could not be (or must not be re-)consumed."""


class PollTimeoutError(ExecutionOutboxError):
    """The worker did not finish inside the poll budget (never a turn failure)."""


class ExecutionOutboxTerminalError(ExecutionOutboxError):
    """The job or its verification failed terminally."""


class CompletionVerificationError(ExecutionOutboxError):
    """A worker's self-reported completion could not be independently verified.

    Raised (and honoured by the coordinator) when a job reports success but the
    completion proof is missing, unverifiable, or malformed.  "The job said it
    finished" is a claim; only a verified artifact + passport is evidence.
    """


class ExecutionOutboxState(str, Enum):
    """The durable lifecycle of one approval-to-execution intent."""

    #: Persisted *before* the single-use approval is consumed.
    INTENT_STAGED = "INTENT_STAGED"
    #: The single-use approval is consumed; ready to enqueue.
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    #: A durable ``job_id`` exists in the queue.
    JOB_ENQUEUED = "JOB_ENQUEUED"
    #: The poll budget expired while the job is still PENDING/RUNNING.
    #: NOT a failure: the worker is active and the row can be reconciled.
    AWAITING_WORKER = "AWAITING_WORKER"
    #: The worker finished and the artifact/provenance verification passed.
    COMPLETED = "COMPLETED"
    #: An unrecoverable execution or verification failure.
    FAILED_TERMINAL = "FAILED_TERMINAL"


def derive_idempotency_key(
    *,
    tenant_id: str,
    project_id: str,
    session_id: str,
    turn_id: str,
    approval_id: str,
    revision_jcs_sha256: str,
) -> str:
    """Deterministic key for one (turn, approval, revision) execution intent.

    ``NUL``-joined so no field can be shifted into its neighbour, and the same
    six inputs always yield the same key in every process.
    """
    for field_name, value in (
        ("tenant_id", tenant_id),
        ("project_id", project_id),
        ("session_id", session_id),
        ("turn_id", turn_id),
        ("approval_id", approval_id),
        ("revision_jcs_sha256", revision_jcs_sha256),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ExecutionOutboxError(
                "invalid_idempotency_field", f"{field_name} must be a non-empty string"
            )
    material = "\0".join(
        (tenant_id, project_id, session_id, turn_id, approval_id, revision_jcs_sha256)
    ).encode("utf-8")
    return "obx_" + hashlib.sha256(material).hexdigest()


def derive_reserved_job_id(idempotency_key: str) -> str:
    """Deterministic job id reserved for one intent, derived from its key.

    The reservation must exist **before** the single-use approval is consumed:
    that is what makes a crash between ``approval.py``'s commit and the outbox's
    own ``APPROVAL_CONSUMED`` commit recoverable.  The ledger can then be asked
    "was this approval consumed *for this exact job*?" and answer yes, instead of
    the recovery being misread as a replay attack.

    Deterministic (never ``uuid4``) so a restarted process recomputes the same
    reservation from the durable ``idempotency_key`` alone — no extra column and
    no extra coordination is needed to remember it across a crash.
    """
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ExecutionOutboxError(
            "invalid_idempotency_key", "idempotency_key must be a non-empty string"
        )
    return "job-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def approval_identity_for(row: OutboxRow) -> Any:
    """Build the ``ApprovalIdentity`` one outbox row presents to the ledger.

    Kept as a helper (rather than inlined at the call site) so the *same*
    identity is presented on the first consumption and on every later
    reconciliation — the ledger refuses a changed identity with ``replay``.
    """
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity

    payload = row.plan_payload
    identity = payload.get("approval_identity")
    if not isinstance(identity, Mapping):
        raise ExecutionOutboxError(
            "missing_approval_identity",
            f"row {row.outbox_id!r} carries no approval_identity to consume with",
        )
    return ApprovalIdentity(
        principal_id=str(identity["principal_id"]),
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        sequence_number=int(identity["sequence_number"]),
        source_revision=int(identity["source_revision"]),
        source_asset_digest=str(identity["source_asset_digest"]),
        ast_digest=str(identity["ast_digest"]),
        plan_digest=str(identity["plan_digest"]),
        preview_digest=str(identity["preview_digest"]),
        revision_id=str(identity["revision_id"]) if identity.get("revision_id") else None,
        scope_signature=(
            str(identity["scope_signature"]) if identity.get("scope_signature") else None
        ),
        expires_at_utc=(
            str(identity["expires_at_utc"]) if identity.get("expires_at_utc") else None
        ),
    )


#: Payload keys that must agree with the durable row they are staged on.  A
#: payload that names a *different* tenant/project/session/turn/revision would
#: execute one principal's approval against another scope, so it is refused
#: before anything is written.
_SCOPE_KEYS: tuple[str, ...] = (
    "tenant_id",
    "project_id",
    "session_id",
    "turn_id",
    "revision_jcs_sha256",
)


def _assert_payload_scope(
    payload: Mapping[str, Any],
    *,
    tenant_id: str,
    project_id: str,
    session_id: str,
    turn_id: str,
    revision_jcs_sha256: str,
) -> None:
    """Refuse a plan payload that disagrees with the row's own scope."""
    declared = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "revision_jcs_sha256": revision_jcs_sha256,
    }
    for key in _SCOPE_KEYS:
        if key not in payload:
            continue
        actual = payload[key]
        if not isinstance(actual, str) or not hmac.compare_digest(actual, declared[key]):
            raise ExecutionOutboxConflictError(
                "payload_scope_mismatch",
                f"payload {key}={actual!r} disagrees with the staged intent {declared[key]!r}",
            )


@dataclass(frozen=True)
class OutboxRow:
    """One durable outbox record (a snapshot read, never a live handle)."""

    outbox_id: str
    idempotency_key: str
    tenant_id: str
    project_id: str
    session_id: str
    turn_id: str
    principal_id: str
    approval_id: str
    revision_id: str
    revision_jcs_sha256: str
    plan_payload_json: str
    state: str
    job_id: str | None
    artifact_id: str | None
    passport_id: str | None
    attempt_count: int
    last_error: str
    created_at_utc: str
    updated_at_utc: str
    #: The deterministic job id reserved for this intent *before* the approval
    #: was consumed, so a crash in that window is recoverable rather than a
    #: false "replay" refusal.
    reserved_job_id: str = ""

    @property
    def plan_payload(self) -> dict[str, Any]:
        """The frozen plan payload (parsed on demand; never mutated)."""
        parsed = json.loads(self.plan_payload_json)
        if not isinstance(parsed, dict):
            raise ExecutionOutboxError(
                "malformed_payload", f"row {self.outbox_id!r} payload is not an object"
            )
        return parsed


@dataclass(frozen=True)
class ExecutionOutboxOutcome:
    """What the coordinator returns for one intent, in whatever state it is."""

    status: str
    outbox_id: str
    idempotency_key: str
    job_id: str | None = None
    artifact_id: str | None = None
    passport_id: str | None = None
    can_resume: bool = False
    resumed_from: str = ""
    poll_elapsed_seconds: float = 0.0
    detail: str = ""

    @property
    def terminal(self) -> bool:
        """True only for a state no further work can change."""
        return self.status in (
            ExecutionOutboxState.COMPLETED.value,
            ExecutionOutboxState.FAILED_TERMINAL.value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outbox_id": self.outbox_id,
            "idempotency_key": self.idempotency_key,
            "job_id": self.job_id,
            "artifact_id": self.artifact_id,
            "passport_id": self.passport_id,
            "can_resume": self.can_resume,
            "resumed_from": self.resumed_from,
            "poll_elapsed_seconds": self.poll_elapsed_seconds,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class VerifiedExecutionCompletion:
    """Independently verified proof that a job's artifact really exists.

    Produced by a ``verify_completion_fn`` and *required* before an intent may
    transition to ``COMPLETED``.  The coordinator validates every field, so a
    callback that returns an empty or malformed proof fails closed instead of
    promoting a worker's self-report into a durable success.
    """

    artifact_id: str
    artifact_sha256: str
    passport_id: str
    lineage_digest: str
    verified_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise CompletionVerificationError(
                "invalid_completion_proof", "artifact_id must be a non-empty string"
            )
        digest = self.artifact_sha256
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise CompletionVerificationError(
                "invalid_completion_proof",
                "artifact_sha256 must be 64 lowercase hex characters",
            )
        for field_name in ("passport_id", "lineage_digest", "verified_at_utc"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise CompletionVerificationError(
                    "invalid_completion_proof", f"{field_name} must be a non-empty string"
                )

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "passport_id": self.passport_id,
            "lineage_digest": self.lineage_digest,
            "verified_at_utc": self.verified_at_utc,
        }


@dataclass(frozen=True)
class _WorkerCompletion:
    """What the caller's worker driver reports back about one job."""

    status: str
    artifact_id: str = ""
    passport_id: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in ("COMPLETED", "SUCCEEDED", "VERIFIED")

    @property
    def failed(self) -> bool:
        return self.status in (
            "FAILED",
            "FAILED_TERMINAL",
            "FAILED_RETRYABLE",
            "CANCELLED",
            "VERIFICATION_FAILED",
        )


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS nexus_ibpc_execution_outbox (
        outbox_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        tenant_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        revision_jcs_sha256 TEXT NOT NULL,
        plan_payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        job_id TEXT,
        artifact_id TEXT,
        passport_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        reserved_job_id TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_ibpc_execution_outbox_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        occurred_at_utc TEXT NOT NULL
    )
    """,
)


class DurableExecutionOutboxStore:
    """SQLite WAL intent log for approval-to-enqueue execution.

    Writes are ``BEGIN IMMEDIATE``; the store serializes in-process callers with
    an ``RLock`` (a nested transaction joins the outer one) and relies on
    SQLite's own write lock across processes for the rest.  Reconciliation
    additionally takes an ``fcntl.flock`` on ``<db>.reconcile.lock`` so two
    reconcilers never race the same row.
    """

    def __init__(self, path: str | Path, *, now: Callable[[], str] = utc_now) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            for statement in _SCHEMA:
                self._conn.execute(statement)
            # Forward-compatible migration for a store written by the previous
            # revision: the reservation column is additive and defaults to ''.
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(nexus_ibpc_execution_outbox)")
            }
            if "reserved_job_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE nexus_ibpc_execution_outbox "
                    "ADD COLUMN reserved_job_id TEXT NOT NULL DEFAULT ''"
                )
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover - filesystems without POSIX modes
            pass

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> DurableExecutionOutboxStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.execute("COMMIT")

    # -- row access -------------------------------------------------------- #

    def get(self, *, outbox_id: str = "", idempotency_key: str = "") -> OutboxRow | None:
        """Read one row by id or key (exactly one selector must be given)."""
        if bool(outbox_id) == bool(idempotency_key):
            raise ExecutionOutboxError(
                "invalid_selector", "exactly one of outbox_id/idempotency_key is required"
            )
        with self._read_snapshot():
            if outbox_id:
                row = self._conn.execute(
                    "SELECT * FROM nexus_ibpc_execution_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM nexus_ibpc_execution_outbox WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
        return None if row is None else self._row(row)

    def list_in_flight(self, *, limit: int = 100) -> tuple[OutboxRow, ...]:
        """Every row a crash or a poll timeout left awaiting work, oldest first."""
        if limit < 1:
            raise ExecutionOutboxError("invalid_limit", f"limit must be >= 1, got {limit}")
        placeholders = ", ".join("?" for _ in IN_FLIGHT_STATES)
        with self._read_snapshot():
            rows = self._conn.execute(
                f"SELECT * FROM nexus_ibpc_execution_outbox WHERE state IN ({placeholders}) "
                "ORDER BY created_at_utc ASC, outbox_id ASC LIMIT ?",  # noqa: S608 - static SQL
                (*sorted(IN_FLIGHT_STATES), limit),
            ).fetchall()
        return tuple(self._row(row) for row in rows)

    def events_for(self, outbox_id: str) -> tuple[dict[str, Any], ...]:
        """The append-only transition log of one intent."""
        with self._read_snapshot():
            rows = self._conn.execute(
                "SELECT from_state, to_state, detail, occurred_at_utc "
                "FROM nexus_ibpc_execution_outbox_events WHERE outbox_id = ? "
                "ORDER BY event_id ASC",
                (outbox_id,),
            ).fetchall()
        return tuple(
            {
                "from_state": row[0],
                "to_state": row[1],
                "detail": row[2],
                "occurred_at_utc": row[3],
            }
            for row in rows
        )

    # -- transitions ------------------------------------------------------- #

    def stage_intent(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        project_id: str,
        session_id: str,
        turn_id: str,
        principal_id: str,
        approval_id: str,
        revision_id: str,
        revision_jcs_sha256: str,
        plan_payload: Mapping[str, Any],
    ) -> tuple[OutboxRow, bool]:
        """Stage ``INTENT_STAGED`` (idempotent); return ``(row, created)``.

        A retry with the same key and the same canonical payload returns the
        stored row unchanged.  A retry with a *different* payload raises
        :class:`ExecutionOutboxConflictError` — the intent is not a suggestion.
        """
        resolved = dict(plan_payload)
        _assert_payload_scope(
            resolved,
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            revision_jcs_sha256=revision_jcs_sha256,
        )
        payload_json = canonical_json(resolved)
        with self._transaction():
            existing = self._fetch_locked(idempotency_key=idempotency_key)
            if existing is not None:
                if not hmac.compare_digest(existing.plan_payload_json, payload_json):
                    raise ExecutionOutboxConflictError(
                        "payload_conflict",
                        f"idempotency_key {idempotency_key!r} already staged a different payload",
                    )
                # ``revision_jcs_sha256`` is part of the derived key, so a stored
                # row with this key necessarily carries this revision; only the
                # payload can disagree.  The scope guard below is what refuses a
                # revision that does not belong to this intent.
                return existing, False
            outbox_id = f"obx_{uuid.uuid4().hex}"
            now = self._now()
            # The reservation is written in the *same* transaction as the
            # INTENT_STAGED row, so no observer can ever see one without the
            # other.  ``reserved_job_id`` is NULL-free ('' means "not yet
            # derived") so an older store's rows still read back cleanly.
            reserved_job_id = derive_reserved_job_id(idempotency_key)
            self._conn.execute(
                """
                INSERT INTO nexus_ibpc_execution_outbox (
                    outbox_id, idempotency_key, tenant_id, project_id, session_id,
                    turn_id, principal_id, approval_id, revision_id,
                    revision_jcs_sha256, plan_payload_json, state, job_id,
                    artifact_id, passport_id, attempt_count, last_error,
                    created_at_utc, updated_at_utc, reserved_job_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTENT_STAGED', NULL,
                          NULL, NULL, 0, '', ?, ?, ?)
                """,
                (
                    outbox_id,
                    idempotency_key,
                    tenant_id,
                    project_id,
                    session_id,
                    turn_id,
                    principal_id,
                    approval_id,
                    revision_id,
                    revision_jcs_sha256,
                    payload_json,
                    now,
                    now,
                    reserved_job_id,
                ),
            )
            self._record_event_locked(
                outbox_id=outbox_id, from_state="", to_state="INTENT_STAGED", detail=""
            )
            row = self._fetch_locked(outbox_id=outbox_id)
        assert row is not None
        return row, True

    def mark_approval_consumed(self, *, outbox_id: str, detail: str = "") -> OutboxRow:
        """``INTENT_STAGED`` → ``APPROVAL_CONSUMED`` (only from that state)."""
        return self._transition(
            outbox_id=outbox_id,
            expected=("INTENT_STAGED",),
            target="APPROVAL_CONSUMED",
            detail=detail,
        )

    def mark_job_enqueued(self, *, outbox_id: str, job_id: str, detail: str = "") -> OutboxRow:
        """``APPROVAL_CONSUMED`` → ``JOB_ENQUEUED`` recording the durable job id."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ExecutionOutboxError("invalid_job_id", "job_id must be a non-empty string")
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state == ExecutionOutboxState.JOB_ENQUEUED.value:
                if not hmac.compare_digest(row.job_id or "", job_id):
                    raise ExecutionOutboxConflictError(
                        "job_already_enqueued",
                        f"row {outbox_id!r} already records job {row.job_id!r}, not {job_id!r}",
                    )
                return row
            if row.state != ExecutionOutboxState.APPROVAL_CONSUMED.value:
                raise ExecutionOutboxError(
                    "illegal_transition",
                    f"cannot record a job from state {row.state!r} (expected APPROVAL_CONSUMED)",
                )
            self._apply_locked(
                outbox_id=outbox_id,
                target="JOB_ENQUEUED",
                job_id=job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=row.last_error,
                attempt_increment=1,
            )
            self._record_event_locked(
                outbox_id=outbox_id,
                from_state=row.state,
                to_state="JOB_ENQUEUED",
                detail=detail or job_id,
            )
            return self._require_locked(outbox_id)

    def mark_awaiting_worker(
        self, *, outbox_id: str, job_id: str = "", detail: str = ""
    ) -> OutboxRow:
        """``JOB_ENQUEUED``/``AWAITING_WORKER`` → ``AWAITING_WORKER`` (not a failure)."""
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state not in (
                ExecutionOutboxState.JOB_ENQUEUED.value,
                ExecutionOutboxState.AWAITING_WORKER.value,
            ):
                raise ExecutionOutboxError(
                    "illegal_transition",
                    f"cannot await the worker from state {row.state!r}",
                )
            resolved_job_id = row.job_id or job_id
            if not resolved_job_id:
                raise ExecutionOutboxError(
                    "missing_job_id", f"row {outbox_id!r} has no job to await"
                )
            # ``detail`` carries the poll timeout (elapsed, observed status), so
            # a restarted operator can see *why* the turn is parked rather than
            # having to diff the event log.
            self._apply_locked(
                outbox_id=outbox_id,
                target="AWAITING_WORKER",
                job_id=resolved_job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=detail or row.last_error,
                attempt_increment=0,
            )
            self._record_event_locked(
                outbox_id=outbox_id,
                from_state=row.state,
                to_state="AWAITING_WORKER",
                detail=detail,
            )
            return self._require_locked(outbox_id)

    def mark_completed(
        self,
        *,
        outbox_id: str,
        artifact_id: str,
        passport_id: str,
        detail: str = "",
    ) -> OutboxRow:
        """Any in-flight state → ``COMPLETED`` with the verified artifact ids."""
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state == ExecutionOutboxState.COMPLETED.value:
                return row
            if row.state not in IN_FLIGHT_STATES:
                raise ExecutionOutboxError(
                    "illegal_transition", f"cannot complete from state {row.state!r}"
                )
            self._apply_locked(
                outbox_id=outbox_id,
                target="COMPLETED",
                job_id=row.job_id,
                artifact_id=artifact_id,
                passport_id=passport_id,
                last_error="",
                attempt_increment=0,
            )
            self._record_event_locked(
                outbox_id=outbox_id,
                from_state=row.state,
                to_state="COMPLETED",
                detail=detail,
            )
            return self._require_locked(outbox_id)

    def mark_failed_terminal(self, *, outbox_id: str, error: str) -> OutboxRow:
        """Any in-flight state → ``FAILED_TERMINAL`` with the recorded error."""
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state == ExecutionOutboxState.FAILED_TERMINAL.value:
                return row
            if row.state not in IN_FLIGHT_STATES:
                raise ExecutionOutboxError(
                    "illegal_transition", f"cannot fail from state {row.state!r}"
                )
            self._apply_locked(
                outbox_id=outbox_id,
                target="FAILED_TERMINAL",
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=error[:1000],
                attempt_increment=0,
            )
            self._record_event_locked(
                outbox_id=outbox_id,
                from_state=row.state,
                to_state="FAILED_TERMINAL",
                detail=error[:1000],
            )
            return self._require_locked(outbox_id)

    def confirm_reserved_consumption(self, *, outbox_id: str, detail: str = "") -> OutboxRow:
        """``INTENT_STAGED`` → ``APPROVAL_CONSUMED`` for a recovered reservation.

        Used *only* when the ledger proves the approval was consumed for this
        row's reserved job: the effect already happened, the outbox row simply
        never got its commit.  The event detail names the recovery so the log
        distinguishes a crash repair from a fresh consumption.
        """
        return self._transition(
            outbox_id=outbox_id,
            expected=("INTENT_STAGED",),
            target="APPROVAL_CONSUMED",
            detail=detail or "recovered_consumed_reservation",
        )

    def record_retryable_enqueue_failure(self, *, outbox_id: str, error: str) -> OutboxRow:
        """Hold an ``APPROVAL_CONSUMED`` row for retry, recording why.

        A transient enqueue failure must never destroy a spent approval: the row
        stays in ``APPROVAL_CONSUMED`` (still ``can_resume``) with the error and
        an incremented attempt count, so a later ``reconcile_in_flight`` retries
        ``enqueue_fn`` with the *same* idempotency key.
        """
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state != ExecutionOutboxState.APPROVAL_CONSUMED.value:
                raise ExecutionOutboxError(
                    "illegal_transition",
                    f"cannot park for retry from state {row.state!r}",
                )
            self._apply_locked(
                outbox_id=outbox_id,
                target="APPROVAL_CONSUMED",
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=error[:1000],
                attempt_increment=1,
            )
            self._record_event_locked(
                outbox_id=outbox_id,
                from_state=row.state,
                to_state="APPROVAL_CONSUMED",
                detail=f"retry_pending: {error}",
            )
            return self._require_locked(outbox_id)

    def record_attempt_failure(self, *, outbox_id: str, error: str) -> OutboxRow:
        """Log a recoverable error (attempt count +1) without leaving the state."""
        with self._transaction():
            row = self._require_locked(outbox_id)
            self._apply_locked(
                outbox_id=outbox_id,
                target=row.state,
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=error[:1000],
                attempt_increment=1,
            )
            return self._require_locked(outbox_id)

    # -- locking ----------------------------------------------------------- #

    @contextmanager
    def _file_lock(self, suffix: str) -> Iterator[None]:
        """Cross-process exclusive lock on ``<db><suffix>``.

        ``fcntl.flock`` is advisory, but it is held for the whole critical
        section, which is what stops two processes driving the same row into two
        different jobs.  On a platform without ``fcntl`` the scope degrades to
        this process's own lock (SQLite's write lock still serialises the
        transitions themselves).
        """
        try:
            import fcntl  # POSIX only; the deployment target is Linux
        except ImportError:  # pragma: no cover - non-POSIX host
            with self._lock:
                yield
            return
        lock_path = self._path.with_suffix(self._path.suffix + suffix)
        handle = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    @contextmanager
    def reconcile_lock(self) -> Iterator[None]:
        """Cross-process exclusive lock for one reconciliation pass.

        Retained under its historical name; it is the same lock
        :meth:`outbox_lock` takes, so a reconciler and a direct
        ``execute_or_recover`` call can never interleave on one row.
        """
        with self._file_lock(".reconcile.lock"):
            yield

    @contextmanager
    def outbox_lock(self) -> Iterator[None]:
        """Cross-process lock covering a whole ``execute_or_recover`` critical path.

        Distinct sidecar name (``<db>.outbox.lock``) from the reconciler's, but
        *the same* exclusion contract: any two writers that can drive one row —
        a reconciler pass and a direct execution, or two direct executions —
        serialize on it before reading the row's state.
        """
        with self._file_lock(".outbox.lock"):
            yield

    # -- internals --------------------------------------------------------- #

    def _transition(
        self, *, outbox_id: str, expected: Sequence[str], target: str, detail: str
    ) -> OutboxRow:
        with self._transaction():
            row = self._require_locked(outbox_id)
            if row.state == target:
                return row
            if row.state not in expected:
                raise ExecutionOutboxError(
                    "illegal_transition",
                    f"cannot move {outbox_id!r} from {row.state!r} to {target!r}",
                )
            self._apply_locked(
                outbox_id=outbox_id,
                target=target,
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                last_error=row.last_error,
                attempt_increment=0,
            )
            self._record_event_locked(
                outbox_id=outbox_id, from_state=row.state, to_state=target, detail=detail
            )
            return self._require_locked(outbox_id)

    def _apply_locked(
        self,
        *,
        outbox_id: str,
        target: str,
        job_id: str | None,
        artifact_id: str | None,
        passport_id: str | None,
        last_error: str,
        attempt_increment: int,
    ) -> None:
        self._conn.execute(
            """
            UPDATE nexus_ibpc_execution_outbox
            SET state = ?, job_id = ?, artifact_id = ?, passport_id = ?,
                last_error = ?, attempt_count = attempt_count + ?, updated_at_utc = ?
            WHERE outbox_id = ?
            """,
            (
                target,
                job_id,
                artifact_id,
                passport_id,
                last_error,
                attempt_increment,
                self._now(),
                outbox_id,
            ),
        )

    def _record_event_locked(
        self, *, outbox_id: str, from_state: str, to_state: str, detail: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO nexus_ibpc_execution_outbox_events "
            "(outbox_id, from_state, to_state, detail, occurred_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (outbox_id, from_state, to_state, detail[:1000], self._now()),
        )

    def _fetch_locked(self, *, outbox_id: str = "", idempotency_key: str = "") -> OutboxRow | None:
        if outbox_id:
            row = self._conn.execute(
                "SELECT * FROM nexus_ibpc_execution_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM nexus_ibpc_execution_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else self._row(row)

    def _require_locked(self, outbox_id: str) -> OutboxRow:
        row = self._fetch_locked(outbox_id=outbox_id)
        if row is None:
            raise ExecutionOutboxError("unknown_outbox", f"{outbox_id!r} does not exist")
        return row

    @staticmethod
    def _row(row: tuple[Any, ...]) -> OutboxRow:
        return OutboxRow(
            outbox_id=str(row[0]),
            idempotency_key=str(row[1]),
            tenant_id=str(row[2]),
            project_id=str(row[3]),
            session_id=str(row[4]),
            turn_id=str(row[5]),
            principal_id=str(row[6]),
            approval_id=str(row[7]),
            revision_id=str(row[8]),
            revision_jcs_sha256=str(row[9]),
            plan_payload_json=str(row[10]),
            state=str(row[11]),
            job_id=None if row[12] is None else str(row[12]),
            artifact_id=None if row[13] is None else str(row[13]),
            passport_id=None if row[14] is None else str(row[14]),
            attempt_count=int(row[15]),
            last_error=str(row[16]),
            created_at_utc=str(row[17]),
            updated_at_utc=str(row[18]),
            reserved_job_id=str(row[19]) if len(row) > 19 and row[19] is not None else "",
        )


class ExecutionOutboxCoordinator:
    """Drives one approval-to-execution intent, resuming from any crash point.

    Parameters
    ----------
    store:
        The durable outbox.
    consume_approval_fn:
        ``(approval_id, identity, job_id=..., attempt=...) -> None``; must raise
        on refusal.  Always presented the row's durable ``reserved_job_id`` so
        the ledger can recognise a recovery of the *same* intent.
    confirm_consumption_fn:
        Optional ``(row, identity) -> bool``; asked only after ``consume``
        refused.  Returns True when the ledger proves the approval was already
        consumed **for this row's reserved job** — i.e. the previous process
        crashed after ``approval.py`` committed and before the outbox committed
        ``APPROVAL_CONSUMED``.  Defaults to the equivalent check against
        ``DurableApprovalLedger`` reservation rows.
    enqueue_job_fn:
        ``(idempotency_key=..., payload=..., job_id=...) -> job_id``; must be
        idempotent on the key (the queue's own contract).  Called *only* when
        the row is ``APPROVAL_CONSUMED``.  ``job_id`` is optional for the
        callee (a callable that accepts only ``idempotency_key``/``payload``
        still works and simply receives the queue's own id).
    await_completion_fn:
        ``(job_id) -> worker-completion`` (mapping or an object with ``status``);
        polled until the job finishes or the budget expires.
    verify_completion_fn:
        **Required when a worker reports success.**  ``(row, completion) ->
        VerifiedExecutionCompletion``; a missing verifier, a raised error, or a
        malformed proof all fail closed — a completed job that cannot be
        independently verified is never reported as success.
    max_enqueue_attempts:
        How many transient enqueue failures ``reconcile_in_flight`` will retry
        before the intent is declared terminal.  ``0`` means "never give up".
    poll_timeout_seconds / poll_interval_seconds / clock:
        The poll budget and its (injected) clock; a timeout is
        ``AWAITING_WORKER``, never a failure.
    """

    def __init__(
        self,
        *,
        store: DurableExecutionOutboxStore,
        consume_approval_fn: Callable[..., None],
        enqueue_job_fn: Callable[..., str],
        await_completion_fn: Callable[[str], Any],
        verify_completion_fn: Callable[[OutboxRow, Any], Any] | None = None,
        confirm_consumption_fn: Callable[[OutboxRow, Any], bool] | None = None,
        poll_timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.01,
        max_enqueue_attempts: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_timeout_seconds < 0:
            raise ExecutionOutboxError("invalid_poll_timeout", "poll_timeout_seconds must be >= 0")
        if poll_interval_seconds < 0:
            raise ExecutionOutboxError(
                "invalid_poll_interval", "poll_interval_seconds must be >= 0"
            )
        if isinstance(max_enqueue_attempts, bool) or max_enqueue_attempts < 0:
            raise ExecutionOutboxError(
                "invalid_max_enqueue_attempts", "max_enqueue_attempts must be >= 0"
            )
        self._store = store
        self._consume = consume_approval_fn
        # ``None`` means "no crash-window repair is possible": a refused
        # consumption is then always terminal, which is the pre-Wave-1 behaviour
        # and is what a caller with a hand-rolled consume callback wants unless
        # it opts in.
        self._confirm = confirm_consumption_fn
        self._enqueue = enqueue_job_fn
        self._await = await_completion_fn
        self._verify = verify_completion_fn
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_enqueue_attempts = max_enqueue_attempts
        self._clock = clock
        self._sleep = sleep

    @property
    def store(self) -> DurableExecutionOutboxStore:
        return self._store

    def confirm_consumption_from_ledger(self, ledger: Any) -> Callable[[OutboxRow, Any], bool]:
        """Build a ``confirm_consumption_fn`` that interrogates a real ledger.

        The returned callable answers one question: *was this approval consumed
        for this very intent?*  It is True only when all four hold, so any other
        shape (another job, another plan, a finalized attempt, a revoked or
        expired binding) is reported as a genuine replay instead of silently
        repairing it:

        1. the binding's state is ``CONSUMED``;
        2. the stored reservation's job id equals this row's ``reserved_job_id``;
        3. the reservation is not finalized (no other attempt closed it);
        4. the reservation's identity fingerprint is this row's identity.
        """

        def _confirm(row: OutboxRow, identity: Any) -> bool:
            reserved = self._reserved_job_id(row)
            try:
                binding = ledger.get(row.approval_id)
            except Exception:
                return False
            if getattr(binding.state, "value", binding.state) != "CONSUMED":
                return False
            reservation = ledger._read_reservation(row.approval_id)
            if reservation is None:
                return False
            job_id, fingerprint, finalized_at, _attempt = reservation
            if finalized_at is not None:
                return False
            if not isinstance(job_id, str) or not hmac.compare_digest(job_id, reserved):
                return False
            return hmac.compare_digest(fingerprint, identity.identity_digest())

        return _confirm

    # -- public API -------------------------------------------------------- #

    def execute_or_recover(
        self,
        *,
        tenant_id: str,
        project_id: str,
        session_id: str,
        turn_id: str,
        principal_id: str,
        approval_id: str,
        revision_id: str,
        revision_jcs_sha256: str,
        plan_payload: Mapping[str, Any],
    ) -> ExecutionOutboxOutcome:
        """Run (or resume) the intent for one approved turn.

        Idempotent end to end: a second call with the same inputs returns the
        cached ``COMPLETED`` outcome, resumes a crashed intent from its recorded
        state, and never re-consumes or double-enqueues.

        The whole stage-then-drive sequence runs under the store's inter-process
        ``flock``.  Without it two workers calling this with one
        ``idempotency_key`` could both read ``INTENT_STAGED`` and both drive the
        row: one would consume the approval, the other would observe the consumed
        ledger and misclassify it as a replay.
        """
        key = derive_idempotency_key(
            tenant_id=tenant_id,
            project_id=project_id,
            session_id=session_id,
            turn_id=turn_id,
            approval_id=approval_id,
            revision_jcs_sha256=revision_jcs_sha256,
        )
        with self._store.outbox_lock():
            row, _created = self._store.stage_intent(
                idempotency_key=key,
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                turn_id=turn_id,
                principal_id=principal_id,
                approval_id=approval_id,
                revision_id=revision_id,
                revision_jcs_sha256=revision_jcs_sha256,
                plan_payload=plan_payload,
            )
            return self._drive(row)

    def reconcile_in_flight(self, *, limit: int = 100) -> tuple[ExecutionOutboxOutcome, ...]:
        """Drive every in-flight row forward under one cross-process lock."""
        with self._store.reconcile_lock(), self._store.outbox_lock():
            rows = self._store.list_in_flight(limit=limit)
            return tuple(self._drive(row) for row in rows)

    # -- the state machine ------------------------------------------------- #

    def _drive(self, row: OutboxRow) -> ExecutionOutboxOutcome:
        resumed_from = row.state
        if row.state == ExecutionOutboxState.COMPLETED.value:
            return self._outcome(row, resumed_from="")
        if row.state == ExecutionOutboxState.FAILED_TERMINAL.value:
            return ExecutionOutboxOutcome(
                status=ExecutionOutboxState.FAILED_TERMINAL.value,
                outbox_id=row.outbox_id,
                idempotency_key=row.idempotency_key,
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                can_resume=False,
                detail=row.last_error,
            )
        if row.state == ExecutionOutboxState.INTENT_STAGED.value:
            row = self._consume_approval(row)
        if row.state == ExecutionOutboxState.APPROVAL_CONSUMED.value:
            row = self._enqueue_job(row)
        if row.state == ExecutionOutboxState.JOB_ENQUEUED.value:
            row = self._await_worker(row)
        if row.state == ExecutionOutboxState.AWAITING_WORKER.value:
            # A previous poll timed out.  Try once more: the worker may have
            # finished since (a *short* extra probe, never a second budget).
            row = self._probe_once(row)
        if row.state == ExecutionOutboxState.COMPLETED.value:
            return self._outcome(row, resumed_from=resumed_from)
        if row.state == ExecutionOutboxState.FAILED_TERMINAL.value:
            return ExecutionOutboxOutcome(
                status=ExecutionOutboxState.FAILED_TERMINAL.value,
                outbox_id=row.outbox_id,
                idempotency_key=row.idempotency_key,
                job_id=row.job_id,
                artifact_id=row.artifact_id,
                passport_id=row.passport_id,
                can_resume=False,
                resumed_from=resumed_from,
                detail=row.last_error,
            )
        return ExecutionOutboxOutcome(
            status=row.state,
            outbox_id=row.outbox_id,
            idempotency_key=row.idempotency_key,
            job_id=row.job_id,
            artifact_id=row.artifact_id,
            passport_id=row.passport_id,
            can_resume=row.state in IN_FLIGHT_STATES,
            resumed_from=resumed_from,
            detail=row.last_error,
        )

    def _reserved_job_id(self, row: OutboxRow) -> str:
        """The row's durable reservation, or the derived one for a legacy row."""
        return row.reserved_job_id or derive_reserved_job_id(row.idempotency_key)

    def _consume_approval(self, row: OutboxRow) -> OutboxRow:
        """Drive ``INTENT_STAGED`` → ``APPROVAL_CONSUMED`` exactly once.

        Three outcomes, and the difference between the first two is the whole
        point of this method:

        * the ledger consumes the approval → mark the row consumed;
        * the ledger *refuses* but confirms the approval was consumed for this
          row's reserved job → the previous process crashed in the window
          between the ledger's commit and ours; advance the row, do not fail;
        * the ledger refuses and does not confirm → a genuine replay (different
          job, different plan, revoked, expired).  Terminal, with the typed
          reason preserved.
        """
        try:
            identity = approval_identity_for(row)
        except Exception as exc:
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"approval_identity_failed: {type(exc).__name__}: {exc}",
            )
        reserved = self._reserved_job_id(row)
        try:
            self._consume(row.approval_id, identity, job_id=reserved, attempt=1)
        except Exception as exc:
            # Before declaring the intent dead, ask the ledger whether the crash
            # window is what we are looking at.
            if self._confirm is not None:
                try:
                    recovered = bool(self._confirm(row, identity))
                except Exception:
                    recovered = False
                if recovered:
                    return self._store.confirm_reserved_consumption(
                        outbox_id=row.outbox_id,
                        detail=f"recovered_reservation:{reserved}",
                    )
            # The single-use approval is the one thing the outbox cannot repair:
            # a refusal here means the intent can never execute as approved.
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"approval_consumption_failed: {type(exc).__name__}: {exc}",
            )
        return self._store.mark_approval_consumed(outbox_id=row.outbox_id, detail=row.approval_id)

    def _enqueue_job(self, row: OutboxRow) -> OutboxRow:
        """Enqueue the job, never destroying a spent approval for a transient error.

        Once the row is ``APPROVAL_CONSUMED`` the approval is gone: marking the
        row ``FAILED_TERMINAL`` would burn it and require a fresh human approval
        for what may be a one-second queue hiccup.  A *validation* refusal
        (``ValueError``) is different — the payload can never be accepted — so
        that, and an exhausted ``max_enqueue_attempts``, are the only terminal
        enqueue outcomes.
        """
        try:
            job_id = self._enqueue(
                idempotency_key=row.idempotency_key,
                payload=row.plan_payload,
                job_id=self._reserved_job_id(row),
            )
        except ValueError as exc:
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"enqueue_rejected: {type(exc).__name__}: {exc}",
            )
        except Exception as exc:
            attempts = row.attempt_count + 1
            if self._max_enqueue_attempts and attempts >= self._max_enqueue_attempts:
                return self._store.mark_failed_terminal(
                    outbox_id=row.outbox_id,
                    error=f"enqueue_attempts_exhausted({attempts}): {type(exc).__name__}: {exc}",
                )
            # Durable fact, resumable state: the next reconcile retries with the
            # same idempotency key, so the queue's own dedupe makes it safe.
            return self._store.record_retryable_enqueue_failure(
                outbox_id=row.outbox_id,
                error=f"enqueue_retryable: {type(exc).__name__}: {exc}",
            )
        if not isinstance(job_id, str) or not job_id.strip():
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id, error="enqueue_returned_no_job_id"
            )
        return self._store.mark_job_enqueued(outbox_id=row.outbox_id, job_id=job_id)

    def _await_worker(self, row: OutboxRow) -> OutboxRow:
        job_id = row.job_id or ""
        started = self._clock()
        while True:
            completion = self._completion(job_id)
            if completion.succeeded:
                return self._complete(row, completion)
            if completion.failed:
                return self._store.mark_failed_terminal(
                    outbox_id=row.outbox_id,
                    error=f"worker_failed: {completion.status}: {completion.detail}",
                )
            elapsed = self._clock() - started
            if elapsed >= self._poll_timeout:
                # NOT a failure: the worker is still running.  Record the durable
                # fact and let a later reconciliation finish the turn.
                return self._store.mark_awaiting_worker(
                    outbox_id=row.outbox_id,
                    detail=f"poll_timeout_after={elapsed:.6f}s status={completion.status}",
                )
            self._sleep(self._poll_interval)

    def _probe_once(self, row: OutboxRow) -> OutboxRow:
        job_id = row.job_id or ""
        completion = self._completion(job_id)
        if completion.succeeded:
            return self._complete(row, completion)
        if completion.failed:
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"worker_failed: {completion.status}: {completion.detail}",
            )
        return row

    def _complete(self, row: OutboxRow, completion: _WorkerCompletion) -> OutboxRow:
        """``AWAITING_WORKER``/``JOB_ENQUEUED`` → ``COMPLETED``, or hold.

        "The job finished" is a claim by the worker; ``COMPLETED`` is a durable
        assertion that a passport exists for a real artifact.  Only an
        independently verified :class:`VerifiedExecutionCompletion` may cross
        that line, so a missing verifier or an unusable proof leaves the row in
        ``AWAITING_WORKER`` (``can_resume``) rather than promoting it — the turn
        stays open and reconcilable instead of being recorded as done.
        """
        if self._verify is None:
            return self._hold_for_verification(
                row, CompletionVerificationError("verify_completion_fn_required")
            )
        try:
            proof = self._verify(row, completion)
        except CompletionVerificationError as exc:
            return self._hold_for_verification(row, exc)
        except Exception as exc:
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"verification_failed: {type(exc).__name__}: {exc}",
            )
        if not isinstance(proof, VerifiedExecutionCompletion):
            return self._hold_for_verification(
                row,
                CompletionVerificationError(
                    "invalid_completion_proof",
                    f"verify_completion_fn returned {type(proof).__name__}, "
                    "expected VerifiedExecutionCompletion",
                ),
            )
        return self._store.mark_completed(
            outbox_id=row.outbox_id,
            artifact_id=proof.artifact_id,
            passport_id=proof.passport_id,
            detail=f"job={row.job_id} artifact_sha256={proof.artifact_sha256} "
            f"lineage={proof.lineage_digest}",
        )

    def _hold_for_verification(self, row: OutboxRow, error: Exception) -> OutboxRow:
        """Park a completion claim that cannot be verified, without losing it."""
        try:
            return self._store.mark_awaiting_worker(
                outbox_id=row.outbox_id,
                detail=f"completion_unverified: {error}",
            )
        except ExecutionOutboxError:
            # The row is not in a state that can await a worker (e.g. it was
            # never enqueued).  Nothing can verify it, so the claim is refused.
            return self._store.mark_failed_terminal(
                outbox_id=row.outbox_id,
                error=f"completion_unverified: {error}",
            )

    def _completion(self, job_id: str) -> _WorkerCompletion:
        raw = self._await(job_id)
        if isinstance(raw, _WorkerCompletion):
            return raw
        if isinstance(raw, Mapping):
            return _WorkerCompletion(
                status=str(raw.get("status", "PENDING")),
                artifact_id=str(raw.get("artifact_id") or ""),
                passport_id=str(raw.get("passport_id") or ""),
                detail=str(raw.get("detail") or ""),
            )
        status = getattr(raw, "status", None)
        if status is None:
            raise ExecutionOutboxError(
                "malformed_completion",
                f"await_completion_fn returned {type(raw).__name__} without a status",
            )
        return _WorkerCompletion(
            status=str(getattr(status, "value", status)),
            artifact_id=str(getattr(raw, "artifact_id", "") or ""),
            passport_id=str(getattr(raw, "passport_id", "") or ""),
            detail=str(getattr(raw, "detail", "") or ""),
        )

    @staticmethod
    def _outcome(row: OutboxRow, *, resumed_from: str) -> ExecutionOutboxOutcome:
        return ExecutionOutboxOutcome(
            status=row.state,
            outbox_id=row.outbox_id,
            idempotency_key=row.idempotency_key,
            job_id=row.job_id,
            artifact_id=row.artifact_id,
            passport_id=row.passport_id,
            can_resume=False,
            resumed_from=resumed_from,
            detail=row.last_error,
        )
