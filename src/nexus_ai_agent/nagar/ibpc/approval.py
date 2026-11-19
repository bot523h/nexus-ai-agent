"""Single-use, fully identity-bound approval ledger for the IBPC layer.

Rules enforced here (mission NAGAR-IBPC-SECURITY-CLOSURE-01 §4):

* an approval is valid ONLY in the exact context it was issued in:

  ``Approval Identity = principal + tenant + project + session + turn +
  sequence + source revision + asset digest + AST digest + plan digest +
  preview digest``

  Every one of those fields is re-checked against the live request at
  ``approve`` AND at ``consume`` — a binding from session A can never be
  approved or consumed in session B, turn B, project B or tenant B;
* ``APPROVED → CONSUMED`` happens exactly once, atomically (the compare-and-set):
  concurrent consumers serialize, the first wins, every later one is a typed
  ``replay`` refusal;
* revocation (interrupt/cancel/stale turn) moves the binding to REVOKED and no
  later call can resurrect it;
* the binding is **not** a permission grant — it is evidence of a human
  decision; the canonical Policy/CommandBus/queue still authorize.

Two storage backends share one transition contract:

* :class:`ApprovalLedger` keeps bindings in process memory. This is the default
  and is what the IBPC session path uses; restart clears it
  (docs/engineering/NAGAR_IBPC.md §Lifecycle).
* :class:`DurableApprovalLedger` persists the same bindings in a SQLite file.
  Every operation runs in ``BEGIN IMMEDIATE``, so the single-use check holds
  across restarts and across processes that open the same file.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
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
    """Thread-safe, single-use, fully identity-bound approval bindings.

    Every state transition runs inside :meth:`_transaction`. The in-memory
    default keeps bindings in a process-local dict. :class:`DurableApprovalLedger`
    overrides only the storage hooks, so the transition logic is shared and
    cannot drift between the two backends.
    """

    def __init__(self, *, now: Callable[[], str]) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._bindings: dict[str, ApprovalBinding] = {}

    # --- storage seam -------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize one read-check-write unit. Re-entrant."""
        with self._lock:
            yield

    def _fetch(self, approval_id: str) -> ApprovalBinding | None:
        return self._bindings.get(approval_id)

    def _store(self, binding: ApprovalBinding) -> None:
        self._bindings[binding.approval_id] = binding

    def _insert(self, binding: ApprovalBinding) -> bool:
        """Insert only when the id is absent. ``False`` means a duplicate."""
        if binding.approval_id in self._bindings:
            return False
        self._bindings[binding.approval_id] = binding
        return True

    def _all(self) -> tuple[ApprovalBinding, ...]:
        return tuple(self._bindings.values())

    # --- public contract ----------------------------------------------------

    def create(self, binding: ApprovalBinding) -> ApprovalBinding:
        """Register a PENDING binding. Re-creating the same id is refused."""
        if binding.state is not ApprovalState.PENDING:
            raise ApprovalError("invalid_state", "only PENDING bindings can be registered")
        with self._transaction():
            stored = binding.model_copy(update={"created_at": self._now()})
            if not self._insert(stored):
                raise ApprovalError("duplicate_approval", binding.approval_id)
            return stored

    def get(self, approval_id: str) -> ApprovalBinding:
        binding = self._fetch(approval_id)
        if binding is None:
            raise ApprovalError("unknown_approval", approval_id)
        return binding

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
            binding = self.get(approval_id)
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
            self._store(approved)
            return approved

    def consume(self, approval_id: str, *, identity: ApprovalIdentity) -> ApprovalBinding:
        """APPROVED → CONSUMED, exactly once.

        The whole read-check-transition runs inside one transaction. For the
        durable backend that is a ``BEGIN IMMEDIATE`` write lock, so two
        processes cannot both win: the first commits CONSUMED and every later
        call reads CONSUMED and is a typed ``replay`` refusal. A refused call
        rolls back and leaves the approval exactly as it was.
        """
        with self._transaction():
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "approval already consumed — execution is single-use")
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked")
            if binding.state is not ApprovalState.APPROVED:
                raise ApprovalError("not_approved", f"cannot consume from {binding.state.value}")
            self._check_identity(binding, identity)
            consumed = binding.model_copy(update={"state": ApprovalState.CONSUMED})
            self._store(consumed)
            return consumed

    def revoke(self, approval_id: str) -> ApprovalBinding:
        """REVOKED — terminal. Used on interrupt/cancel/stale turn."""
        with self._transaction():
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "a consumed approval cannot be revoked")
            revoked = binding.model_copy(update={"state": ApprovalState.REVOKED})
            self._store(revoked)
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
                    out.append(self.revoke(binding.approval_id))
            return tuple(out)


class DurableApprovalLedger(ApprovalLedger):
    """The same single-use ledger, persisted in a SQLite file.

    Bindings survive a restart and are shared by every process that opens the
    same file. Each outermost operation runs in ``BEGIN IMMEDIATE``, a
    cross-process write lock, so the consume compare-and-set holds across
    processes as well as threads. Rows keep the canonical JSON of the binding;
    the state, session and turn columns are denormalised only for lookup.
    """

    def __init__(self, path: str | Path, *, now: Callable[[], str]) -> None:
        super().__init__(now=now)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            " approval_id TEXT PRIMARY KEY,"
            " session_id TEXT NOT NULL,"
            " turn_id TEXT NOT NULL,"
            " state TEXT NOT NULL,"
            " body TEXT NOT NULL)"
        )
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                # A nested call (revoke_turn → revoke) joins the open write lock.
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

    def _fetch(self, approval_id: str) -> ApprovalBinding | None:
        row = self._conn.execute(
            "SELECT body FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        return ApprovalBinding.model_validate_json(row[0])

    def _store(self, binding: ApprovalBinding) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO approvals (approval_id, session_id, turn_id, state, body)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                binding.approval_id,
                binding.session_id,
                binding.turn_id,
                binding.state.value,
                binding.model_dump_json(),
            ),
        )

    def _insert(self, binding: ApprovalBinding) -> bool:
        if self._fetch(binding.approval_id) is not None:
            return False
        self._store(binding)
        return True

    def _all(self) -> tuple[ApprovalBinding, ...]:
        rows = self._conn.execute("SELECT body FROM approvals ORDER BY rowid").fetchall()
        return tuple(ApprovalBinding.model_validate_json(row[0]) for row in rows)


def durable_approval_ledger_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for the studio's durable approval ledger."""
    return Path(workspace) / "queue" / "approvals.sqlite3"


__all__ = [
    "ApprovalError",
    "ApprovalIdentity",
    "ApprovalLedger",
    "DurableApprovalLedger",
    "durable_approval_ledger_path",
]
