"""Single-use, fully identity-bound approval ledger for the IBPC layer.

Rules enforced here (mission NAGAR-IBPC-SECURITY-CLOSURE-01 §4):

* an approval is valid ONLY in the exact context it was issued in:

  ``Approval Identity = principal + tenant + project + session + turn +
  sequence + source revision + asset digest + AST digest + plan digest +
  preview digest``

  Every one of those fields is re-checked against the live request at
  ``approve`` AND at ``consume`` — a binding from session A can never be
  approved or consumed in session B, turn B, project B or tenant B;
* ``APPROVED → CONSUMED`` happens exactly once, atomically under the ledger
  lock (the compare-and-set): concurrent consumers serialize, the first wins,
  every later one is a typed ``replay`` refusal;
* revocation (interrupt/cancel/stale turn) moves the binding to REVOKED and no
  later call can resurrect it;
* the binding is **not** a permission grant — it is evidence of a human
  decision; the canonical Policy/CommandBus/queue still authorize.

The default ledger is process-local. DurableApprovalLedger provides the same
authority and state transitions backed by a transactional SQLite database.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, ApprovalState

#: The immutable identity fields of an approval. ``ApprovalIdentity.of_request``
#: builds it from the live request context; every field is compared against the
#: stored binding on every sensitive operation.
_IDENTITY_FIELDS = (
    "principal_id",
    "tenant_id",
    "project_id",
    "session_id",
    "turn_id",
    "sequence_number",
    "source_revision",
    "source_asset_digest",
    "ast_digest",
    "plan_digest",
    "preview_digest",
)


@dataclass(frozen=True)
class ApprovalIdentity:
    """The full identity a request must present to touch a binding."""

    principal_id: str
    tenant_id: str
    project_id: str
    session_id: str
    turn_id: str
    sequence_number: int
    source_revision: int
    source_asset_digest: str
    ast_digest: str
    plan_digest: str
    preview_digest: str

    def as_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in _IDENTITY_FIELDS}

    def identity_digest(self) -> str:
        return digest_of(self.as_dict())

    def mismatches(self, binding: ApprovalBinding) -> tuple[tuple[str, str, str], ...]:
        """Every (field, expected, actual) where the request disagrees with the
        stored binding. Empty tuple means the identity matches exactly."""
        out: list[tuple[str, str, str]] = []
        for name in _IDENTITY_FIELDS:
            actual = getattr(binding, name)
            expected = getattr(self, name)
            if actual != expected:
                out.append((name, str(expected), str(actual)))
        return tuple(out)


class ApprovalError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class ApprovalLedger:
    """Thread-safe, single-use, fully identity-bound approval bindings."""

    def __init__(self, *, now: Callable[[], str]) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._bindings: dict[str, ApprovalBinding] = {}
        # A missing reservation for a CONSUMED legacy binding means finalized.
        self._reservations: dict[str, tuple[str, str, str | None]] = {}

    @contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        with self._lock:
            yield

    def _read(self, approval_id: str) -> ApprovalBinding:
        try:
            return self._bindings[approval_id]
        except KeyError:
            raise ApprovalError("unknown_approval", approval_id) from None

    def _write(self, binding: ApprovalBinding) -> None:
        self._bindings[binding.approval_id] = binding

    def _all(self) -> tuple[ApprovalBinding, ...]:
        return tuple(self._bindings.values())

    def _read_reservation(self, approval_id: str) -> tuple[str, str, str | None] | None:
        return self._reservations.get(approval_id)

    def _write_reservation(
        self, approval_id: str, job_id: str, fingerprint: str, finalized_at: str | None
    ) -> None:
        self._reservations[approval_id] = (job_id, fingerprint, finalized_at)

    def create(self, binding: ApprovalBinding) -> ApprovalBinding:
        """Register a PENDING binding. Re-creating the same id is refused."""
        if binding.state is not ApprovalState.PENDING:
            raise ApprovalError("invalid_state", "only PENDING bindings can be registered")
        with self._transaction():
            try:
                self._read(binding.approval_id)
            except ApprovalError as exc:
                if exc.reason != "unknown_approval":
                    raise
            else:
                raise ApprovalError("duplicate_approval", binding.approval_id)
            stored = binding.model_copy(update={"created_at": self._now()})
            self._write(stored)
            return stored

    def get(self, approval_id: str) -> ApprovalBinding:
        with self._transaction():
            return self._read(approval_id)

    @staticmethod
    def _check_identity(binding: ApprovalBinding, identity: ApprovalIdentity) -> None:
        """Fail-closed full identity comparison. Every field must match; the
        first mismatch raises a typed error naming the field. The immutable
        identity digest is compared as a whole as well, so a binding whose
        identity was tampered with can never validate."""
        mismatches = identity.mismatches(binding)
        if mismatches:
            field, expected, actual = mismatches[0]
            raise ApprovalError(
                f"identity_mismatch_{field}",
                f"expected {expected!r}, binding holds {actual!r} "
                f"({len(mismatches)} field(s) differ)",
            )
        # whole-digest tamper check: the stored binding's own immutable identity
        # digest must equal the digest recomputed from the request identity plus
        # the binding's volatile-free fields (schema id, approval id, nonce)
        expected = digest_of(
            {
                "schema_id": binding.schema_id,
                "approval_id": binding.approval_id,
                "nonce": binding.nonce,
                **identity.as_dict(),
            }
        )
        if expected != binding.identity_digest:
            raise ApprovalError("identity_digest_mismatch", "immutable identity digest differs")

    def approve(
        self,
        approval_id: str,
        *,
        identity: ApprovalIdentity,
        turn_is_active: Callable[[], bool],
    ) -> ApprovalBinding:
        """PENDING → APPROVED, only when the full identity still matches."""
        with self._transaction():
            binding = self._read(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "approval already consumed")
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked (interrupt/cancel/stale)")
            if binding.state is not ApprovalState.PENDING:
                raise ApprovalError("invalid_state", f"cannot approve from {binding.state.value}")
            if not turn_is_active():
                raise ApprovalError("turn_inactive", "the turn is no longer active")
            self._check_identity(binding, identity)
            approved = binding.model_copy(
                update={"state": ApprovalState.APPROVED, "decided_at": self._now()}
            )
            self._write(approved)
            return approved

    def consume(
        self, approval_id: str, *, identity: ApprovalIdentity, job_id: str | None = None
    ) -> ApprovalBinding:
        """Consume once, or reserve for recovery of the same identified job.

        The optional reservation is not an execution fence: the queue must
        independently prevent overlapping attempts from publishing concurrently.
        """
        if job_id is not None and (not isinstance(job_id, str) or not job_id.strip()):
            raise ApprovalError("invalid_job_id", "job id must be a non-empty string")
        with self._transaction():
            binding = self._read(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                reservation = self._read_reservation(approval_id)
                if (
                    job_id is None
                    or reservation is None
                    or reservation[0] != job_id
                    or reservation[2] is not None
                    or reservation[1] != identity.identity_digest()
                ):
                    raise ApprovalError("replay", "approval already consumed")
                try:
                    self._check_identity(binding, identity)
                except ApprovalError as exc:
                    raise ApprovalError("replay", "reserved identity changed") from exc
                return binding
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked")
            if binding.state is not ApprovalState.APPROVED:
                raise ApprovalError("not_approved", f"cannot consume from {binding.state.value}")
            self._check_identity(binding, identity)
            consumed = binding.model_copy(update={"state": ApprovalState.CONSUMED})
            self._write(consumed)
            if job_id is not None:
                self._write_reservation(approval_id, job_id, identity.identity_digest(), None)
            return consumed

    def finalize_consumption(self, approval_id: str, *, job_id: str) -> None:
        """Close a reservation owned by this job, preventing future recovery."""
        with self._transaction():
            binding = self._read(approval_id)
            reservation = self._read_reservation(approval_id)
            if (
                binding.state is not ApprovalState.CONSUMED
                or reservation is None
                or reservation[0] != job_id
                or reservation[2] is not None
            ):
                raise ApprovalError("replay", "no open reservation for this job")
            self._write_reservation(approval_id, job_id, reservation[1], self._now())

    def revoke(self, approval_id: str) -> ApprovalBinding:
        """REVOKED — terminal. Used on interrupt/cancel/stale turn."""
        with self._transaction():
            binding = self._read(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "a consumed approval cannot be revoked")
            revoked = binding.model_copy(update={"state": ApprovalState.REVOKED})
            self._write(revoked)
            return revoked

    def revoke_turn(self, session_id: str, turn_id: str) -> tuple[ApprovalBinding, ...]:
        """Revoke every non-terminal binding of one turn (interrupt/cancel)."""
        with self._transaction():
            out: list[ApprovalBinding] = []
            for binding in self._all():
                if (
                    binding.session_id == session_id
                    and binding.turn_id == turn_id
                    and binding.state in (ApprovalState.PENDING, ApprovalState.APPROVED)
                ):
                    revoked = binding.model_copy(update={"state": ApprovalState.REVOKED})
                    self._write(revoked)
                    out.append(revoked)
            return tuple(out)


class DurableApprovalLedger(ApprovalLedger):
    """The IBPC approval ledger persisted with cross-process atomic transitions."""

    def __init__(self, path: str | Path, *, now: Callable[[], str]) -> None:
        super().__init__(now=now)
        self._path = Path(path)
        # Create with restrictive permissions, without following a final symlink.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._connection = sqlite3.connect(
            self._path, timeout=5, isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            "approval_id TEXT PRIMARY KEY, binding TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS approval_reservations ("
            "approval_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "fingerprint TEXT NOT NULL, finalized_at TEXT)"
        )

    def _read_reservation(self, approval_id: str) -> tuple[str, str, str | None] | None:
        row = self._connection.execute(
            "SELECT job_id, fingerprint, finalized_at FROM approval_reservations "
            "WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return (row[0], row[1], row[2]) if row is not None else None

    def _write_reservation(
        self, approval_id: str, job_id: str, fingerprint: str, finalized_at: str | None
    ) -> None:
        self._connection.execute(
            "INSERT INTO approval_reservations (approval_id, job_id, fingerprint, finalized_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(approval_id) DO UPDATE SET "
            "job_id=excluded.job_id, fingerprint=excluded.fingerprint, "
            "finalized_at=excluded.finalized_at",
            (approval_id, job_id, fingerprint, finalized_at),
        )

    @contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    def _read(self, approval_id: str) -> ApprovalBinding:
        row = self._connection.execute(
            "SELECT binding FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalError("unknown_approval", approval_id)
        return ApprovalBinding.model_validate_json(row[0])

    def _write(self, binding: ApprovalBinding) -> None:
        self._connection.execute(
            "INSERT INTO approvals (approval_id, binding) VALUES (?, ?) "
            "ON CONFLICT(approval_id) DO UPDATE SET binding=excluded.binding",
            (binding.approval_id, binding.model_dump_json()),
        )

    def _all(self) -> tuple[ApprovalBinding, ...]:
        return tuple(
            ApprovalBinding.model_validate_json(row[0])
            for row in self._connection.execute("SELECT binding FROM approvals")
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = ["ApprovalError", "ApprovalIdentity", "ApprovalLedger", "DurableApprovalLedger"]
