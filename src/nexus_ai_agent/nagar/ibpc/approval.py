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

The ledger is in-memory session state, not a durable evidence store: durable
evidence lives in the existing provenance journal/passport (see
:mod:`nagar.ibpc.service`). No parallel ledger is created. Restart semantics:
all bindings vanish (documented in docs/engineering/NAGAR_IBPC.md §Lifecycle).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

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

    def create(self, binding: ApprovalBinding) -> ApprovalBinding:
        """Register a PENDING binding. Re-creating the same id is refused."""
        if binding.state is not ApprovalState.PENDING:
            raise ApprovalError("invalid_state", "only PENDING bindings can be registered")
        with self._lock:
            if binding.approval_id in self._bindings:
                raise ApprovalError("duplicate_approval", binding.approval_id)
            stored = binding.model_copy(update={"created_at": self._now()})
            self._bindings[stored.approval_id] = stored
            return stored

    def get(self, approval_id: str) -> ApprovalBinding:
        with self._lock:
            try:
                return self._bindings[approval_id]
            except KeyError:
                raise ApprovalError("unknown_approval", approval_id) from None

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
        with self._lock:
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
            self._bindings[approval_id] = approved
            return approved

    def consume(self, approval_id: str, *, identity: ApprovalIdentity) -> ApprovalBinding:
        """APPROVED → CONSUMED, exactly once.

        The whole read-check-transition runs under the ledger lock, so two
        concurrent consumers cannot both win: the first transitions the state,
        every later call sees CONSUMED and is a typed ``replay`` refusal.
        """
        with self._lock:
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "approval already consumed — execution is single-use")
            if binding.state is ApprovalState.REVOKED:
                raise ApprovalError("revoked", "approval was revoked")
            if binding.state is not ApprovalState.APPROVED:
                raise ApprovalError("not_approved", f"cannot consume from {binding.state.value}")
            self._check_identity(binding, identity)
            consumed = binding.model_copy(update={"state": ApprovalState.CONSUMED})
            self._bindings[approval_id] = consumed
            return consumed

    def revoke(self, approval_id: str) -> ApprovalBinding:
        """REVOKED — terminal. Used on interrupt/cancel/stale turn."""
        with self._lock:
            binding = self.get(approval_id)
            if binding.state is ApprovalState.CONSUMED:
                raise ApprovalError("replay", "a consumed approval cannot be revoked")
            revoked = binding.model_copy(update={"state": ApprovalState.REVOKED})
            self._bindings[approval_id] = revoked
            return revoked

    def revoke_turn(self, session_id: str, turn_id: str) -> tuple[ApprovalBinding, ...]:
        """Revoke every non-terminal binding of one turn (interrupt/cancel)."""
        with self._lock:
            out: list[ApprovalBinding] = []
            for approval_id, binding in list(self._bindings.items()):
                if (
                    binding.session_id == session_id
                    and binding.turn_id == turn_id
                    and binding.state in (ApprovalState.PENDING, ApprovalState.APPROVED)
                ):
                    out.append(self.revoke(approval_id))
            return tuple(out)


__all__ = ["ApprovalError", "ApprovalIdentity", "ApprovalLedger"]
